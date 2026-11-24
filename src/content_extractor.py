"""Privacy-safe short text preview extraction for Tier 2 AI classification."""

from __future__ import annotations

import json
import re
from pathlib import Path

from guardrails import has_sensitive_content, is_sensitive_file

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".py", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".tex", ".sql", ".c", ".cpp", ".h", ".hpp", ".java",
    ".js", ".ts", ".rs", ".go", ".css",
}
MAX_TEXT_BYTES = 4096
MAX_ICS_BYTES = 32768
MAX_PDF_PAGES = 2
MAX_DOCX_PARAGRAPHS = 25
MAX_EXTRACT_FILE_BYTES = 60 * 1024 * 1024  # skip parsing huge documents

ICS_KEEP_PROPS = ("SUMMARY", "LOCATION", "ORGANIZER", "DTSTART", "DESCRIPTION")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _read_pdf(path: Path) -> str:
    import logging
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(str(path), strict=False)
    if getattr(reader, "is_encrypted", False):
        return ""
    chunks: list[str] = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks)


def _read_docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    paras = [p.text for p in document.paragraphs[:MAX_DOCX_PARAGRAPHS] if p.text]
    return "\n".join(paras)


def _read_ipynb(path: Path) -> str:
    raw = path.read_bytes()[: MAX_TEXT_BYTES * 4].decode("utf-8", errors="replace")
    try:
        nb = json.loads(raw)
        cells = nb.get("cells", [])[:6]
        return "\n".join("".join(c.get("source", [])) for c in cells)
    except Exception:
        return raw[:MAX_TEXT_BYTES]


def _read_text(path: Path) -> str:
    with open(path, "rb") as f:
        return f.read(MAX_TEXT_BYTES).decode("utf-8", errors="replace")


def _read_ics(path: Path) -> str:
    """Unfold RFC 5545 calendar lines and extract key event fields past VTIMEZONE headers."""
    with open(path, "rb") as f:
        raw = f.read(MAX_ICS_BYTES).decode("utf-8", errors="replace")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    unfolded = re.sub(r"\n[ \t]", "", normalized)

    extracted: dict[str, str] = {}
    for line in unfolded.splitlines():
        if ":" not in line:
            continue
        left, val = line.split(":", 1)
        prop = left.split(";", 1)[0].strip().upper()
        if prop not in ICS_KEEP_PROPS or prop in extracted:
            continue
        cleaned = (
            val.replace("\\n", " ")
            .replace("\\N", " ")
            .replace("\\,", ",")
            .replace("\\;", ";")
            .strip()
        )
        if prop == "ORGANIZER":
            cn_match = re.search(r"(?:^|;)CN=([^;:]+)", left, re.IGNORECASE)
            if cn_match:
                cn = cn_match.group(1).strip().strip('"')
                cleaned = f"{cn} ({cleaned})" if cleaned else cn
        if cleaned:
            extracted[prop] = cleaned[:250] if prop == "DESCRIPTION" else cleaned

    if extracted:
        return " | ".join(f"{k}: {extracted[k]}" for k in ICS_KEEP_PROPS if k in extracted)
    return raw[:MAX_TEXT_BYTES]


def extract_safe_preview(path: Path | str, max_chars: int = 400) -> tuple[str | None, bool, str]:
    """Return (preview_text, sensitive_detected, note).

    Sensitive filenames are NEVER opened. Extracted snippets containing secrets/PII
    are discarded and flagged so the caller routes the file to human review.
    """
    p = Path(path)
    sensitive, reason = is_sensitive_file(p)
    if sensitive:
        return None, True, f"sensitive_filename:{reason}"

    ext = p.suffix.lower()
    try:
        if not p.is_file():
            return None, False, "not_a_file"
        if p.stat().st_size > MAX_EXTRACT_FILE_BYTES:
            return None, False, "too_large_for_preview"

        if ext == ".pdf":
            text = _read_pdf(p)
        elif ext == ".docx":
            text = _read_docx(p)
        elif ext == ".ipynb":
            text = _read_ipynb(p)
        elif ext == ".ics":
            text = _read_ics(p)
        elif ext in TEXT_EXTENSIONS:
            text = _read_text(p)
        else:
            return None, False, "unsupported_type"
    except Exception as exc:  # corrupt / locked / encrypted documents
        return None, False, f"extract_error:{type(exc).__name__}"

    # Scan the full extracted window first so secrets just past the cut are still caught.
    if has_sensitive_content(text):
        return None, True, "sensitive_content"

    snippet = _normalize(text)[: max(0, int(max_chars))]
    if not snippet:
        return None, False, "empty_text"
    return snippet, False, "ok"
