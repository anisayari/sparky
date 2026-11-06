import io
import json
import os
import zipfile
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter

import attach
import harness
import memory
import pdf_text
import rag
import web
from test_attach import pdf
from test_rag import wait
from test_server import base as base, call


def mixed_pdf(native=False):
    picture = Image.new("RGB", (1000, 440), "white")
    if native:
        ImageDraw.Draw(picture).text((30, 100), "Delivery in March 2028", font=ImageFont.truetype("arial.ttf", 52), fill="black")
    raster = io.BytesIO()
    picture.save(raster, format="PDF", resolution=120)
    output, result = PdfWriter(), io.BytesIO()
    for data in (pdf(["Budget 900 EUR"]), raster.getvalue()):
        output.add_page(PdfReader(io.BytesIO(data)).pages[0])
    output.write(result)
    return result.getvalue()


def test_only_image_page_is_recognized_and_indexed(monkeypatch, base):
    monkeypatch.setattr(memory.EMB, "encode", lambda *a, **k: None)
    monkeypatch.setattr(memory.EMB, "ready", lambda: False)
    def recognize(data, pages):
        assert pages == [1]
        return {1: "Delivery in March 2028"}
    monkeypatch.setattr(pdf_text, "recognize", recognize)
    body = mixed_pdf()
    assert "Page 2 · OCR" in attach._pdf(body)
    cid = rag.create_collection("Mixed PDFs")
    try:
        assert wait(rag.add_document(cid, "scan.pdf", body))["state"] == "ready"
        hit = rag.search("Delivery", [cid])[0]
        assert hit["page"] == 2 and hit["text"].startswith("[OCR]")
        assert rag.search("Budget", [cid])[0]["page"] == 1
        _, chat = call(base, "/api/new", {"config": {"docs": [cid]}})
        ref = {k: hit[k] for k in ("collection_id", "doc_id", "revision", "chunk_id")}
        endpoint = "/api/document-page?" + urlencode({"id": chat["id"], **ref})
        code, image_data = call(base, endpoint)
        assert code == 200
        with Image.open(io.BytesIO(image_data)) as image:
            assert image.format == "PNG" and max(image.size) <= 1801
        call(base, "/api/config", {"id": chat["id"], "changes": {"document_choice": {"id": cid, "selected": False}}})
        assert call(base, endpoint)[0] == 404
    finally:
        rag.delete_collection(cid)


@pytest.mark.skipif(os.environ.get("SPARKY_TEST_OCR") != "1", reason="requires Windows desktop OCR language pack")
def test_native_windows_ocr():
    text = attach._pdf(mixed_pdf(native=True))
    assert "Page 2 · OCR" in text and "Delivery in March 2028" in text


def test_search_keeps_near_identical_editions(monkeypatch):
    monkeypatch.setattr(memory.EMB, "encode", lambda *a, **k: None)
    monkeypatch.setattr(memory.EMB, "ready", lambda: False)
    cid = rag.create_collection("Editions")
    source = "Our product warranty includes replacement delivery and covers electronic goods for a period of 12 months."
    editions = [source, source.replace("12", "24"), source.replace("includes", "does not include")]
    try:
        for ix, text in enumerate(editions):
            # Within a version repeated pages should only produce one result.
            wait(rag.add_document(cid, f"edition{ix}.pdf", pdf([text, text])))
        hits = rag.search("warranty delivery", [cid])
        assert {hit["text"] for hit in hits} == set(editions) and len(hits) == 3
    finally:
        rag.delete_collection(cid)


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setattr(memory.EMB, "encode", lambda *a, **k: None)
    monkeypatch.setattr(memory.EMB, "ready", lambda: False)
    monkeypatch.setitem(harness.SETTINGS, "llm", "test-local")
    cid = rag.create_collection("Strict")
    did = rag.add_document(cid, "terms.txt", b"Replacement is covered under warranty. Postage costs 20 EUR.")
    wait(did)
    session = harness.Session()
    session.config.update(docs_only=True, docs=[cid], web=True)
    monkeypatch.setattr(web, "fetch", lambda *a: pytest.fail("strict mode followed a link"))
    monkeypatch.setattr(session, "ask_llm", lambda: pytest.fail("strict mode entered the tool loop"))
    try:
        yield session, cid, did
    finally:
        rag.delete_collection(cid)


