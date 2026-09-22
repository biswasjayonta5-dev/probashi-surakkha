#!/usr/bin/env python3
"""Turn the Act PDF text into one JSON chunk per section.

Run:  python scripts/build_index.py

Output: backend/data/act_chunks/section_NN.json

Each chunk carries the section number (so answers can cite "ধারা ২২"), its chapter,
and curated Bangla keywords. The keywords exist because the official Act text is
English while users ask in Bangla - see app/rag/index.py for why that matters and
what the real fix is.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.contracts.docio import normalise_extracted_text  # noqa: E402

RAW = ROOT / "backend" / "data" / "raw" / "act2013.txt"
OUT_DIR = ROOT / "backend" / "data" / "act_chunks"

OFFICIAL_SOURCE_URL = (
    "http://legislativediv.portal.gov.bd/sites/default/files/files/legislativediv.portal.gov.bd/"
    "page/64379df1_f98c_47ff_b9e6_cbcabadd8ece/27.Overseas%20Employment%20and%20Emigration%20Act,%202013.pdf"
)
RETRIEVED_FROM = (
    "http://asianparliamentarians.mfasia.org/wp-content/uploads/2017/01/"
    "bangladesh_overseas_empl_migrants_act2013-_eng.pdf"
)

# Curated Bangla keywords per section. Hand-written, deliberately short: they are the
# bridge between a Bangla question and English statute text.
KEYWORDS_BN: dict[int, list[str]] = {
    1: ["আইনের নাম", "কার্যকর"],
    2: ["সংজ্ঞা", "প্রতারণা", "দালাল", "নিয়োগ এজেন্সি", "লাইসেন্স", "অভিবাসন", "প্রবাসী"],
    3: ["নিয়ন্ত্রণ", "সরকার", "বিএমইটি", "নিয়োগ"],
    4: ["অভিবাসনের কাগজ", "ভিসা", "নিয়োগপত্র", "ওয়ার্ক পারমিট"],
    5: ["পর্যটক", "ছাত্র", "প্রশিক্ষণার্থী", "ট্যুরিস্ট ভিসা", "কাজের ভিসা নয়"],
    6: ["সমতা", "বৈষম্য", "নারী", "ধর্ম"],
    7: ["যাওয়ার বন্দর", "নির্ধারিত স্থান", "বিমানবন্দর"],
    8: ["নিষেধাজ্ঞা", "দেশ বন্ধ", "নিরাপত্তা"],
    9: ["লাইসেন্স", "আরএল নম্বর", "নিবন্ধন", "অনুমোদন", "জামানত"],
    10: ["যোগ্যতা", "লাইসেন্স পাওয়ার শর্ত", "অপরাধী", "কোম্পানি"],
    11: ["মেয়াদ", "নবায়ন", "তিন বছর"],
    12: ["লাইসেন্স বাতিল", "স্থগিত", "শর্ত ভঙ্গ", "আপিল"],
    13: ["প্রত্যাহার", "গেজেট"],
    14: ["শাখা অফিস", "অনুমতি"],
    15: ["এজেন্সির দায়িত্ব", "বেতন দেওয়া", "নিবন্ধন করানো", "ক্লিয়ারেন্স"],
    16: ["এজেন্সির গ্রেড", "শ্রেণি", "বাছাই"],
    17: ["লাইসেন্স হস্তান্তর", "ঠিকানা পরিবর্তন", "ওয়ারিশ"],
    18: ["জামানত বাজেয়াপ্ত", "ক্ষতিপূরণ", "ফেরত পাঠানো"],
    19: ["নিবন্ধন", "তালিকাভুক্তি", "কম্পিউটারাইজড", "ট্রেড"],
    20: ["মাইগ্রেশন ক্লিয়ারেন্স", "স্মার্ট কার্ড", "পাসপোর্ট সিল"],
    21: ["খরচের সীমা", "ফি", "সরকার নির্ধারিত ফি", "অভিবাসন খরচ"],
    22: ["চুক্তি", "বেতন", "থাকার ব্যবস্থা", "চাকরির মেয়াদ", "মৃত্যুতে ক্ষতিপূরণ", "ফেরার টিকিট"],
    23: ["শ্রম কল্যাণ উইং", "দূতাবাস"],
    24: ["তদারকি", "পরিদর্শন", "বার্ষিক রিপোর্ট"],
    25: ["সমঝোতা স্মারক", "দ্বিপাক্ষিক চুক্তি"],
    26: ["তথ্য জানার অধিকার", "চুক্তির শর্ত জানা"],
    27: ["আইনি সহায়তা", "লিগ্যাল এইড", "সহায়তা"],
    28: ["ক্ষতিপূরণের মামলা", "দেওয়ানি মামলা"],
    29: ["দেশে ফেরা", "আটকে পড়া", "ফেরত পাঠানো", "দূতাবাসের সহায়তা"],
    30: ["ঋণ", "সঞ্চয়", "কল্যাণ তহবিল", "প্রবাসী কল্যাণ ব্যাংক"],
    31: ["শাস্তি", "জেল", "জরিমানা", "পাসপোর্ট আটকে রাখা", "মিথ্যা প্রতিশ্রুতি", "অবৈধ ফি"],
    32: ["বিজ্ঞাপন", "অনুমোদন ছাড়া বিজ্ঞাপন"],
    33: ["ভিসা বাণিজ্য", "ডিমান্ড নোট", "ভিসা বিক্রি"],
    34: ["অবৈধ পথে যাওয়া", "নির্ধারিত স্থান ছাড়া", "কাস্টমস"],
    35: ["অন্যান্য অপরাধ", "অন্যান্য শাস্তি"],
    36: ["সহযোগিতা", "প্ররোচনা", "উদ্দেশ্যপ্রণোদিত"],
    37: ["কোম্পানির অপরাধ", "পরিচালক", "দায়"],
    38: ["বিচার", "ম্যাজিস্ট্রেট", "সময়সীমা", "চার মাস"],
    39: ["গ্রেপ্তারযোগ্য", "জামিনযোগ্য", "আপসযোগ্য"],
    40: ["ভ্রাম্যমাণ আদালত", "মোবাইল কোর্ট"],
    41: ["অভিযোগ", "তদন্ত", "৩০ কর্মদিবস", "সালিশ", "নিষ্পত্তি"],
    42: ["পরিদর্শন", "তল্লাশি", "যানবাহন"],
    43: ["টাকা উদ্ধার", "অবৈধ চার্জ", "আদায়"],
    44: ["ক্ষমতা অর্পণ", "শ্রম অ্যাটাশে", "প্রতিনিধি"],
    45: ["অসুবিধা দূরীকরণ"],
    46: ["অন্যান্য আইন", "পাসপোর্ট আইন", "মানব পাচার"],
    47: ["বিধিমালা", "নিয়ম প্রণয়ন"],
    48: ["বাংলা মূল", "ইংরেজি অনুবাদ", "সরকারি অনুবাদ"],
    49: ["পুরাতন অধ্যাদেশ বিলুপ্ত", "স্থগিতাদেশ"],
}

SECTION_START_RE = re.compile(r"(?m)^[ \t]*(\d{1,2})\.[ \t]+(?=[A-Z])")
CHAPTER_RE = re.compile(r"(?m)^[ \t]*CHAPTER\s+([IVXL]+)[ \t]*\n[ \t]*([^\n]{3,90})")
PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")


def split_title(chunk: str) -> tuple[str, int]:
    """Split "Title.— rest" or "Title.\\nrest"; some sections have no dash at all.

    Section 27 in the source PDF ("27. Legal aid. Migrant workers shall...") lost its
    dash, so the fallback cuts the title at the first sentence end instead of taking
    the whole line.
    """
    with_dash = re.match(r"(.{0,200}?)(?:\.\s*—|—)", chunk, re.S)
    if with_dash:
        return with_dash.group(1).strip().rstrip("."), with_dash.end()
    sentence = re.match(r"(.{2,120}?\.)(?=\s+[A-Z])", chunk, re.S)
    if sentence:
        return sentence.group(1).strip().rstrip("."), sentence.end()
    first_line = re.match(r"([^\n]{2,200})", chunk)
    if first_line:
        return first_line.group(1).strip().rstrip("."), first_line.end()
    return "", 0


def parse_sections(text: str) -> list[dict]:
    """Position-based parse with a sequential-number guard.

    A section number only counts when it is exactly one more than the previous one,
    which removes false starts from cross-references and numbered sub-clauses.
    """
    starts: list[tuple[int, int]] = []
    expected = 1
    for match in SECTION_START_RE.finditer(text):
        number = int(match.group(1))
        if number != expected:
            continue
        starts.append((match.start(), number))
        expected += 1

    chapters: list[tuple[int, str, str]] = []
    for match in CHAPTER_RE.finditer(text):
        body_pos = match.start()
        chapters.append((body_pos, match.group(1), match.group(2).strip()))

    sections: list[dict] = []
    for index, (position, number) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        slug_end = text.index(" ", position) + 1
        title = " ".join(split_title(text[slug_end:end])[0].split())
        body = text[slug_end + split_title(text[slug_end:end])[1] : end]
        # A chapter heading sits between two sections; it belongs to neither body.
        body = re.sub(r"(?m)^\s*CHAPTER\s+[IVXL]+\s*\n[^\n]*\s*$", "", body)
        chapter_roman, chapter_title = "", ""
        for chapter_pos, roman, title_text in chapters:
            if chapter_pos < position:
                chapter_roman, chapter_title = roman, title_text
            else:
                break
        sections.append(
            {
                "number": number,
                "title": title,
                "chapter_roman": chapter_roman,
                "chapter_title": chapter_title,
                "text": f"{title}.— {' '.join(body.split())}".strip(),
            }
        )
    return sections


def main() -> int:
    if not RAW.exists():
        sys.stderr.write(
            f"missing {RAW}\n"
            "Download the Act first:\n"
            "  curl -sSL -o backend/data/raw/act2013.pdf "
            f"'{RETRIEVED_FROM}'\n"
            "  then extract its text to backend/data/raw/act2013.txt\n"
        )
        return 2

    text = normalise_extracted_text(RAW.read_text(encoding="utf-8"))
    # The PDF uses private-use glyphs where an em dash was dropped in extraction.
    text = PRIVATE_USE_RE.sub(" ", text)
    sections = parse_sections(text)
    numbers = [section["number"] for section in sections]
    if len(sections) < 45:
        sys.stderr.write(
            f"only {len(sections)} sections parsed (expected 49): {numbers}\n"
            "The source text format probably changed - fix the parser before indexing.\n"
        )
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUT_DIR.glob("section_*.json"):
        old.unlink()

    total_chars = 0
    for section in sections:
        payload = {
            "source_id": f"act:{section['number']}",
            "section": str(section["number"]),
            "title": section["title"],
            "chapter": f"Chapter {section['chapter_roman']} — {section['chapter_title']}".strip(" —"),
            "text": section["text"],
            "keywords_bn": KEYWORDS_BN.get(section["number"], []),
            "kind": "act",
            "source": "Overseas Employment and Migrants Act 2013 (official English translation)",
            "source_url": RETRIEVED_FROM,
            "official_source_url": OFFICIAL_SOURCE_URL,
            "verified": False,
            "verify_note": (
                "Text extracted from a third-party mirror of the official English translation. "
                "Compare with the Legislative Division PDF and, where the Bangla and English "
                "texts differ, section 48(2) makes the Bangla text authoritative."
            ),
        }
        path = OUT_DIR / f"section_{section['number']:02d}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        total_chars += len(section["text"])

    print(f"parsed {len(sections)} sections, {total_chars:,} chars -> {OUT_DIR}")
    missing_keywords = [s["number"] for s in sections if not KEYWORDS_BN.get(s["number"])]
    if missing_keywords:
        print(f"note: no Bangla keywords for sections {missing_keywords} (English-only retrieval)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
