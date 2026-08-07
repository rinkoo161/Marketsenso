"""PDF text extraction — used ONLY when the RSS/API metadata is too thin
to classify (the metadata-first design: ~90% of filings never get here).

pdfplumber first; OCR (pytesseract) as the scan fallback IF the tesseract
binary exists — on this machine it currently does not, so scans yield
None and the classifier proceeds on metadata with reduced confidence
rather than failing. Install `brew install tesseract` to enable OCR.

Extraction is cached next to the document (<sha>.txt in the same content-
addressed bucket) so a PDF is parsed at most once ever.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from marketsense.core.logging import get_logger
from marketsense.db.models import Document, Filing

log = get_logger("a2.extract")

MAX_PAGES = 4          # classification needs the opening pages, not annexures
MAX_CHARS = 6000

_HAS_TESSERACT = shutil.which("tesseract") is not None


def _pdf_text(path: Path) -> str | None:
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber_missing")
        return None
    try:
        with pdfplumber.open(path) as pdf:
            parts = []
            for page in pdf.pages[:MAX_PAGES]:
                parts.append(page.extract_text() or "")
            text = "\n".join(parts).strip()
    except Exception as e:
        log.warning("pdf_parse_failed", path=str(path), error=str(e)[:120])
        return None
    if len(text) >= 200:
        return text[:MAX_CHARS]
    # near-empty text layer → scanned document → OCR if available
    if not _HAS_TESSERACT:
        log.info("scan_no_ocr", path=path.name)
        return text or None
    try:
        import pytesseract
        with pdfplumber.open(path) as pdf:
            parts = []
            for page in pdf.pages[:2]:  # OCR is slow; 2 pages is the budget
                img = page.to_image(resolution=200).original
                parts.append(pytesseract.image_to_string(img))
        return "\n".join(parts).strip()[:MAX_CHARS] or None
    except Exception as e:
        log.warning("ocr_failed", path=str(path), error=str(e)[:120])
        return text or None


def text_for_filing(db: Session, filing: Filing) -> str | None:
    """Extracted text for a filing's fetched PDF, cached as <sha>.txt.
    None when there is no fetched PDF or extraction found nothing."""
    doc = db.scalar(
        select(Document).where(
            Document.filing_id == filing.id,
            Document.fetch_status == "fetched",
            Document.local_path.isnot(None),
        )
    )
    if doc is None or not doc.local_path.lower().endswith(".pdf"):
        return None
    path = Path(doc.local_path)
    if not path.exists():
        return None

    cache = path.with_suffix(".txt")
    if cache.exists():
        return cache.read_text(errors="replace") or None

    text = _pdf_text(path)
    cache.write_text(text or "")  # empty file = "tried, nothing" (negative cache)
    return text
