"""Office archive budgets and admission control shared by the attachment and RAG workers."""
import base64
import io
import threading
import warnings
import zipfile

import pytest

import attach
import rag
from test_rag import wait


def archive(*entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, value in entries:
            z.writestr(name, value)
    return output.getvalue()


def test_rejects_large_office_xml_before_opening_member(monkeypatch):
    data = archive(("word/document.xml", b"x" * 2048))
    monkeypatch.setattr(attach, "MAX_OFFICE_PART", 512)
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *a, **kw: pytest.fail("oversized member was opened"))
    with pytest.raises(ValueError, match="XML Office"):
        attach._office(data, ".docx")
    cid = rag.create_collection("Office limits")
    try:
        result = wait(rag.add_document(cid, "sample.docx", data))
        assert result["state"] == "error" and result["chunks"] == 0 and "XML Office" in result["error"]
    finally:
        rag.delete_collection(cid)


def test_aggregate_xml_budget_excludes_unread_media(monkeypatch):
    a, b = "<t>slide one</t>", "<t>slide two</t>"
    data = archive(("ppt/slides/slide1.xml", a), ("ppt/slides/slide2.xml", b), ("ppt/media/example.png", b"x" * 1024))
    monkeypatch.setattr(attach, "MAX_OFFICE_XML", len(a) + len(b))
    assert "slide two" in attach._office(data, ".pptx")
    monkeypatch.setattr(attach, "MAX_OFFICE_XML", len(a) + len(b) - 1)
    with pytest.raises(ValueError, match="XML Office"):
        attach._office(data, ".pptx")


def test_office_directory_and_member_limits(monkeypatch):
    data = archive(("word/document.xml", "t"), ("extra", "t"))
    with monkeypatch.context() as m:
        m.setattr(attach, "MAX_OFFICE_MEMBERS", 1)
        with pytest.raises(ValueError, match="complexe"):
            attach._office(data, ".docx")
    with monkeypatch.context() as m:
        m.setattr(attach, "MAX_OFFICE_DIRECTORY", 80)
        with pytest.raises(ValueError, match="ZIP"):
            attach._office(data, ".docx")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        duplicate = archive(("word/document.xml", "one"), ("word/document.xml", "two"))
    with pytest.raises(ValueError, match="ambigu"):
        attach._office(duplicate, ".docx")


def test_expansion_of_repeated_spreadsheet_values_is_bounded(monkeypatch):
    data = archive(("xl/sharedStrings.xml", "<sst><si><t>" + "a" * 40 + "</t></si></sst>"),
                   ("xl/worksheets/sheet1.xml", '<row>' + '<c t="s"><v>0</v></c>' * 4 + '</row>'))
    monkeypatch.setattr(attach, "MAX_SHEET_TEXT", 100)
    with pytest.raises(ValueError, match="classeur"):
        attach._office(data, ".xlsx")


@pytest.mark.parametrize("ext,part,xml", [
    (".docx", "word/document.xml", b"<document>" + b"<" * 65536),
    (".xlsx", "xl/sharedStrings.xml", b"<sst>" + b"<si/>" * 10000),
    (".xlsx", "xl/worksheets/sheet1.xml", b"<sheet>" + b"<row/>" * 10000),
], ids=["word-malformed", "sheet-strings-malformed", "sheet-rows-malformed"])
def test_malformed_xml_is_rejected(ext, part, xml):
    with pytest.raises(ValueError, match="XML Office mal formé"):
        attach._office(archive((part, xml)), ext)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize("declaration", ['<!DOCTYPE root [<!ENTITY sample "expanded">]>',
                                          '<!DOCTYPE root SYSTEM "urn:sparky-test-doctype">'])
def test_office_dtd_is_refused_before_expansion(encoding, declaration):
    xml = (declaration + "<root>ok</root>").encode(encoding)
    with pytest.raises(ValueError, match="DTD"):
        attach._office(archive(("content.xml", xml)), ".odt")


def test_office_xml_depth_is_limited(monkeypatch):
    monkeypatch.setattr(attach, "MAX_XML_DEPTH", 4)
    with pytest.raises(ValueError, match="imbriqué"):
        attach._office(archive(("word/document.xml", "<n>" * 10 + "</n>" * 10)), ".docx")


def test_office_paragraphs_tabs_unicode_and_escaping():
    xml = '<w:body xmlns:w="urn:word"><w:p>A&amp;B<w:tab/>été</w:p><w:p><![CDATA[<txt>]]></w:p></w:body>'
    assert attach._office(archive(("word/document.xml", xml.encode("utf-16"))), ".docx") == "A&B\tété\n<txt>\n"


def test_open_document_heading_and_paragraph():
    xml = '<body xmlns:text="urn:text"><text:h>Titre</text:h><text:p>Premier<text:tab/>suivant</text:p></body>'
    assert attach._office(archive(("content.xml", xml)), ".odt") == "Titre\nPremier\tsuivant\n"


def test_xlsx_rich_shared_and_inline_strings():
    shared = '<x:sst xmlns:x="urn:sheet"><x:si><x:r><x:t>pré</x:t></x:r><x:r><x:t>nom</x:t></x:r></x:si></x:sst>'
    sheet = '<row><c t="s"><v>0</v></c><c t="inlineStr"><is><r><t>lune</t></r><r><t> &amp; mer</t></r></is></c></row>'
    result = attach._office(archive(("xl/sharedStrings.xml", shared), ("xl/worksheets/sheet1.xml", sheet)), ".xlsx")
    assert result == "--- Feuille 1 ---\nprénom\tlune & mer"


def test_chat_and_rag_share_capacity_and_release_it(monkeypatch):
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(attach, "_readers", gate)
    started, release = threading.Event(), threading.Event()
    original = attach._extract

    def hold(*args):
        started.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(attach, "_extract", hold)
    cid = rag.create_collection("Shared worker capacity")
    try:
        encoded = base64.b64encode(b"hello").decode()
        first = attach.add("file.txt", encoded)
        assert started.wait(3)
        second = attach.add("file.txt", encoded)
        assert second["state"] == "error" and second["error"] == attach.READERS_BUSY
        queued = rag.add_document(cid, "third.txt", b"hello")
        assert rag._read("SELECT doc_id FROM pending_documents WHERE doc_id=?", (queued,))
        release.set()
        assert wait(queued)["state"] == "ready"
        assert gate.acquire(timeout=3)
        gate.release()
        assert attach.status(first["id"])["state"] == "ok"
        assert wait(rag.add_document(cid, "fourth.txt", b"hello"))["state"] == "ready"
    finally:
        release.set()
        rag.delete_collection(cid)


def test_worker_start_failure_restores_capacity(monkeypatch):
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(attach, "_readers", gate)

    def unavailable(self):
        raise RuntimeError("no thread available")

    monkeypatch.setattr(threading.Thread, "start", unavailable)
    result = attach.add("file.txt", "aGVsbG8=")
    assert result["state"] == "error"
    assert gate.acquire(blocking=False)
