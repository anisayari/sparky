"""Attached files -> text given to the assistant as context (everything stays on this PC).

  text and code   read as they are
  PDF             pypdf
  Word, PowerPoint, Excel, OpenDocument   their XML, without extra package
  audio and video transcribed offline with Whisper (decoded by PyAV)
Images do not come here: the UI sends them as pictures the assistant can see.
"""
import base64, io, os, re, threading, time, uuid, zipfile
from xml.parsers import expat

MAX_CHARS = 40000          # per file (~10k tokens): the context of the assistant is 32k tokens
MAX_BYTES = 200 * 2**20
MAX_OFFICE_DIRECTORY = 2 * 2**20
MAX_OFFICE_MEMBERS = 4096
MAX_OFFICE_PART = 16 * 2**20    # uncompressed bytes of one text-bearing XML part
MAX_OFFICE_XML = 32 * 2**20     # cumulative uncompressed XML bytes per document
MAX_XML_DEPTH = 128
MAX_SHEET_TEXT = 8_000_000      # shared strings can be referenced by many cells
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
            ".conf", ".log", ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".scss", ".java", ".kt", ".c",
            ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".sql", ".sh", ".bat", ".ps1", ".r", ".lua",
            ".tex", ".srt", ".vtt", ".rtf", ".env", ".gitignore", ".dockerfile"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma", ".aiff", ".amr"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".mpeg", ".mpg", ".3gp"}
OFFICE = {".docx": "word/document.xml", ".pptx": "ppt/slides/", ".xlsx": "xl/sharedStrings.xml",
          ".odt": "content.xml", ".odp": "content.xml", ".ods": "content.xml"}

files = {}   # id -> {"name", "kind", "state": "reading|ok|error", "text", "error", "chars", "truncated", "t"}
_lock = threading.Lock()
_readers = threading.BoundedSemaphore(2)  # shared by chat attachments and RAG imports
READERS_BUSY = "lecteurs de documents occupés, réessaie dans un instant"


def start_reader(target, *args):
    """Reject excess jobs before creating a thread or queuing another large document."""
    if not _readers.acquire(blocking=False):
        return False

    def run():
        try:
            target(*args)
        finally:
            _readers.release()

    try:
        threading.Thread(target=run, daemon=True).start()
    except RuntimeError:  # the OS could not start the thread
        _readers.release()
        return False
    return True


def kind_of(name, mime=""):
    ext = os.path.splitext(name.lower())[1]
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext in OFFICE:
        return "doc"
    if ext in AUDIO_EXT or mime.startswith("audio/"):
        return "audio"
    if ext in VIDEO_EXT or mime.startswith("video/"):
        return "video"
    if ext in TEXT_EXT or mime.startswith("text/") or mime in ("application/json", "application/xml"):
        return "text"
    return "other"


def _decode_text(data):
    for enc in ("utf-16",) if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc).lstrip("﻿")
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _looks_text(data):
    head = data[:8192]
    return b"\x00" not in head and (not head or sum(32 <= b < 127 or b in (9, 10, 13) or b >= 128 for b in head) / len(head) > 0.95)


def _parse_office_xml(xml, start, end, text):
    """Consume SAX-style events from a bounded XML part, without a tree, DTD, or external entities."""
    parser = expat.ParserCreate()
    depth = 0

    def on_start(name, attrs):
        nonlocal depth
        depth += 1
        if depth > MAX_XML_DEPTH:
            raise ValueError("XML Office trop imbriqué")
        start(name.rsplit(":", 1)[-1], attrs)

    def on_end(name):
        nonlocal depth
        end(name.rsplit(":", 1)[-1])
        depth -= 1

    def refuse_doctype(*args):
        raise ValueError("DTD interdit dans le XML Office")

    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler, parser.EndElementHandler = on_start, on_end
    parser.CharacterDataHandler = text
    try:
        parser.Parse(xml, True)
    except expat.ExpatError as e:
        raise ValueError("XML Office mal formé") from e


def _xml_text(xml):
    out = io.StringIO()

    def start(tag, attrs):
        if tag == "tab":
            out.write("\t")

    def end(tag):
        if tag in ("p", "h", "row"):
            out.write("\n")

    _parse_office_xml(xml, start, end, out.write)
    return out.getvalue()


def _sheet_values(xml, shared, write=None):
    """Collect an XLSX string table, or emit worksheet values one cell at a time."""
    record = "c" if write else "si"
    buffer = capture = None
    shared_index = False
    cells = None

    def start(tag, attrs):
        nonlocal buffer, capture, shared_index, cells
        if tag == "row" and write:
            if cells is not None:
                raise ValueError("ligne imbriquée dans le classeur")
            cells = 0
            write("\n")
        elif tag == record:
            if buffer is not None or (write and cells is None):
                raise ValueError("valeur mal placée dans le classeur")
            buffer = io.StringIO()
            shared_index = attrs.get("t") == "s"
        elif buffer is not None and tag in ("t", "v"):
            capture = tag

    def text(value):
        if capture:
            buffer.write(value)

    def end(tag):
        nonlocal buffer, capture, cells
        if tag == capture:
            capture = None
        elif tag == "row" and write:
            cells = None
        elif tag == record:
            value = buffer.getvalue()
            buffer = None
            if not write:
                shared.append(value)
                return
            if shared_index:
                index = value.strip()
                if not index.isascii() or not index.isdecimal():
                    raise ValueError("référence de chaîne partagée invalide")
                index = index.lstrip("0") or "0"
                if len(index) > 8 or int(index) >= len(shared):
                    raise ValueError("référence de chaîne partagée invalide")
                value = shared[int(index)]
            if cells:
                write("\t")
            write(value)
            cells += 1

    _parse_office_xml(xml, start, end, text)


