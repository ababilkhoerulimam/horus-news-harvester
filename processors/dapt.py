"""
DAPT Corpus Preparation Module for NusaBERT MLM.
Prepares, cleans, and standardizes Indonesian news articles into a
high-quality text corpus (.txt) for Masked Language Modeling (MLM) pre-training.
Outputs clean document-separated format for Hugging Face run_mlm.py.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Union

logger = logging.getLogger("processors.dapt")

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

    if remove_boilerplates:
        for pat in COMPILED_BOILERPLATE:
            text = pat.sub(" ", text)

    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    text = re.sub(r"\[(?:foto|gambar|video|caption)[^\]]*\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\((?:foto|gambar|video|antara|reuters|afp)[^)]*\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"@[\w_]+", "", text)
    text = re.sub(r"#[\w_]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def stream_articles_from_jsonl(file_path: Path) -> Generator[Dict[str, Any], None, None]:
    """Memory-efficient streaming generator for reading large JSONL article archives."""
    if not file_path.exists():
        logger.warning(f"File not found: {file_path}")
        return

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f, start=1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                record = json.loads(line_str)
                yield record
            except json.JSONDecodeError:
                continue


def build_dapt_corpus(
    input_paths: Optional[Union[List[Union[Path, str]], Path, str]] = None,
    output_path: Optional[Union[Path, str]] = None,
    min_word_count: int = 50,
    dedup_content: bool = True,
    doc_separator: str = "\n\n",
    input_path: Optional[Union[Path, str]] = None,
    min_words: Optional[int] = None,
    doc_format: str = "document",
) -> Dict[str, Any]:
    """Compiles and sanitizes raw JSONL article files into a unified text corpus for MLM."""
    # Resolve input paths (support single or plural parameter)
    resolved_inputs: List[Path] = []
    raw_in = input_paths if input_paths is not None else input_path
    if raw_in is not None:
        if isinstance(raw_in, (list, tuple)):
            resolved_inputs = [Path(p) for p in raw_in]
        else:
            resolved_inputs = [Path(raw_in)]

    out_p = Path(output_path) if output_path else Path("data/processed/corpus.txt")
    out_p.parent.mkdir(parents=True, exist_ok=True)

    min_words_limit = min_words if min_words is not None else min_word_count
    sep = "\n" if doc_format == "line" else doc_separator

    seen_hashes: Set[int] = set()

    total_read = 0
    total_written = 0
    total_words = 0
    total_tokens_est = 0
    skipped_short = 0
    skipped_duplicate = 0

    with open(out_p, "w", encoding="utf-8") as out_f:
        for p in resolved_inputs:
            for article in stream_articles_from_jsonl(p):
                total_read += 1
                raw_body = article.get("content") or article.get("raw_text") or ""
                clean_body = sanitize_dapt_text(raw_body)
                if doc_format == "line":
                    clean_body = re.sub(r"\s+", " ", clean_body).strip()

                words = clean_body.split()

                if len(words) < min_words_limit:
                    skipped_short += 1
                    continue

                if dedup_content:
                    chash = hash(" ".join(words[:40]))
                    if chash in seen_hashes:
                        skipped_duplicate += 1
                        continue
                    seen_hashes.add(chash)

                out_f.write(clean_body + sep)
                total_written += 1
                total_words += len(words)
                total_tokens_est += int(len(words) * 1.3)

    return {
        "status": "success",
        "output_path": str(out_p),
        "total_read": total_read,
        "total_written": total_written,
        "articles_retained": total_written,
        "skipped_short": skipped_short,
        "discarded_short": skipped_short,
        "skipped_duplicate": skipped_duplicate,
        "discarded_duplicate": skipped_duplicate,
        "total_words": total_words,
        "estimated_tokens": total_tokens_est,
    }


def format_corpus_summary(stats: Dict[str, Any]) -> str:
    """Renders human-readable summary table of DAPT corpus generation."""
    lines = [
        "      NusaBERT DAPT Sports Corpus Preparation Report        ",
        f" Target File        : {stats.get('output_path')}",
        f" Articles Processed : {stats.get('total_read', 0):,}",
        f" Articles Accepted  : {stats.get('articles_retained', stats.get('total_written', 0)):,}",
        f" Discarded (< Min Len): {stats.get('discarded_short', stats.get('skipped_short', 0)):,}",
        f" Discarded (Duplicate): {stats.get('discarded_duplicate', stats.get('skipped_duplicate', 0)):,}",
        f" Total Words        : {stats.get('total_words', 0):,} words",
        f" Estimated Tokens   : ~{stats.get('estimated_tokens', 0):,} tokens",
    ]
    return "\n".join(lines)
