"""Upload handling: validate, read text out of, and immediately forget the file.

The document never touches disk. ``analyse`` works on the bytes it is handed and
returns text plus metadata; the caller drops the bytes as soon as it is done, which
is what makes the "delete after processing" promise structurally true rather than a
cleanup routine that might not run.
"""

from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path

from ..config import settings
from ..errors import EmptyDocument, FileTooLarge, UnsupportedFile, log_event

_MAGIC = {
    b"%PDF": "application/pdf",
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
}


def sniff_type(data: bytes, declared: str | None) -> str:
    """Trust the bytes, not the browser's content-type header."""
    for magic, media_type in _MAGIC.items():
        if data.startswith(magic):
            return media_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) > 12 and data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "image/heic"
    if declared == "text/plain" or _looks_like_text(data):
        return "text/plain"
    return declared or "application/octet-stream"


def _looks_like_text(data: bytes) -> bool:
    sample = data[:2048]
    if not sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for byte in sample if 32 <= byte < 127 or byte >= 0x80 or byte in (9, 10, 13))
    return printable / len(sample) > 0.9


def validate(data: bytes, filename: str, declared: str | None) -> str:
    if not data:
        raise EmptyDocument("empty upload")
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) > limit:
        log_event("upload_rejected", reason="too_large", bytes=len(data), name=filename)
        raise FileTooLarge(f"{len(data)} bytes")
    media_type = sniff_type(data, declared)
    if media_type not in settings.allowed_upload_types:
        log_event("upload_rejected", reason="bad_type", media_type=media_type, name=filename)
        raise UnsupportedFile(media_type)
    return media_type


def extract_text(data: bytes, media_type: str) -> tuple[str, dict]:
    """Return ``(text, info)``. Images produce no text here on purpose.

    Images are sent to the model's vision input when a key is configured; offline we
    refuse to guess instead of pretending we could read them.
    """
    info: dict = {"media_type": media_type}
    if media_type == "application/pdf":
        text, pages = _pdf_text(data)
        info.update(pages=pages, chars=len(text), read_mode="pdf_text")
        return text, info
    if media_type == "text/plain":
        text = data.decode("utf-8", errors="replace")
        info.update(pages=1, chars=len(text), read_mode="plain_text")
        return text, info
    info.update(pages=None, chars=0, read_mode="image_no_ocr")
    return "", info