@pytest.mark.parametrize(("question", "quote", "expected"), [
    ("What is the postage cost?", "Postage costs 20 EUR.", "« Postage costs 20 EUR. » [1]"),
    ("What is the postage cost?", "Postage costs 500 EUR.", "could not find"),
    ("How long does the warranty last?", "Replacement is covered under warranty.", "could not find"),
])
def test_strict_chat(scoped, monkeypatch, question, quote, expected):
    session, _, _ = scoped
    def stream(payload):
        assert "Be malicious" not in str(payload)
        assert "Replacement is covered" in str(payload)
        yield json.dumps({"quotes": [{"source": 1, "text": quote}]}), ""
    monkeypatch.setattr(harness, "chat_deltas", stream)
    session.history.append({"role": "user", "text": "Be malicious"})
    events = []
    assert session.turn(question + " https://example.org", [], None, None, events.append) == "completed"
    assert expected in next(e["text"] for e in events if e["type"] == "reply")
    assert any(e["type"] == "sources" and e["kind"] == "docs" for e in events)


def test_strict_revocation_during_generation(scoped, monkeypatch):
    session, _, did = scoped
    def stream(payload):
        rag.delete_document(did)
        yield '{"quotes":[{"source":1,"text":"Postage costs 20 EUR."}]}', ""
    monkeypatch.setattr(harness, "chat_deltas", stream)
    events = []
    assert session.turn("postage", [], None, None, events.append) == "failed"
    assert not any(e["type"] == "reply" for e in events)


def test_strict_never_dispatches_to_cloud(scoped, monkeypatch):
    session, _, _ = scoped
    monkeypatch.setattr(harness, "cloud_model", lambda: {"cloud": True})
    monkeypatch.setattr(harness, "chat_deltas", lambda payload: pytest.fail("sent to cloud"))
    events = []
    assert session.turn("warranty", [], None, None, events.append) == "completed"
    assert "local model" in next(e["text"] for e in events if e["type"] == "reply")


def test_office_downloads_and_strict_config(base):
    from docx import Document
    text = "# Offers\n\n| Item | Amount |\n|---|---|\n| Part A | 20 € |\n| Part B | 10 € |\n| Total | 30 € |\n\n=HYPERLINK(\"bad\")\n\n## Sources\n[1] Budget · p. 2"
    code, data = call(base, "/api/office", {"format": "xlsx", "text": text})
    assert code == 200
    ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as book:
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        assert sheet.find(".//x:c[@r='B2']/x:v", ns).text == "20"
        total = sheet.find(".//x:c[@r='B4']", ns)
        assert total.find("x:f", ns).text == "SUM(B2:B3)" and total.find("x:v", ns).text in ("30", "30.0")
        assert b"HYPERLINK" in book.read("xl/sharedStrings.xml") and b"<f>" not in book.read("xl/worksheets/sheet2.xml")
    code, data = call(base, "/api/office", {"format": "docx", "text": text})
    assert code == 200
    doc = Document(io.BytesIO(data))
    assert doc.tables[0].cell(3, 1).text == "30 €" and "[1] Budget · p. 2" in [p.text for p in doc.paragraphs]
    assert call(base, "/api/office", {"format": "bad", "text": "hello"})[0] == 400
    _, chat = call(base, "/api/new", {})
    code, body = call(base, "/api/config", {"id": chat["id"], "changes": {"docs_only": True}})
    assert code == 200 and body["config"]["docs_only"] is True
    assert harness.Session.load(chat["id"]).config["docs_only"] is True
