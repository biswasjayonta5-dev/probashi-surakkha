"""Retrieval over the Act + rule cards.

Why BM25 and not embeddings (yet): the corpus is ~50 sections, the whole thing fits
in a few thousand tokens, and BM25 is deterministic, free and auditable - which
matters for a tool whose answers carry legal citations. The plan's target is a
multilingual embedding model (BGE-M3 / multilingual-E5); `EmbeddingRetriever` below
is the seam where that drops in later, and the eval harness measures whatever is
active so the swap can be judged on numbers.

Cross-lingual problem: the official Act text is English but users ask in Bangla.
Solved for now by (a) indexing curated Bangla keywords per section
(`scripts/build_index.py`) and (b) writing the rule cards bilingually. That is a
deliberate, documented stopgap - not a claim that Bangla retrieval is solved.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ..config import settings

K1 = 1.2
B = 0.75
KEYWORD_REPEAT = 3  # weight given to curated Bangla keywords

# Minimum evidence for a grounded answer. Chosen from the eval run, not guesswork:
# see README "Retrieval thresholds".
MIN_BEST_SCORE = 1.0
MIN_COVERAGE = 0.34

_TOKEN_RE = re.compile(r"[\u0980-\u09FF]+|[a-zA-Z][a-zA-Z\-]+|\d+(?:\.\d+)?")
_STOP_EN = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "is", "are", "be",
    "with", "by", "as", "at", "that", "this", "it", "from", "any", "his", "her",
    "their", "was", "were", "will", "shall", "may", "not", "no", "if", "under",
}
_STOP_BN = {
    "এবং", "বা", "এর", "একটি", "আমি", "আমার", "কি", "কী", "কিভাবে", "কোথায়", "কত",
    "কেন", "হয়", "হবে", "করা", "করে", "করতে", "থেকে", "জন্য", "সাথে", "না", "নেই",
    "আছে", "ছিল", "একটা", "এই", "সেই", "যে", "যদি", "তবে", "আর",
}
# Conservative suffix stripping so "অভিযোগের" matches "অভিযোগ". Stems are applied
# to both the corpus and the query, so an occasional over-strip costs far less than
# a missed match on a case marker ("থাকতে" / "থাকার" vs "থাক").
_BN_SUFFIXES = (
    "দেরকে", "দের", "গুলোর", "গুলো", "গুলি", "েরা", "ের", "ার", "ির", "তে", "টি", "টা", "কে", "য়",
)


def stem(token: str) -> str:
    if re.fullmatch(r"[\u0980-\u09FF]+", token):
        for suffix in _BN_SUFFIXES:
            if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                return token[: -len(suffix)]
        # Bare genitive "র" only on longer words, where it is nearly always a case
        # marker rather than part of the stem.
        if token.endswith("র") and len(token) >= 5:
            return token[:-1]
        return token
    lowered = token.lower()
    for suffix in ("ies", "ing", "ed", "es", "s"):
        if lowered.endswith(suffix) and len(lowered) - len(suffix) >= 4:
            return lowered[: -len(suffix)]
    return lowered


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        lowered = raw.lower()
        if lowered in _STOP_EN or lowered in _STOP_BN:
            continue
        if len(lowered) < 2:
            continue
        tokens.append(stem(lowered))
    return tokens


@dataclass
class Chunk:
    source_id: str
    title: str
    text: str
    section: str | None = None
    chapter: str | None = None
    source_url: str | None = None
    keywords_bn: list[str] = field(default_factory=list)
    kind: str = "act"  # "act" | "rule_card" | "procedure"
    verified: bool = False

    def snippet(self, limit: int = 420) -> str:
        text = re.sub(r"\s+", " ", self.text).strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# Curated Bangla keywords per rule card. The heading text alone is not enough: the
# complaint card must win for "কোথায় অভিযোগ করব", not the penalties card that happens
# to mention fraud more often.
_CARD_KEYWORDS_BN: dict[str, list[str]] = {
    "CARD-01": ["লাইসেন্স", "এজেন্সি", "আরএল", "অনুমোদিত", "নিবন্ধিত"],
    "CARD-02": ["দালাল", "সাব-এজেন্ট", "রসিদ", "মধ্যস্বত্বভোগী"],
    "CARD-03": ["ফি", "সীমা", "খরচ", "টাকা", "সরকার নির্ধারিত"],
    "CARD-04": ["চুক্তি", "বেতন", "থাকার", "মেয়াদ", "ক্ষতিপূরণ", "টিকিট", "শর্ত"],
    "CARD-05": ["তথ্য", "জানার অধিকার", "চুক্তির শর্ত"],
    "CARD-06": ["ভিসা", "পর্যটক", "ট্যুরিস্ট", "ভিজিট", "কাজের ভিসা"],
    "CARD-07": ["নিবন্ধন", "ক্লিয়ারেন্স", "স্মার্ট কার্ড", "রেজিস্ট্রেশন"],
    "CARD-08": ["অভিযোগ", "নালিশ", "কোথায়", "প্রতারণা", "রসিদ", "বিএমইটি", "হেল্পলাইন"],
    "CARD-09": ["আইনি সহায়তা", "মামলা", "ফেরা", "দূতাবাস", "অধিকার"],
    "CARD-10": ["শাস্তি", "জেল", "জরিমানা", "অপরাধ", "পাসপোর্ট আটকে"],
}


def _card_chunks() -> list[Chunk]:
    """Split rule_cards.md into one chunk per `## CARD-xx` block."""
    path = settings.rule_cards_md
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=##\s)", raw)
    chunks: list[Chunk] = []
    for block in blocks:
        heading = re.match(r"##\s+([^\n(]+)(\(([^)]+)\))?", block.strip())
        if not heading:
            continue
        if not heading.group(1).strip().upper().startswith("CARD-"):
            continue  # preamble / notes are prose, not citable rules
        label = heading.group(1).strip()
        card_id = label.split("—")[0].strip() or label
        display_title = label.split("—", 1)[1].strip() if "—" in label else label
        bangla = (heading.group(3) or "").strip()
        body = block.strip()
        keywords_bn = list(
            _CARD_KEYWORDS_BN.get(card_id.upper())
            or [w for w in re.findall(r"[\u0980-\u09FF]{3,}", bangla)]
        )
        chunks.append(
            Chunk(
                source_id=f"card:{card_id}",
                title=display_title,
                section=None,
                text=re.sub(r"\s+\n", "\n", body),
                keywords_bn=keywords_bn,
                kind="rule_card",
                verified=False,
            )
        )
    return chunks


def _act_chunks() -> list[Chunk]:
    directory = settings.act_chunks_dir
    if not directory.exists():
        return []
    chunks: list[Chunk] = []
    for path in sorted(directory.glob("section_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        chunks.append(
            Chunk(
                source_id=data["source_id"],
                title=data.get("title", ""),
                section=data.get("section"),
                chapter=data.get("chapter"),
                text=data.get("text", ""),
                source_url=data.get("source_url"),
                keywords_bn=data.get("keywords_bn", []),
                kind="act",
                verified=bool(data.get("verified", False)),
            )
        )
    return chunks


@lru_cache(maxsize=1)
def _corpus_signature() -> tuple:
    directory = settings.act_chunks_dir
    act_mtime = max((p.stat().st_mtime for p in directory.glob("section_*.json")), default=0.0)
    cards_mtime = settings.rule_cards_md.stat().st_mtime if settings.rule_cards_md.exists() else 0.0
    return (act_mtime, cards_mtime)


@lru_cache(maxsize=1)
def _index(signature: tuple) -> dict:
    chunks = _act_chunks() + _card_chunks()
    token_counts: list[Counter] = []
    doc_freq: Counter = Counter()
    for chunk in chunks:
        bag = tokenize(chunk.text)
        for keyword in chunk.keywords_bn:
            bag.extend(tokenize(keyword) * KEYWORD_REPEAT)
        counter = Counter(bag)
        token_counts.append(counter)
        for token in counter:
            doc_freq[token] += 1
    total_docs = max(len(chunks), 1)
    avg_len = (sum(sum(c.values()) for c in token_counts) / total_docs) or 1.0
    return {
        "chunks": chunks,
        "token_counts": token_counts,
        "doc_freq": doc_freq,
        "avg_len": avg_len,
        "total_docs": total_docs,
    }


def load_index() -> dict:
    return _index(_corpus_signature())


def corpus_size() -> int:
    return len(load_index()["chunks"])


def retrieve(query: str, k: int = 4) -> list[tuple[Chunk, float, float]]:
    """Return ``(chunk, bm25_score, coverage)`` for the top ``k`` chunks."""
    index = load_index()
    chunks: list[Chunk] = index["chunks"]
    if not chunks:
        return []

    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    scores: list[float] = []
    coverages: list[float] = []
    for position, counter in enumerate(index["token_counts"]):
        length = sum(counter.values()) or 1
        score = 0.0
        matched = 0
        for token in query_tokens:
            freq = counter.get(token, 0)
            if not freq:
                continue
            matched += 1
            df = index["doc_freq"].get(token, 0) or 0.5
            idf = math.log(1 + (index["total_docs"] - df + 0.5) / (df + 0.5))
            score += idf * (freq * (K1 + 1)) / (freq + K1 * (1 - B + B * length / index["avg_len"]))
        scores.append(score)
        coverages.append(matched / len(query_tokens))

    ranked = sorted(
        ((chunks[i], scores[i], coverages[i]) for i in range(len(chunks))),
        key=lambda item: item[1],
        reverse=True,
    )
    return [item for item in ranked[:k] if item[1] > 0]


def is_grounded(hits: list[tuple[Chunk, float, float]]) -> bool:
    if not hits:
        return False
    _, best, coverage = hits[0]
    return best >= MIN_BEST_SCORE and coverage >= MIN_COVERAGE


class EmbeddingRetriever:  # pragma: no cover - deliberate placeholder
    """Seam for the multilingual embedding upgrade named in the plan.

    Not implemented in the MVP on purpose: the corpus is small enough for BM25, and
    an unvalidated embedding swap would weaken the citation guarantee. Implementing
    this requires (a) a model that handles Bangla (BGE-M3 or multilingual-E5),
    (b) a Bangla retrieval eval set, and (c) a decision recorded in the README.
    """

    def __init__(self, *_: object, **__: object) -> None:
        raise NotImplementedError(
            "Embedding retrieval is a documented post-MVP upgrade (plan §5). "
            "Use the BM25 retriever or implement this behind the eval harness."
        )
