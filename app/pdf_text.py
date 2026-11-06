"""Local PDF text extraction with page-level Windows OCR for image-only pages."""
import io
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading

MAX_SCAN_PAGES = 200
# PDFium is not thread-safe. Admit one page at a time before loading the original bytes.
preview_lock = threading.Lock()


def page_image(data, number):
    import pypdfium2
    with pypdfium2.PdfDocument(data) as document:
        page = document[number - 1]
        try:
            size = max(page.get_size())
            if not math.isfinite(size) or size <= 0:
                raise ValueError("Dimensions de page invalides.")
            bitmap = page.render(scale=min(3, 1800 / size))
            try:
                out = io.BytesIO()
                bitmap.to_pil().save(out, format="PNG")
                return out.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()


def recognize(data, indexes):
    if os.name != "nt":
        raise ValueError("L’OCR hors ligne nécessite Windows et un pack de langue OCR installé.")
    windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    executable = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    with tempfile.TemporaryDirectory(prefix="sparky-ocr-") as scratch:
        pdf = Path(scratch, "document.pdf")
        pdf.write_bytes(data)
        try:
            job = subprocess.run(
                [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                 str(Path(__file__).with_name("pdf_ocr.ps1")), str(pdf), ",".join(map(str, indexes))],
                capture_output=True, timeout=min(360, 25 + 10 * len(indexes)), creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("OCR Windows indisponible ou délai dépassé. Réessayez avec moins de pages.") from exc
    if job.returncode:
        raise ValueError("Échec de l’OCR local. Vérifiez le PDF et le pack de langue OCR dans les paramètres de Windows.")
    rows = [json.loads(line) for line in job.stdout.decode("utf-8-sig").splitlines() if line.strip()]
    return {row["page"]: row["text"].strip() for row in rows}


def pages(data, limit):
    """Return (one-based page, text, recognized) up to the caller's character budget."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    rows, scanned, used = [], [], 0
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if not text and page.get("/Contents") is not None:
            scanned.append(i)
            if len(scanned) > MAX_SCAN_PAGES:
                raise ValueError(f"OCR limité à {MAX_SCAN_PAGES} pages scannées par fichier. Découpez le PDF.")
        rows.append(text[:limit - used])
        used += len(rows[-1])
        if used >= limit:
            break
    optical = recognize(data, scanned) if scanned else {}
    if not any(rows) and not any(optical.values()):
        raise ValueError("Aucun texte lisible dans le PDF.")
    used = 0
    for i, text in enumerate(rows):
        value = (text or optical.get(i, ""))[:limit - used]
        yield i + 1, value, bool(optical.get(i))
        used += len(value)
        if used >= limit:
            break
