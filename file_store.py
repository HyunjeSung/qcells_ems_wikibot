"""사용자가 대화에 첨부하는 파일 저장소.

원본은 uploads/<file_id><확장자>, 추출 텍스트는 uploads/<file_id>.txt로 디스크에 두고, 메타데이터는
wiki_chat_history의 files 테이블에 둔다. 답변 생성 시 첨부된 파일의 추출 텍스트를 "참고 자료"
맨 앞에 붙인다(wiki_chat_server.build_upload_context 참고).

텍스트 추출은 표준 라이브러리만 쓴다(서버 환경 pip가 깨져 있어 새 패키지 설치가 어려움 —
_parse_xlsx_text 독스트링 참고). PDF만 예외로 pypdf가 설치돼 있으면 쓰고, 없으면 PDF는
"텍스트를 추출하지 못함"으로 저장만 한다.
"""

import io
import re
import uuid
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 답변 프롬프트에 파일 하나당 넣는 최대 글자 수(파일 여러 개 + 위키 검색 결과가 함께 들어가므로).
CONTEXT_CHARS_PER_FILE = 24000

TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".xml", ".yaml", ".yml", ".ini", ".conf", ".cfg",
    ".py", ".c", ".cc", ".cpp", ".h", ".hpp", ".sh", ".js", ".ts", ".html", ".css", ".sql", ".bb", ".bbappend",
}
ALLOWED_EXTS = TEXT_EXTS | {".xlsx", ".docx", ".pptx", ".pdf"}

# 서버가 import 시점에 자기 _parse_xlsx_text를 꽂아 넣는다(순환 import 방지).
XLSX_PARSER = None

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _decode_text(data):
    for enc in ("utf-8-sig", "cp949", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _docx_text(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(_W_NS + "p"):
        text = "".join(t.text or "" for t in p.iter(_W_NS + "t"))
        if text.strip():
            paras.append(text)
    return "\n".join(paras)


def _pptx_text(data):
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted(
            (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)),
        )
        for i, name in enumerate(slides, 1):
            root = ET.fromstring(z.read(name))
            texts = [t.text for t in root.iter(_A_NS + "t") if t.text and t.text.strip()]
            if texts:
                out.append(f"## Slide {i}\n" + "\n".join(texts))
    return "\n\n".join(out)


def _pdf_text(data):
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        t = (page.extract_text() or "").strip()
        if t:
            pages.append(f"## Page {i}\n{t}")
    return "\n\n".join(pages)


def extract_text(filename, data):
    """(텍스트 또는 None, 종류 라벨)."""
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXTS:
        return _decode_text(data), "text"
    if ext == ".xlsx":
        return (XLSX_PARSER(data) if XLSX_PARSER else None), "xlsx"
    if ext == ".docx":
        return _docx_text(data), "docx"
    if ext == ".pptx":
        return _pptx_text(data), "pptx"
    if ext == ".pdf":
        return _pdf_text(data), "pdf"
    return None, ext.lstrip(".") or "file"


def save_upload(filename, data):
    """원본과 추출 텍스트를 디스크에 저장한다. 반환: (file_id, stored_name, kind, text_chars)."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise ValueError(f"지원하지 않는 형식입니다({ext or '확장자 없음'}). 텍스트·CSV·로그·코드·xlsx·docx·pptx·pdf만 올릴 수 있습니다.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"파일이 너무 큽니다(최대 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB).")
    file_id = uuid.uuid4().hex
    stored_name = file_id + ext
    (UPLOAD_DIR / stored_name).write_bytes(data)
    try:
        text, kind = extract_text(filename, data)
    except Exception:
        text, kind = None, ext.lstrip(".")
    text = (text or "").strip()
    (UPLOAD_DIR / (file_id + ".txt")).write_text(text, encoding="utf-8")
    return file_id, stored_name, kind, len(text)


def read_text(file_id):
    path = UPLOAD_DIR / (file_id + ".txt")
    return path.read_text(encoding="utf-8") if path.exists() else ""


def remove(file_row):
    for name in (file_row["stored_name"], file_row["id"] + ".txt"):
        try:
            (UPLOAD_DIR / name).unlink()
        except FileNotFoundError:
            pass