def _pdf_text(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency is pinned
        return "", 0
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:  # malformed PDF
        log_event("pdf_parse_failed", error=type(exc).__name__)
        return "", 0
    return normalise_extracted_text(text), pages


# Mid-word breaks that the PDF extractor produced with a space in them, so they
# cannot be repaired by the vocabulary check below. Curated by reading the extracted
# text of this specific document; if the source file changes, regenerate this list.
_FIXUPS = {
    "vic tims": "victims",
    "vic tim": "victim",
    "appropr iate": "appropriate",
    "punishab le": "punishable",
    "throug h": "through",
    "insol vent": "insolvent",
    "involv ing": "involving",
    "partnersh ip": "partnership",
    "worke rs": "workers",
    "delegated aut": "delegated authority",
    "Bure au": "Bureau",
    "the reof": "thereof",
    "fro m": "from",
    "this Ac ": "this Act ",
    "Labour Wel fare": "Labour Welfare",
    "dign ity": "dignity",
    "authori zed": "authorized",
    "Five Lakh taka": "Five Lakh taka",
    "hin four": "within four",
    "thi rty": "thirty",
    "Banglad eshi": "Bangladeshi",
    "home coun try": "home country",
    "the ori ginal": "the original",
    "doc uments": "documents",
    "pla ces": "places",
    "for col lecting": "for collecting",
    "for ov erseas": "for overseas",
    "offe red": "offered",
    "assistance offe red": "assistance offered",
    "rel evant": "relevant",
    "aut hority": "authority",
    "num ber": "number",
    "electronic card": "electronic card",
    "registration num ber": "registration number",
    "sp onsor": "sponsor",
    "co nvicted": "convicted",
    "the sp ecified": "the specified",
    "the ef fect": "the effect",
    "functions rel ating": "functions relating",
    "expanding re ach": "expanding reach",
    "with nam es": "with names",
    "nam es": "names",
    "appoi nt": "appoint",
    "compleme nt": "complement",
    "impress ion": "impression",
}


@lru_cache(maxsize=1)
def _wordlist() -> set[str]:
    """Optional dictionary used to repair mid-word PDF breaks.

    Not a hard dependency: if no system word list exists, the repair falls back to
    the document's own vocabulary, which is weaker but never wrong (a merge only
    happens when the merged form is a word that occurs in the same document).
    """
    for path in (Path("/usr/share/dict/american-english"), Path("/usr/share/dict/words")):
        try:
            if path.exists():
                words = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                return {w.strip().lower() for w in words if w.strip() and w.strip().isalpha()}
        except OSError:
            continue
    return set()


# Short function words. Needed because system dictionaries are inconsistent about
# them (the Debian american-english list used during development has "an" but not
# "and"), and without them a break like "an d" can never be repaired.
_COMMON_WORDS = frozenset(
    """a am an and are as at be been but by can could did do does for from had has have
    he her him his how i if in into is it its me may might must my no nor not of on
    onto or our out shall should so some such than that the their them then there these
    they this those to under up upon us was we were what when where which who whom why
    will with would you your""".split()
)


def _is_known(word: str) -> bool:
    lowered = word.lower()
    if lowered in _COMMON_WORDS:
        return True
    wordlist = _wordlist()
    return bool(wordlist) and lowered in wordlist


def _merge_pair(left: str, right: str, vocabulary: set[str]) -> str | None:
    """Return the merged token when ``left`` + ``right`` is one broken word.

    Guards that keep real two-word phrases intact:
    * the merged form must be a real word (document vocabulary or dictionary);
    * the right-hand piece must not itself be a word, so "in to" / "any one" / "may be"
      are never glued;
    * a longer right-hand piece is only attached when the left piece is not a word
      either ("accommo" + "dation"), which is exactly the mid-word break signature.
    """
    if not left or not right:
        return None
    bare_right = right.strip(",.;:()[]")
    if not (0 < len(bare_right) <= 8) or not bare_right.isalpha() or not bare_right.islower():
        return None
    if not left.isalpha() or len(left) < 2:
        return None

    candidate = (left + bare_right).lower()
    is_word = candidate in vocabulary or _is_known(candidate)
    if not is_word:
        return None

    if len(bare_right) <= 2:
        # A one/two-letter piece is a fragment unless it is a real function word,
        # so "an d" -> "and" but "in to" stays two words.
        return None if bare_right in _COMMON_WORDS else left + right

    if _is_known(bare_right) or _is_known(left):
        return None
    return left + right


def _repair_words(text: str, vocabulary: set[str]) -> str:
    words = text.split(" ")
    out: list[str] = []
    index = 0
    while index < len(words):
        merged = None
        if index + 1 < len(words):
            merged = _merge_pair(words[index], words[index + 1], vocabulary)
        if merged is not None:
            out.append(merged)
            index += 2
            continue
        out.append(words[index])
        index += 1
    return " ".join(out)


def normalise_extracted_text(text: str) -> str:
    """Clean up PDF extraction artefacts.

    PDF extraction breaks words in two ways: at a line end inside a word
    ("appropr\\niate") and mid-line with a stray space ("vic tims"). Both are repaired
    with the same self-checking test in :func:`_merge_pair`; ``_FIXUPS`` catches the
    handful this cannot decide.
    """
    if not text:
        return ""
    text = re.sub(r"\n?===PAGE===\n?", "\n", text)
    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip() and not re.fullmatch(r"\s*\d{1,3}\s*", line)
    ]

    vocabulary = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", " ".join(lines))}

    # 1. Stitch words broken across a line boundary.
    stitched: list[str] = []
    for line in lines:
        if stitched:
            tail = re.search(r"([A-Za-z]{2,})$", stitched[-1])
            head = re.match(r"([a-z]{1,8})\b", line)
            if tail and head and _merge_pair(tail.group(1), head.group(1), vocabulary):
                stitched[-1] = stitched[-1] + line
                continue
        stitched.append(line)

    # Lines are kept (not collapsed) so that consumers which need section structure
    # - scripts/build_index.py - still see one section per line start.
    joined = re.sub(r"[ \t]+", " ", "\n".join(stitched))
    joined = re.sub(r"[ \t]+([,.;:])", r"\1", joined)
    joined = re.sub(r"[\ue000-\uf8ff]", " ", joined)

    # 2. Repair mid-line breaks ("conclud ed", "compl aint,").
    joined = _repair_words(joined, vocabulary)

    for broken, fixed in _FIXUPS.items():
        joined = joined.replace(broken, fixed)
    return re.sub(r" {2,}", " ", joined).strip()
