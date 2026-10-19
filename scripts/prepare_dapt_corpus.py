"""
DAPT Corpus Preparation Module for NusaBERT MLM.
Prepares, cleans, and standardizes Indonesian football news articles into a
high-quality text corpus (.txt) for Masked Language Modeling (MLM) pre-training.
Outputs clean single-line or document-separated format for Hugging Face run_mlm.py.
"""

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

# Ensure repository root is on Python module search path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from processors.cleaner import clean_news_text

logger = logging.getLogger("prepare_dapt_corpus")

# Indonesian sports news noise and advertisement patterns to excise
BOILERPLATE_PATTERNS = [
    r"baca\s+juga\s*:?[^\n]+",
    r"simak\s+video\s*:?[^\n]+",
    r"tonton\s+video\s*:?[^\n]+",
    r"halaman\s+selanjutnya[^\n]*",
    r"halaman\s+\d+\s+dari\s+\d+[^\n]*",
    r"advertisement[^\n]*",
    r"scroll\s+to\s+continue\s+with\s+content[^\n]*",
    r"pilihan\s+editor\s*:?[^\n]+",
    r"berita\s+terkait\s*:?[^\n]+",
    r"ikuti\s+perkembangan\s+berita[^\n]+",
    r"ikuti\s+saluran\s+whatsapp[^\n]+",
    r"gabung\s+kompas\.com\s+plus[^\n]+",
    r"unduh\s+aplikasi[^\n]+",
    r"copyright\s+[^\n]+",
    r"hak\s+cipta\s+dilindungi[^\n]+",
]

COMPILED_BOILERPLATE = [re.compile(p, re.IGNORECASE) for p in BOILERPLATE_PATTERNS]


def sanitize_dapt_text(raw_text: str, remove_boilerplates: bool = True) -> str:
    """
    Sanitizes article body text for NLP Masked Language Modeling.
    Strips noise, ad markers, inline social links, and normalizes spacing.
    """
    if not raw_text:
        return ""

    text = raw_text

    # 1. Excise common editorial boilerplates
    if remove_boilerplates:
        for pat in COMPILED_BOILERPLATE:
            text = pat.sub(" ", text)

    # 2. Strip leftover URLs or image captions
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"\[(?:foto|gambar|video|caption)[^\]]*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\((?:foto|gambar|video|antara|reuters|afp)[^)]*\)", "", text, flags=re.IGNORECASE)

    # 3. Strip social media handles and hashtags that contaminate standard vocabulary
    # ponytail: remove raw @mentions to protect MLM token distributions, preserve Indonesian team names
    text = re.sub(r"@[\w_]+", "", text)

    # 4. Remove residual HTML tags if any leaked
    text = re.sub(r"<[^>]+>", " ", text)

    # 5. Normalize quotation marks and dashes to standard ASCII
    text = re.sub(r"[‘’‚‛]", "'", text)
    text = re.sub(r"[“”„‟]", '"', text)
    text = re.sub(r"[–—]", "-", text)
    text = re.sub(r"…", "...", text)

    # 6. Normalize whitespace: reduce runs of whitespace to a single space
    # but preserve paragraph sentence flow
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned_lines = []
    for line in lines:
        line_clean = re.sub(r"\s+", " ", line).strip()
        # Filter out very short fragment lines (< 15 chars) like metadata or photo credits
        if len(line_clean) >= 15:
            cleaned_lines.append(line_clean)

    return " ".join(cleaned_lines)


def stream_raw_articles(file_path: Path) -> Generator[Dict[str, Any], None, None]:
    """Yields parsed JSON records from a raw JSONL dataset."""
    if not file_path.exists():
        return

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                obj = json.loads(line_str)
                yield obj
            except Exception as e:
                logger.debug(f"JSON decode error at line {line_num} in {file_path}: {e}")
                continue