class _OfficeInput(io.BytesIO):
    """Bound ZipFile's eager central-directory read; member data is read in small blocks."""
    def __init__(self, data):
        super().__init__(data)
        self.length = len(data)

    def read(self, size=-1):
        if size < 0:
            size = self.length - self.tell()
        if size > MAX_OFFICE_DIRECTORY:
            raise ValueError("répertoire ZIP trop volumineux")
        return super().read(size)


def _office(data, ext):
    if len(data) > MAX_BYTES:
        raise ValueError("document trop volumineux")
    with zipfile.ZipFile(_OfficeInput(data)) as z:
        names = z.namelist()
        if len(names) > MAX_OFFICE_MEMBERS or len(set(names)) != len(names):
            raise ValueError("archive Office trop complexe ou ambiguë")
        remaining = MAX_OFFICE_XML

        def read_xml(name):
            nonlocal remaining
            info = z.getinfo(name)
            limit = min(MAX_OFFICE_PART, remaining)
            if info.file_size > limit or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError("taille ou compression XML Office non prise en charge")
            data = bytearray()
            with z.open(info) as part:
                while block := part.read(min(65536, limit - len(data) + 1)):
                    if len(data) + len(block) > limit:
                        raise ValueError("limite de décompression Office dépassée")
                    data.extend(block)
            remaining -= len(data)
            return data

        return _office_text(ext, names, read_xml)


def _office_text(ext, names, read_xml):
    if ext == ".pptx":
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        return "\n\n".join(f"--- Diapositive {i + 1} ---\n" + _xml_text(read_xml(n)).strip()
                           for i, n in enumerate(slides))
    if ext == ".xlsx":  # shared strings + inline values of each sheet, row by row
        shared = []
        if "xl/sharedStrings.xml" in names:
            _sheet_values(read_xml("xl/sharedStrings.xml"), shared)
        out = io.StringIO()

        def write(text):
            if out.tell() + len(text) > MAX_SHEET_TEXT:
                raise ValueError("texte du classeur trop volumineux")
            out.write(text)

        for n in sorted(x for x in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", x)):
            if out.tell():
                write("\n")
            write("--- Feuille " + re.findall(r"\d+", n)[-1] + " ---")
            _sheet_values(read_xml(n), shared, write)
        return out.getvalue()
    return _xml_text(read_xml(OFFICE[ext]))


def _pdf(data):
    import pdf_text
    return "\n\n".join(f"--- Page {page}{' · OCR' if ocr else ''} ---\n{text}"
                       for page, text, ocr in pdf_text.pages(data, MAX_CHARS + 1))


def _audio(data):
    """Any audio or video file -> 16 kHz mono -> Whisper."""
    import av
    import numpy as np
    import stt
    m, ready = server_voice()
    if not ready:
        raise RuntimeError("installe la dictée vocale (Whisper) pour transcrire l'audio")
    chunks = []
    with av.open(io.BytesIO(data)) as c:
        if not c.streams.audio:
            raise RuntimeError("aucune piste audio")
        res = av.AudioResampler(format="flt", layout="mono", rate=16000)
        for frame in c.decode(audio=0):
            for f in res.resample(frame):
                chunks.append(f.to_ndarray().reshape(-1))
        for f in res.resample(None):
            chunks.append(f.to_ndarray().reshape(-1))
    audio = np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)
    text = stt.transcribe_array(audio, m["id"])
    if not text:
        raise RuntimeError("aucune parole détectée")
    return f"[Transcription de l'audio, {len(audio) / 16000:.0f} s]\n" + text


def _extract(fid, name, data, mime):
    f = files[fid]
    try:
        k, ext = f["kind"], os.path.splitext(name.lower())[1]
        if k == "pdf":
            text = _pdf(data)
        elif k == "doc":
            text = _office(data, ext)
        elif k in ("audio", "video"):
            text = _audio(data)
        elif k == "text" or _looks_text(data):
            f["kind"], text = "text", _decode_text(data)
        else:
            raise RuntimeError("format non lu : seuls le texte, les PDF, Office, l'audio et la vidéo sont pris en compte")
        text = text.replace("\r\n", "\n").strip()
        if not text:
            raise RuntimeError("fichier vide")
        f.update(truncated=len(text) > MAX_CHARS, chars=len(text), text=text[:MAX_CHARS], state="ok")
    except Exception as e:
        f.update(state="error", error=str(e) or e.__class__.__name__)


def add(name, data_b64, mime=""):
    """Starts reading a file; the UI polls status(id)."""
    data = base64.b64decode(data_b64.split(",", 1)[-1])
    fid = uuid.uuid4().hex[:12]
    with _lock:
        for k in [k for k, v in files.items() if time.time() - v["t"] > 6 * 3600]:  # forget old unsent files
            files.pop(k, None)
        files[fid] = {"id": fid, "name": name, "kind": kind_of(name, mime), "state": "reading", "text": "",
                      "error": "", "chars": 0, "truncated": False, "size": len(data), "t": time.time()}
    if len(data) > MAX_BYTES:
        files[fid].update(state="error", error="fichier trop gros (200 Mo max.)")
    elif not start_reader(_extract, fid, name, data, mime):
        files[fid].update(state="error", error=READERS_BUSY)
    return status(fid)


def status(fid):
    f = files.get(fid)
    return {k: v for k, v in f.items() if k != "text"} if f else None


def take(ids):
    """Files ready to send: [{name, kind, text, truncated}]."""
    return [{"name": f["name"], "kind": f["kind"], "text": f["text"], "truncated": f["truncated"], "size": f["size"]}
            for f in (files.get(i) for i in ids or []) if f and f["state"] == "ok"]


server_voice = None   # set by server.py: returns (voice model, installed)