def build_dapt_corpus(
    input_path: str = "data/raw/news/football.jsonl",
    output_path: str = "data/processed/nusabert_football_dapt.txt",
    min_words: int = 50,
    doc_format: str = "line"
) -> Dict[str, Any]:
    """
    Builds clean text corpus from raw Indonesian football news JSONL.
    doc_format:
      - 'line': 1 article per line (separated by newline). Ideal for LineByLineTextDataset.
      - 'document': Articles separated by double newlines (\n\n). Ideal for chunked sequence packing.
    """
    in_file = Path(input_path)
    if not in_file.is_absolute():
        in_file = REPO_ROOT / input_path

    out_file = Path(output_path)
    if not out_file.is_absolute():
        out_file = REPO_ROOT / output_path

    out_file.parent.mkdir(parents=True, exist_ok=True)

    if not in_file.exists():
        return {
            "status": "error",
            "message": f"Input file not found: {in_file}",
            "articles_retained": 0,
        }

    seen_hashes: Set[str] = set()
    total_articles = 0
    retained_articles = 0
    discarded_short = 0
    discarded_duplicate = 0

    total_words = 0
    vocabulary_set: Set[str] = set()

    with open(out_file, "w", encoding="utf-8") as out_f:
        for record in stream_raw_articles(in_file):
            total_articles += 1
            raw_text = record.get("content") or record.get("text") or ""
            clean_text = sanitize_dapt_text(raw_text)

            words = clean_text.split()
            word_count = len(words)

            if word_count < min_words:
                discarded_short += 1
                continue

            # Deduplication based on normalized first 15 words signature
            signature = " ".join(words[:15]).lower()
            if signature in seen_hashes:
                discarded_duplicate += 1
                continue
            seen_hashes.add(signature)

            # Record stats
            retained_articles += 1
            total_words += word_count
            for w in words:
                vocabulary_set.add(w.lower())

            # Write formatted text
            if doc_format == "document":
                out_f.write(clean_text + "\n\n")
            else:
                # Default 'line': 1 single continuous line per document
                single_line = re.sub(r"\s+", " ", clean_text).strip()
                out_f.write(single_line + "\n")

    avg_words = round(total_words / retained_articles, 1) if retained_articles > 0 else 0
    # Estimated subword tokens for BERT/NusaBERT (~1.3x word count for Indonesian morphology)
    est_tokens = int(total_words * 1.3)

    stats = {
        "status": "success",
        "input_file": str(in_file),
        "output_file": str(out_file),
        "total_read": total_articles,
        "articles_retained": retained_articles,
        "discarded_short": discarded_short,
        "discarded_duplicate": discarded_duplicate,
        "total_words": total_words,
        "estimated_tokens": est_tokens,
        "unique_vocab_size": len(vocabulary_set),
        "avg_words_per_doc": avg_words,
        "doc_format": doc_format,
    }

    return stats


def format_corpus_summary(stats: Dict[str, Any]) -> str:
    """Formats corpus statistics as clean academic report for thesis documentation."""
    lines = [
        "===========================================================",
        "NusaBERT DAPT Sports Corpus Preparation Report",
        "===========================================================",
        f"Output Path:          {stats.get('output_file')}",
        f"Format:               {stats.get('doc_format')} per article",
        "-----------------------------------------------------------",
        f"Total Raw Records:    {stats.get('total_read', 0):,}",
        f"Articles Retained:    {stats.get('articles_retained', 0):,} ({round(stats.get('articles_retained', 0) / max(1, stats.get('total_read', 1)) * 100, 1)}%)",
        f"Discarded (Too Short):{stats.get('discarded_short', 0):,}",
        f"Discarded (Duplicate):{stats.get('discarded_duplicate', 0):,}",
        "-----------------------------------------------------------",
        f"Total Words:          {stats.get('total_words', 0):,}",
        f"Estimated Tokens:     ~{stats.get('estimated_tokens', 0):,} (NusaBERT ~1.3x)",
        f"Unique Vocabulary:    {stats.get('unique_vocab_size', 0):,} word forms",
        f"Avg Words / Article:  {stats.get('avg_words_per_doc', 0)} words",
        "===========================================================",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Prepare clean Indonesian sports news corpus for DAPT")
    parser.add_argument("--input", "-i", default="data/raw/news/football.jsonl", help="Input raw JSONL file")
    parser.add_argument("--output", "-o", default="data/processed/nusabert_football_dapt.txt", help="Output .txt file")
    parser.add_argument("--min-words", "-w", type=int, default=50, help="Minimum word count per article")
    parser.add_argument("--format", "-f", choices=["line", "document"], default="line", help="Document layout")
    args = parser.parse_args()

    res = build_dapt_corpus(
        input_path=args.input,
        output_path=args.output,
        min_words=args.min_words,
        doc_format=args.format
    )
    print(format_corpus_summary(res))
