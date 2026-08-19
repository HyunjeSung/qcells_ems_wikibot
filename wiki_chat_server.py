#!/usr/bin/env python3
"""
Qcells EMS 위키봇 — 라이브 Confluence 검색을 배경지식으로 쓰는 ChatGPT 스타일 웹 챗봇 서버.
(로컬 위키 docs/*.md는 답변 소스에서 제외 — 사용자 확정, 느슨하게만 연관된 범용
아키텍처 문서가 섞여 들어가 답변 품질을 해쳤음)

사용법: python3 wiki_chat_server.py [--port 8010]
접속:  http://localhost:8010  (WSL2 -> Windows 브라우저 자동 포워딩)
"""

import os
import io
import re
import sys
import html
import json
import base64
import shutil
import zipfile
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory

sys.path.insert(0, str(Path(__file__).parent))
from search_query_utils import _clean_query, _tech_query, _extract_terms, _KO_STOP  # CQL 검색어 정제용 헬퍼만 재사용 (로컬 위키 검색 자체는 미사용)
from confluence_to_text import render as render_storage_html
from bs4 import BeautifulSoup
from atlassian_mcp_client import rovo_search, _EMPTY_BODY_NOTE
import wiki_chat_history as chat_history

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DOCS_IMAGES_DIR = BASE_DIR / "docs" / "images"
LIVE_DIAGRAM_DIR = BASE_DIR / ".confluence_live_images"
LIVE_DIAGRAM_DIR.mkdir(exist_ok=True)
ENV_PATH = BASE_DIR / ".env.confluence"
# EnergySW로만 좁혀서 검색하면 다른 스페이스(MAG 등)에 있는 실제 관련 문서를
# 통째로 못 찾는다 — 실측: "Advanced TOU - TimeTable 로직 및 확인사항"은 MAG 스페이스.
# confluence_export_multi.py가 이미 export해본 스페이스 전체를 검색 대상으로 삼는다.
CONFLUENCE_SPACES = ["EnergySW", "ACGEN2", "CWS", "GDRI", "MAG", "HP", "SIACS", "GSP1"]
_CQL_SPACE_CLAUSE = "space in (" + ", ".join(f'"{s}"' for s in CONFLUENCE_SPACES) + ")"

app = Flask(__name__)

SYSTEM_PROMPT = """당신은 "Qcells EMS 위키봇"입니다. QCells EMS(Energy Management System) 팀의
Confluence 문서를 배경지식으로 삼아 답하는 개발 어시스턴트입니다.

답변 규칙:
- 아래 제공된 "참고 자료" 안의 내용만 근거로 답하세요. 참고 자료에 없으면 "위키/Confluence에서 해당 내용을 찾지 못했습니다"라고 말하세요
- 참고 자료 문서 전체를 요약/나열하지 말고, 사용자 질문에 답하는 데 필요한 내용만 골라서 답하세요.
  특히 참고 자료가 PRD/FRD처럼 문서 전체를 다루는 경우, Role별 권한표(예: "Qcells Admin",
  "Fleet Partner Admin" 같은 웹 콘솔 접근 권한)나 웹/클라우드 콘솔 메뉴 이동 경로(예: "GNB 검색 →
  Edit Site → Post-Commissioning") 같은 절은 사용자가 웹 UI 사용법이나 권한 체계를 직접 묻지 않는
  한 답변에 옮기지 마세요 — 임베디드 EMS 자체의 동작을 묻는 질문에는 무관한 내용입니다
- 코드·함수명·설정값은 참고 자료의 표현을 그대로 인용하세요
- 한국어로 답변하되 기술 용어는 원문 그대로 사용하세요
- 이전 대화 맥락을 참고해서 자연스럽게 이어서 답하세요
- 참고 자료 안의 표에서 인원/항목을 세거나 전체를 나열해 달라는 질문을 받으면, 표의 마지막 행까지
  전부 훑은 뒤에 답하세요. 표 중간에 그룹 라벨(예: "Energy Control & Monitoring")이 첫 행에만
  적혀있고 이후 행은 비어있는 형태(병합된 셀)라도, 그 그룹 라벨은 다음 그룹 라벨이 나오기 전까지
  이어지는 모든 행에 적용됩니다 — 라벨이 안 보인다고 그 행을 건너뛰거나 이전 행에서 끊긴 것으로
  오해하지 마세요. 특히 표가 길면 뒤쪽 행을 놓치기 쉬우니, 개수를 답하기 전에 실제로 하나씩 세어서
  일치하는지 스스로 검증하세요
- 참고 자료 안에 `![설명](images/파일명)` 형식의 이미지 참조가 있으면, 그 마크다운 이미지 구문을
  그대로 답변에 포함하세요 (경로를 바꾸거나 지어내지 마세요. 참고 자료에 없는 이미지를 있는 것처럼
  언급하는 것은 절대 금지)
- 사용자가 그림/다이어그램을 요청했는데 참고 자료에 실제 이미지가 없으면, 대신 ```mermaid 코드
  블록으로 개념을 요약하는 다이어그램(플로우차트, 시퀀스, 타임라인 등 적절한 형태)을 직접
  생성해서 보여주세요. 이때는 답변에 "위키에 저장된 그림은 없어 아래처럼 요약 다이어그램을
  생성했습니다"라고 명시하세요
- mermaid 코드 작성 시 반드시 지킬 것 (안 지키면 파싱 에러로 다이어그램이 아예 안 나옴):
  - 한 줄에 관계(엣지) 하나만 작성. "A --> B(라벨) -|c| D" 처럼 한 줄에서 새 노드를
    정의하면서 동시에 다음 화살표로 이어 붙이지 말 것
  - 노드 라벨에 괄호/특수문자가 들어가면 큰따옴표로 감쌀 것: A["시작 시간(HH:mm)"]
  - 엣지 라벨은 반드시 `-->|라벨|` 형태만 사용 (`-|`, `~` 같은 변형 문법 금지)
  - 예시(이 패턴만 따라 쓰면 안전함):
    ```mermaid
    graph TD;
      A[TOU 스케줄] --> B[시작 시간];
      A --> C[종료 시간];
      B -->|적용| D[기본 동작];
      C -->|적용| D;
    ```
- 답변 마지막에 참고한 출처를 나열할 때, 각 참고 자료 블록 첫 줄에 있는 실제 URL을 그대로 써서
  마크다운 하이퍼링크 `[출처명](URL)` 형식으로 작성하세요. `[[출처명]]`처럼 URL 없는 이중 대괄호
  형식은 클릭할 수 없으니 쓰지 마세요. 참고 자료에 없는 URL을 지어내는 것도 절대 금지입니다"""


def _load_confluence_env():
    if not ENV_PATH.exists():
        return None
    env = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k] = v
    return env


_CONF_ENV = _load_confluence_env()


def _cql_search(base, headers, cql, limit, timeout):
    params = urllib.parse.urlencode({
        "cql": cql,
        "limit": limit,
        "expand": "body.storage,version",
    })
    url = f"{base}/wiki/rest/api/content/search?{params}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _slugify(s):
    return re.sub(r'[\\/:*?"<>|]', '_', s).strip()[:150]


def _get_attachments(page_id, headers, base, att_cache, timeout):
    if page_id in att_cache:
        return att_cache[page_id]
    url = f"{base}/wiki/rest/api/content/{page_id}/child/attachment?limit=100"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            atts = json.load(resp).get("results", [])
    except Exception as e:
        print(f"⚠️  첨부파일 조회 실패 pageId={page_id}: {e}", file=sys.stderr)
        atts = []
    att_cache[page_id] = atts
    return atts


def _download_attachment(match, headers, base, dest, timeout):
    dl_path = match["_links"]["download"]
    dl_url = f"{base}{dl_path}" if dl_path.startswith("/wiki") else f"{base}/wiki{dl_path}"
    # Accept: application/json은 JSON API 호출용 — 바이너리 다운로드에는 빼는 게 안전
    dl_headers = {k: v for k, v in headers.items() if k.lower() != "accept"}
    req = urllib.request.Request(dl_url, headers=dl_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        dest.write_bytes(resp.read())


_DRAWIO_VERSION_RE = re.compile(r'v(\d+)\.(\d+)', re.IGNORECASE)
_HTML_TAG_RE = re.compile(r'<[^>]+>')
_PAGE_ID_IN_URL_RE = re.compile(r'pageId=(\d+)')


def _clean_drawio_label(raw):
    text = html.unescape(raw or "")
    text = _HTML_TAG_RE.sub(' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _abs_pos(cid, geoms, cache, depth=0):
    """draw.io 자식 도형의 x/y는 부모(group/container)의 로컬 좌표계 기준 상대값이라,
    부모 체인을 타고 올라가며 더해야 캔버스 절대 좌표가 나온다. 이걸 안 하면(단순히
    mxCell을 문서 순서나 raw y로만 정렬하면) 그룹 안에 있는 도형들의 순서가 뒤섞인다
    (실측: 그룹으로 묶인 Install Data 단계가 앞뒤 라벨과 뒤죽박죽으로 나옴)."""
    if cid in cache or depth > 30:
        return cache.get(cid, (0.0, 0.0))
    if cid not in geoms:
        cache[cid] = (0.0, 0.0)
        return cache[cid]
    x, y, parent = geoms[cid]
    if parent:
        px, py = _abs_pos(parent, geoms, cache, depth + 1)
        result = (x + px, y + py)
    else:
        result = (x, y)
    cache[cid] = result
    return result


def _parse_drawio_sequence(xml_bytes):
    """draw.io 시퀀스 다이어그램 XML(mxGraphModel)에서 도형/화살표 텍스트 라벨을
    절대 y좌표(위→아래) 순으로 뽑아 평문 목록으로 재구성한다. draw.io 파일은 압축 없는
    순수 XML이라(실측 확인: mxfile을 열어보면 바로 <mxGraphModel> 텍스트) 별도
    디코딩 없이 표준 라이브러리 xml.etree만으로 파싱 가능 — vision/OCR 불필요."""
    root = ET.fromstring(xml_bytes)
    # 파일 하나 안에 여러 <diagram> "페이지"가 들어있는 경우가 있다(실측: 이 파일 자체가
    # v1.06/v1.05 두 페이지를 담고 있어, root 전체를 훑으면 v1.05가 v1.06과 겹쳐서
    # 거의 모든 라벨이 두 번씩 나옴). 버전 표기가 가장 높은 페이지 하나만 쓴다.
    diagrams = root.findall("diagram")
    if diagrams:
        def diagram_version(d):
            m = _DRAWIO_VERSION_RE.search(d.get("name", ""))
            return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        scope = max(diagrams, key=diagram_version)
    else:
        scope = root
    geoms = {}
    raw_labels = {}
    for cell in scope.iter("mxCell"):
        cid = cell.get("id")
        if not cid:
            continue
        geom = cell.find("mxGeometry")
        x = float(geom.get("x", 0)) if geom is not None else 0.0
        y = float(geom.get("y", 0)) if geom is not None else 0.0
        geoms[cid] = (x, y, cell.get("parent"))
        value = cell.get("value")
        if value:
            raw_labels[cid] = value

    cache = {}
    cells = []
    for cid, raw in raw_labels.items():
        label = _clean_drawio_label(raw)
        # 도형 라벨에 원본 XML 태그/URL-인코딩 흔적이 섞인 손상된 값은(작성자가 실수로
        # 다른 다이어그램을 붙여넣은 경우 등, 실측 확인) 건너뛴다 — 지어내는 것보다 낫다.
        if not label or "mxgraphmodel" in label.lower() or "%3c" in label.lower():
            continue
        x, y = _abs_pos(cid, geoms, cache)
        cells.append((y, x, label))
    cells.sort(key=lambda c: (c[0], c[1]))
    return "\n".join(f"- {label}" for _, _, label in cells)


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_XLSX_DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
# 표가 큰 시트(예: Install 719행)를 통째로 넣으면 컨텍스트가 터지므로 시트당/전체
# 상한을 둔다. Cover/Install처럼 실제 데이터 시트를 우선하고 Pivot_*/Lists 같은
# 내부용 hidden 시트는 처음부터 건너뛴다.
_XLSX_MAX_ROWS_PER_SHEET = 300
_XLSX_MAX_TOTAL_CHARS = 15000


def _xlsx_shared_strings(z):
    try:
        data = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    return ["".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t")) for si in root.findall(f"{_XLSX_NS}si")]


def _xlsx_sheet_paths(z):
    """workbook.xml(숨김 제외 시트 이름+r:id) + workbook.xml.rels(r:id -> 실제 경로)를
    엮어서 [(시트이름, zip내경로), ...]를 문서 순서대로 반환한다."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = [
        (sh.get("name"), sh.get(f"{_XLSX_DOC_REL_NS}id"))
        for sh in wb.find(f"{_XLSX_NS}sheets")
        if sh.get("state") != "hidden"
    ]
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rel_map = {r.get("Id"): r.get("Target") for r in rels.findall(f"{_XLSX_PKG_REL_NS}Relationship")}
    result = []
    for name, rid in sheets:
        target = rel_map.get(rid)
        if not target:
            continue
        result.append((name, target if target.startswith("xl/") else f"xl/{target}"))
    return result


def _xlsx_cell_value(c, shared):
    t = c.get("t")
    if t == "inlineStr":
        is_el = c.find(f"{_XLSX_NS}is")
        return "".join(tt.text or "" for tt in is_el.iter(f"{_XLSX_NS}t")) if is_el is not None else ""
    v = c.find(f"{_XLSX_NS}v")
    if v is None or v.text is None:
        return ""
    if t == "s":
        idx = int(v.text)
        return shared[idx] if 0 <= idx < len(shared) else ""
    return v.text


def _parse_xlsx_sheet_rows(xml_bytes, shared, max_rows):
    root = ET.fromstring(xml_bytes)
    sheet_data = root.find(f"{_XLSX_NS}sheetData")
    if sheet_data is None:
        return []
    lines = []
    for row in list(sheet_data)[:max_rows]:
        values = [v for v in (_xlsx_cell_value(c, shared) for c in row) if v not in ("", None)]
        if values:
            lines.append(" | ".join(values))
    return lines


_XLSX_SKIP_SHEET_RE = re.compile(r'과거|pivot|^lists$', re.IGNORECASE)


def _parse_xlsx_text(xlsx_bytes):
    """xlsx는 zip 컨테이너 안에 시트별 XML이 들어있는 구조라(실측 확인) openpyxl/pandas
    없이 표준 라이브러리(zipfile + xml.etree)만으로 파싱 가능 — 이 환경은 pip 자체가
    깨져있어(pyOpenSSL 버전 충돌) 새 패키지 설치가 안 되므로 의도적으로 무의존성 유지.
    "Install" 류 이름의 시트를 최우선으로 두고(실측: Cover의 revision history가 먼저
    나오면 그것만으로 상한을 거의 다 써버려 정작 필요한 데이터 시트가 밀림), 과거
    스냅샷/피벗용 내부 시트는 건너뛴 뒤, 전체 글자수 상한(_XLSX_MAX_TOTAL_CHARS)에서 멈춘다."""
    out = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as z:
        shared = _xlsx_shared_strings(z)
        sheets = _xlsx_sheet_paths(z)
        order = {path: i for i, (_, path) in enumerate(sheets)}
        sheets.sort(key=lambda s: (0 if "install" in s[0].lower() else 1, order[s[1]]))
        for name, path in sheets:
            if _XLSX_SKIP_SHEET_RE.search(name or ""):
                continue
            if total >= _XLSX_MAX_TOTAL_CHARS:
                break
            try:
                xml_bytes = z.read(path)
            except KeyError:
                continue
            rows = _parse_xlsx_sheet_rows(xml_bytes, shared, _XLSX_MAX_ROWS_PER_SHEET)
            if not rows:
                continue
            block = f"## Sheet: {name}\n" + "\n".join(rows)
            budget_left = _XLSX_MAX_TOTAL_CHARS - total
            if len(block) > budget_left:
                block = block[:budget_left] + "\n(글자수 상한 도달로 이하 생략)"
            out.append(block)
            total += len(block)
    return "\n\n".join(out) if out else None


def _confluence_rest_headers():
    if not _CONF_ENV:
        return None, None
    base = _CONF_ENV["ATLASSIAN_BASE_URL"].rstrip("/")
    auth = base64.b64encode(
        f"{_CONF_ENV['ATLASSIAN_EMAIL']}:{_CONF_ENV['ATLASSIAN_API_TOKEN']}".encode()
    ).decode()
    return base, {"Authorization": f"Basic {auth}", "Accept": "application/json"}


def _download_latest_attachment(page_id, suffix, timeout=15):
    """페이지에 직접 첨부된 파일 중 suffix(예: ".drawio", ".xlsx")로 끝나는 것 중
    파일명의 "v1.06"/"V1.69" 같은 버전 표기가 가장 높은 걸 골라 (제목, bytes)로
    받아온다. 버전 표기가 없으면 순서상 마지막 것을 쓴다. 첨부 자체가 없으면 (None, None)."""
    base, headers = _confluence_rest_headers()
    if not headers:
        return None, None
    atts = _get_attachments(page_id, headers, base, {}, timeout)
    matches = [a for a in atts if a.get("title", "").lower().endswith(suffix)]
    if not matches:
        return None, None

    def version_key(a):
        m = _DRAWIO_VERSION_RE.search(a.get("title", ""))
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)

    matches.sort(key=version_key, reverse=True)
    target = matches[0]
    dl_path = target["_links"]["download"]
    dl_url = f"{base}{dl_path}" if dl_path.startswith("/wiki") else f"{base}/wiki{dl_path}"
    dl_headers = {k: v for k, v in headers.items() if k.lower() != "accept"}
    req = urllib.request.Request(dl_url, headers=dl_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return target.get("title"), resp.read()


def _fetch_drawio_text(page_id, timeout=15):
    """페이지에 직접 첨부된 .drawio 파일 중 가장 높은 버전 하나를 받아 텍스트로
    파싱한다. Rovo Search가 본문 없는 페이지를 찾아와도(atlassian_mcp_client._EMPTY_BODY_NOTE)
    다이어그램 자체엔 실제 시퀀스 내용이 있는 경우가 흔해서(실측: "06_EMS+ MCU-MPU
    Initialization sequence") 이걸로 보완한다. 실패/첨부없음 시 None."""
    try:
        title, data = _download_latest_attachment(page_id, ".drawio", timeout)
        if not data:
            return None
        return _parse_drawio_sequence(data)
    except Exception as e:
        print(f"⚠️  drawio 다운로드/파싱 실패(page={page_id}): {e}", file=sys.stderr)
        return None


def _fetch_xlsx_text(page_id, timeout=20):
    """페이지에 직접 첨부된 .xlsx 파일 중 가장 높은 버전 하나를 받아 시트별 텍스트
    테이블로 파싱한다(실측: "03_Install Document (MPU-MCU)"처럼 본문 없이 Install Group
    정의를 엑셀로만 관리하는 페이지가 있음). 실패/첨부없음 시 None."""
    try:
        title, data = _download_latest_attachment(page_id, ".xlsx", timeout)
        if not data:
            return None
        return _parse_xlsx_text(data)
    except Exception as e:
        print(f"⚠️  xlsx 다운로드/파싱 실패(page={page_id}): {e}", file=sys.stderr)
        return None


def _fetch_attachment_text(page_id, timeout=20):
    """본문이 빈 페이지를 보완할 첨부파일 텍스트를 찾는다 — .drawio, .xlsx 순으로
    시도해서 처음 찾은 것 하나를 쓴다(한 페이지에 둘 다 있는 경우는 아직 못 봤음,
    있다면 이후 필요에 따라 둘 다 합치도록 확장)."""
    text = _fetch_drawio_text(page_id, timeout)
    if text:
        return text, "drawio 다이어그램"
    text = _fetch_xlsx_text(page_id, timeout)
    if text:
        return text, "엑셀 첨부파일"
    return None, None


def _fetch_live_images(soup, page_id, headers, base, att_cache, timeout):
    """본문 텍스트 추출로는 안 잡히는 실제 이미지(설계/화면 문서에 흔함)를 라이브로
    받아온다. 두 가지 임베드 방식을 모두 처리:
    - drawio 매크로: 렌더링된 PNG가 매크로의 pageId 파라미터가 가리키는 "다른" 페이지에
      "{diagramDisplayName}.png"라는 이름의 첨부파일로 존재 (confluence_fetch_diagrams.py와 동일 로직)
    - ac:image(첨부 스크린샷 등): 같은 페이지의 첨부파일을 ri:attachment/@ri:filename으로 직접 참조
    반환: [(표시용 이름, 로컬 파일명), ...]
    """
    found = []

    for macro in soup.find_all("ac:structured-macro", attrs={"ac:name": ["drawio", "drawio-sketch"]}):
        params = {p.get("ac:name"): p.get_text() for p in macro.find_all("ac:parameter", recursive=False)}
        diagram_name = params.get("diagramDisplayName") or params.get("diagramName")
        src_page_id = params.get("pageId")
        if not diagram_name or not src_page_id:
            continue
        fname = f"{src_page_id}_{_slugify(diagram_name)}.png"
        dest = LIVE_DIAGRAM_DIR / fname
        if not dest.exists():
            atts = _get_attachments(src_page_id, headers, base, att_cache, timeout)
            png_title = diagram_name + ".png"
            match = (next((a for a in atts if a.get("title") == png_title), None)
                     or next((a for a in atts if a.get("title", "").endswith(png_title)
                              and a.get("extensions", {}).get("mediaType") == "image/png"), None)
                     or next((a for a in atts if a.get("title", "").startswith(diagram_name)
                              and a.get("extensions", {}).get("mediaType") == "image/png"), None))
            if not match:
                continue
            try:
                _download_attachment(match, headers, base, dest, timeout)
            except Exception as e:
                print(f"⚠️  다이어그램 다운로드 실패 {diagram_name}: {e}", file=sys.stderr)
                continue
        found.append((diagram_name, fname))

    for img in soup.find_all("ac:image"):
        att_ref = img.find("ri:attachment")
        if not att_ref:
            continue
        filename = att_ref.get("ri:filename")
        if not filename:
            continue
        fname = f"{page_id}_{_slugify(filename)}"
        dest = LIVE_DIAGRAM_DIR / fname
        if not dest.exists():
            atts = _get_attachments(page_id, headers, base, att_cache, timeout)
            match = next((a for a in atts if a.get("title") == filename), None)
            if not match:
                continue
            try:
                _download_attachment(match, headers, base, dest, timeout)
            except Exception as e:
                print(f"⚠️  이미지 다운로드 실패 {filename}: {e}", file=sys.stderr)
                continue
        found.append((filename, fname))

    return found


def _page_to_result(item, base, terms, require_body_match, headers, att_cache, timeout):
    title = item.get("title", "(제목 없음)")
    page_id = item.get("id", "")
    storage = item.get("body", {}).get("storage", {}).get("value", "")
    if not storage:
        return None
    soup = BeautifulSoup(storage, "html.parser")
    text = render_storage_html(soup)

    lower = text.lower()
    match_pos = -1
    for t in terms:
        p = lower.find(t.lower())
        if p >= 0:
            match_pos = p
            break
    # require_body_match=True인 경로(본문 fuzzy 검색)는 CQL text~가 관계없는
    # 페이지까지 fuzzy/stemmed 매치시키는 경우가 흔해서 본문에 실제로 있는지 재검증한다.
    # 제목 검색으로 찾은 경우는 이미 제목 자체가 신뢰할 근거라, 본문이 다이어그램
    # 위주(스크린 디자인 문서 등)라 텍스트가 거의 없어도 그대로 채택한다.
    if require_body_match and terms and match_pos < 0:
        return None

    # 매치 위치 주변으로 발췌 (naive head truncation은 실제 관련 내용을 놓칠 수 있음)
    if match_pos >= 0:
        start = max(0, match_pos - 800)
        excerpt = text[start:start + 3000]
    else:
        excerpt = text[:3000]

    try:
        images = _fetch_live_images(soup, page_id, headers, base, att_cache, timeout)
    except Exception as e:
        print(f"⚠️  라이브 이미지 처리 실패: {e}", file=sys.stderr)
        images = []
    if images:
        img_md = "\n".join(f"![{name}](confluence-images/{fname})" for name, fname in images)
        excerpt = img_md + "\n\n" + excerpt

    page_url = f"{base}/wiki/pages/viewpage.action?pageId={page_id}" if page_id else ""
    return {"title": title, "url": page_url, "text": excerpt}


def search_confluence_live(query, limit=3, timeout=8):
    """Confluence CQL 검색으로 관련 페이지 body를 라이브로 가져와 텍스트 변환.

    text~ 검색만 쓰면(예전 방식) "TOU" 같은 짧은 기술 용어에서 Confluence의
    fuzzy/stemmed 매치가 완전히 무관한 페이지(주간 업무 보고서, 무관한 회의록 등)를
    돌려주고, 정작 제목에 "TOU"가 명시된 실제 설계 문서(스크린 디자인처럼 본문이
    다이어그램 위주라 텍스트가 적은 문서 포함)는 상위에 아예 안 나오는 경우가 실측됨.
    그래서 title~ 검색을 우선 시도하고, 제목에 매치가 없을 때만 text~로 폴백한다.
    """
    if not _CONF_ENV:
        return []

    base = _CONF_ENV["ATLASSIAN_BASE_URL"].rstrip("/")
    auth = base64.b64encode(
        f"{_CONF_ENV['ATLASSIAN_EMAIL']}:{_CONF_ENV['ATLASSIAN_API_TOKEN']}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}

    # CQL 검색은 순수 자연어 질문(조사·물음표 포함)엔 매치가 잘 안 되므로
    # wiki_ask.py와 동일한 방식으로 기술 용어/조사 제거된 형태로 정제한다
    search_term = _tech_query(query)
    if search_term == query:  # 기술 용어가 없어 원문 그대로 반환된 경우
        search_term = _clean_query(query)
    safe_query = search_term.replace('"', ' ').strip()
    if not safe_query:
        return []
    terms = [t for t in safe_query.split() if t]

    title_cql = f'{_CQL_SPACE_CLAUSE} and type = page and title ~ "{safe_query}"'
    try:
        data = _cql_search(base, headers, title_cql, limit, timeout)
    except Exception as e:
        print(f"⚠️  Confluence 라이브 검색 실패(title): {e}", file=sys.stderr)
        data = {"results": []}

    require_body_match = False
    if not data.get("results"):
        text_cql = f'{_CQL_SPACE_CLAUSE} and type = page and text ~ "{safe_query}"'
        try:
            data = _cql_search(base, headers, text_cql, limit, timeout)
        except Exception as e:
            print(f"⚠️  Confluence 라이브 검색 실패(text): {e}", file=sys.stderr)
            return []
        require_body_match = True

    att_cache = {}
    results = []
    for item in data.get("results", []):
        r = _page_to_result(item, base, terms, require_body_match, headers, att_cache, timeout)
        if r:
            results.append(r)
    return results


_CONTENT_CHARS = re.compile(r'[가-힣A-Za-z0-9]+')


def _residual_content_length(text):
    """매체 요청/질문어 등 노이즈 문구를 제거하고 실제로 남는 내용어 길이를 잰다.
    _clean_query는 이 결과가 너무 짧으면 원문을 그대로 반환하는 자체 폴백이 있어서
    "정제해도 남는 게 없다"는 신호로 못 쓴다 — 여기서는 그 폴백 없이 직접 측정."""
    stripped = _KO_STOP.sub(' ', text)
    return sum(len(t) for t in _CONTENT_CHARS.findall(stripped))


def _build_search_query(history):
    """대화의 마지막 메시지만 검색어로 쓰면 "그림으로 보여줘"처럼 그 자체로는
    주제가 없는 후속 질문에서 완전히 엉뚱한 문서가 검색된다. 정제 후 내용이 거의
    안 남으면 직전 사용자 발화까지 합쳐서 검색 문맥을 보강한다."""
    user_turns = [h["content"] for h in history if h.get("role") == "user"]
    last = user_turns[-1] if user_turns else ""
    if len(user_turns) < 2:
        return last
    if _residual_content_length(last) <= 3:
        return f"{user_turns[-2]} {last}"
    return last


# 문법상으로만 살아남는 접속/의문 단어들 — _extract_terms는 "로직"/"커미셔닝" 같은
# 내용어를 위해 한글 2글자+를 다 뽑다 보니 이런 것도 같이 딸려온다("TOU 관련해서
# 로직에 대해 설명한 페이지 있니?" -> tech_query가 ASCII만 남겨 "TOU"로 뭉개버리는
# 문제는 해결됐지만, 이번엔 "관련/대해/설명한/있니" 같은 노이즈가 새로 낌).
_GENERIC_KO_WORDS = {
    "관련", "대해", "대한", "있니", "있나요", "있어", "있음", "무엇",
    "어떤", "설명", "설명한", "설명해", "부분", "내용", "관해서", "관해",
}


def _rovo_search_query(query, extra_terms=None):
    """Rovo Search는 짧은 키워드 질의에서 훨씬 정확함(실측: "Advanced TOU 로직"은
    정확 매치, 자연어 문장 그대로는 노이즈↑). 한글 내용어까지 포함해서 뽑되
    (_extract_terms), 의미 없는 접속/의문 단어는 걸러낸다.

    extra_terms로 원본 질문의 키워드를 함께 넘기면 뒤에 이어붙인다(중복 제거) —
    _expand_search_query가 도메인을 잘못 짚어 엉뚱한 동의어로 확장했을 때(실측:
    "gem net id ffff"를 UL1741SB/CSIP/IEEE2030.5 같은 그리드 연계 표준 쪽으로 확장 —
    net id를 GEM-MI PLC 통신이 아니라 전력망 "네트워크"로 오인), 원본에 있던
    "gem"/"netid"/"ffff" 같은 축약어가 검색어에서 완전히 사라지는 것을 막기 위함."""
    terms = [t for t in _extract_terms(query) if t not in _GENERIC_KO_WORDS]
    if extra_terms:
        seen = {t.lower() for t in terms}
        for t in extra_terms:
            if t.lower() not in seen:
                terms.append(t)
                seen.add(t.lower())
    return " ".join(terms) if terms else query


# Rovo Chat은 검색 전에 스스로 검색어를 LLM으로 재구성한다(실측: "DeviceManager
# 동작원리"라는 질문을 "device manager 동작원리 architecture"로 확장해서 검색 —
# 사용자 질문엔 없던 "architecture"를 추가해서 그 단어가 제목에 들어간 문서를 찾아냄).
# _rovo_search_query는 원문에서 불용어만 제거하는 단순 추출이라 이런 동의어/영문
# 전문용어 확장을 못 해서, 같은 개념이 다른 용어로 적힌 문서를 놓친다. claude -p로
# 검색어를 확장하는 단계를 추가해서 이 격차를 좁힌다. 실패/타임아웃 시 원문 그대로
# 진행(검색 자체를 막으면 안 되므로 조용히 폴백).
#
# 대화 맥락도 같이 넘긴다 — 실측: "Energy SW 인원 정보/담당업무"를 논의하던 대화의
# 후속 질문 "담당업무 로테이션으로 바꾸고 싶은데"가 맥락 없이 확장되면 "로테이션"만
# 보고 System Log 앱의 로그파일 로테이션 기능 문서로 완전히 엉뚱하게 매칭됨. 최신
# 질문만으론 내용어가 있어서(_build_search_query의 "거의 비었을 때만 이전 발화 병합"
# 조건에 안 걸림) 이 케이스를 못 잡는다 — 확장 단계에서 최근 대화를 보고 모호한
# 단어의 의미를 그 자리에서 확정하게 한다.
def _expand_search_query(question, history=None, timeout=30):
    if not _claude_cli_available():
        return question
    context_block = ""
    if history and len(history) > 1:
        recent = history[:-1][-4:]  # 최신 질문 이전 최근 2턴 정도
        lines = []
        for m in recent:
            speaker = "사용자" if m.get("role") == "user" else "위키봇"
            content = (m.get("content") or "")[:300]
            lines.append(f"[{speaker}] {content}")
        context_block = "이전 대화 맥락(최신 질문의 모호한 단어 뜻을 여기서 판단):\n" + "\n".join(lines) + "\n\n"
    prompt = (
        f"{context_block}"
        "다음은 사내 기술 위키/Confluence 검색에 쓸 최신 질문이다. 위 대화 맥락이 "
        "있다면 그 맥락에 맞춰 질문 속 모호한 단어의 의미를 확정한 뒤, 이 질문과 "
        "관련된 영어/한글 핵심 키워드나 동의어를 3~6개 뽑아라(예: '동작원리'면 "
        "'architecture', '구조'도 후보). 검색어로 쓸 키워드만 공백으로 구분해서 "
        "한 줄로 출력하고 다른 설명은 절대 붙이지 마라.\n\n"
        f"최신 질문: {question}"
    )
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        # 시스템 프롬프트를 안 주면 기본 Claude Code 에이전트 페르소나가 실행돼서
        # 단순 키워드 추출 대신 "이 요청을 어떻게 처리할까" 하고 헤매다 느려지거나
        # (실측: 20초+ 타임아웃) 엉뚱한 응답(가상의 Bash 실행 서술 등)을 내놓는다.
        # 최소한의 역할 지정 + 도구 완전 비활성화로 순수 텍스트 완성만 하게 만든다.
        "--system-prompt", "너는 검색어 키워드만 한 줄로 출력하는 도구다. 그 외 어떤 말도 하지 마라.",
        "--tools", "",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return question
        data = json.loads(result.stdout)
        if data.get("is_error"):
            return question
        extra = data["result"].strip().splitlines()[0].strip()
        # 정제된 키워드가 나왔으면 그것만 검색어로 쓴다(원문에 붙이지 않음) — Rovo Search는
        # 짧은 키워드 질의에서 훨씬 정확한데(기존에 검증됨), 잡음 섞인 원문 문장을 그대로
        # 이어붙이면 그 원칙과 반대로 가서 관련도가 흔들린다(실측: "Energy SW"를 물어본
        # 대화의 후속 질문에 무관한 문장이 잔뜩 섞이자 엉뚱한 파트의 R&R 문서가 나온 사례).
        return extra if extra else question
    except Exception:
        return question


def build_context(question, history=None):
    # 로컬 위키(docs/*.md, "1.1 ~ 24.6" 번호 체계)는 사용자 확정으로 답변 소스에서
    # 완전히 제외 — 라이브 Confluence 페이지만 근거로 쓴다(실측: 로컬 위키 청크가
    # 질문과 느슨하게만 연관된 범용 아키텍처 문서를 끌어와 답변이 부정확해짐).
    #
    # Rovo/Confluence 검색은 원문 그대로면 "동작원리"처럼 문서 제목과 다른 표현을
    # 못 찾는 경우가 있어(실측: "architecture" 확장 시에만 관련 문서 1순위) 확장된
    # 질의를 쓴다. Rovo Search(Atlassian 공식 검색엔진, claude mcp login atlassian으로
    # 받아둔 OAuth 토큰 재사용, 무료)가 우리가 직접 짠 CQL 검색보다 훨씬 정확함(실측:
    # "Advanced TOU 로직" 질의에서 원하는 문서 1·2순위 정확 매치 + Jira까지 덤으로).
    # MCP 토큰이 없거나 네트워크 문제로 실패하면 빈 리스트가 오므로 기존 CQL 검색으로
    # 폴백한다. limit=5는 한때 getConfluencePage 전체 본문 fetch와 합쳐지면 표 하나가
    # 주간업무 노이즈 여러 개 사이에 묻혀서 작은 모델이 못 찾는 문제가 실측되어 3으로
    # 축소했었으나, "gem net id" 케이스(질문 확장이 엉뚱한 방향으로 튀면서 정답 문서가
    # 3위 밖으로 밀려 아예 안 잡힘)가 실측되어 사용자 확정으로 다시 5로 되돌림
    # (2026-08-19) — 표-노이즈 리스크가 재발하면 그때 다시 조정.
    expanded_question = _expand_search_query(question, history)
    original_terms = [t for t in _extract_terms(question) if t not in _GENERIC_KO_WORDS]
    original_query = " ".join(original_terms) if original_terms else question

    # 원본 질의를 먼저 검색해 최우선 후보로 삼고, 확장 질의 결과로 보완한다(원본 우선 +
    # 확장 보완 병합) — "extra_terms로 확장 질의 문자열 뒤에 원본 키워드를 붙이는" 이전
    # 방식은 부족했다. 실측: "gem net id ffff" 질문에서 claude -p 확장이 "GEM"을 매번
    # 다른 업계 표준(SECS/GEM 반도체 설비 통신, GPON GEM 광통신 포트 등 우리 도메인과
    # 무관한 것)으로 오인해 그쪽 용어를 검색어에 잔뜩 섞어 넣었고, 원본 키워드를 뒤에
    # 이어붙이는 것만으로는 그 노이즈를 못 이겨서 4번 중 1번꼴로 정답 문서가 아예
    # 후보에서 빠지는 게 재현됨. 반면 원본 질의("gem net id ffff") 단독 검색은 4번
    # 전부 정답 문서를 Rovo 1위로 정확히 찾음 — 그래서 원본 질의를 신뢰의 기준으로 삼고,
    # 확장 질의는 "동작원리→architecture"류(실측 검증된 이득)의 동의어 보완 용도로만
    # 추가한다(2026-08-19, "gem net id" 케이스로 재현/수정).
    live_pages = rovo_search(original_query, limit=3)
    using_rovo = bool(live_pages)

    if using_rovo and expanded_question.strip() != original_query.strip():
        expanded_pages = rovo_search(
            _rovo_search_query(expanded_question, extra_terms=original_terms), limit=3, two_hop=False
        )
        seen_urls = {p["url"] for p in live_pages}
        for p in expanded_pages:
            if p["url"] not in seen_urls:
                live_pages.append(p)
                seen_urls.add(p["url"])

    # 본문이 진짜로 비어있는 페이지(다이어그램/엑셀 첨부파일만 있음)는 원본 첨부파일을
    # 직접 파싱해서 보완한다(_fetch_attachment_text 참고, 사용자 확정 2026-08-12).
    for p in live_pages:
        if p.get("text") == _EMPTY_BODY_NOTE:
            m = _PAGE_ID_IN_URL_RE.search(p.get("url", ""))
            if not m:
                continue
            extracted_text, source_kind = _fetch_attachment_text(m.group(1))
            if not extracted_text:
                continue
            if source_kind == "drawio 다이어그램":
                caveat = (
                    "도형 좌표 기준으로 정렬했으나 alt/loop 같은 중첩 프레임 구조상 완벽한 "
                    "시간순 재현은 아닐 수 있습니다 — 각 항목은 실제 다이어그램에 있는 내용이 "
                    "맞지만, 단계 순서는 이 목록의 나열 순서를 그대로 확신하지 말고 register/명령 "
                    "이름의 논리적 흐름으로 재구성해서 답하세요."
                )
            else:
                caveat = (
                    "시트/행 순서 그대로 옮긴 것이라 병합 셀이나 서식으로만 표현된 정보(예: "
                    "그룹 경계선)는 텍스트에 안 드러날 수 있습니다 — 표 구조를 보수적으로 "
                    "해석하고, 확실치 않은 셀 대응은 추측 없이 원본 확인을 권하세요."
                )
            p["text"] = (
                f"(아래는 {source_kind} 원본(페이지 본문 자체는 비어있음)에서 자동 추출한 "
                f"내용입니다. {caveat})\n" + extracted_text
            )

    if not using_rovo:
        # 스페이스를 7개로 넓힌 뒤로 동일 키워드 매치 건수가 훨씬 많아져서(예: "TOU" 20+건)
        # limit=3이면 진짜 관련 문서가 순위 밖으로 밀릴 위험이 커짐 -> 여유 있게 5개
        live_pages = search_confluence_live(expanded_question, limit=5)

    parts = []
    sources = []

    for p in live_pages:
        kind = "Jira" if p.get("type") == "issue" else "Confluence"
        label_type = "jira" if p.get("type") == "issue" else "confluence"
        parts.append(f"[출처: {kind}(live, Rovo Search) - {p['title']} | URL: {p['url']}]\n{p['text']}"
                     if using_rovo else f"[출처: Confluence(live) - {p['title']} | URL: {p['url']}]\n{p['text']}")
        sources.append({"type": label_type, "label": p["title"], "url": p["url"]})

    return "\n\n---\n\n".join(parts), sources


OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = os.environ.get("WIKIBOT_OLLAMA_MODEL", "exaone3.5:2.4b")


def list_ollama_models():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
            data = json.load(resp)
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _build_messages(history, context):
    messages = list(history[:-1])  # 마지막 메시지(현재 질문)는 컨텍스트를 붙여서 재구성
    last_question = history[-1]["content"]
    messages.append({
        "role": "user",
        "content": (
            f"=== 참고 자료 ===\n{context}\n\n"
            f"=== 질문 ===\n{last_question}"
        ),
    })
    return messages


def call_claude(history, context):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=_build_messages(history, context),
    )
    return msg.content[0].text


def call_ollama(history, context, model):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + _build_messages(history, context),
        "stream": False,
        # Ollama 기본 num_ctx=2048은 위키 컨텍스트(보통 3천~1만+자)를 넣으면
        # 앞부분(시스템 프롬프트+가장 관련도 높은 청크)이 통째로 잘려나가 근거 없는
        # 답변(환각)이 나온다 — 반드시 넉넉하게 키워줘야 함.
        # 16384로 키워봤지만(큰 컨텍스트 대응) GPU 4GB에서 KV캐시가 넘쳐 18%/82%
        # CPU/GPU로 갈라지며 속도만 3배 느려지고 환각은 그대로였음(실측) -> 되돌림.
        # 진짜 원인은 컨텍스트 절대량보다 "표 형태 정보가 잡담성 텍스트에 묻히는 것"에
        # 가까움 -> build_context에서 노이즈 청크 필터링 + full-page fetch 개수 축소로 대응
        "options": {"num_ctx": 8192},
    }
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    return data["message"]["content"]


def _ollama_available():
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2)
        return True
    except Exception:
        return False


# Rovo Chat(Confluence 내장 AI)의 "답변 합성" 자체는 비공개 API라 재사용 불가 —
# 공식으로 노출된 건 Rovo Search(검색)뿐이라 그건 이미 build_context()에서 쓰고 있음.
# 대신 Rovo Search가 찾아온 컨텍스트를 로컬 소형 모델이 아니라 Claude로 합성하면
# Rovo Chat에 근접한 답변 품질이 나온다는 판단(사용자 확정) -> 답변 합성도 항상
# `claude -p` CLI로 라우팅한다. 별도 API 결제 없이 로그인된 Claude Pro 세션으로
# 동작하지만(= Claude Code 사용량/속도제한 차감), 매 질문마다 호출됨을 감안할 것.


def _claude_cli_available():
    return shutil.which("claude") is not None


def call_claude_cli(history, context, timeout=180):
    messages = _build_messages(history, context)
    # claude -p는 role 배열이 아니라 프롬프트 문자열 하나만 받으므로 이전 대화를
    # 텍스트로 펼쳐서 넣는다 (그림 요청은 보통 짧은 후속 질문이라 부담 적음)
    parts = []
    for m in messages[:-1]:
        speaker = "사용자" if m["role"] == "user" else "위키봇"
        parts.append(f"[{speaker}] {m['content']}")
    parts.append(messages[-1]["content"])
    prompt = "\n\n".join(parts)

    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--system-prompt", SYSTEM_PROMPT,
        # --disallowedTools(차단 목록)는 mcp__atlassian__* 같은 MCP 도구는 안 걸러서,
        # 모델이 스스로 그 도구를 호출하려다 비대화형(-p) 모드라 권한 승인을 받지
        # 못하고 막히는 사고가 실측됨("도구 권한이 승인되지 않아... 이 세션은
        # non-interactive라서..."). 컨텍스트에 이미 필요한 자료를 다 넣어주므로
        # 답변 합성 단계엔 도구가 전혀 필요 없다 -> 전부 비활성화.
        "--tools", "",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exit={result.returncode}: {result.stderr[:500]}")
    data = json.loads(result.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude CLI error: {data.get('result')}")
    return data["result"]


def generate_answer(history, context, model=None):
    """(답변, backend) 튜플을 반환한다. backend는 _ensure_real_images_shown을
    적용할지 판단하는 데 쓰인다 — claude -p/API는 이미지 유무를 스스로 정확히
    판단하므로 그 판단을 후처리로 덮어쓰면 안 되고, 이 안전장치는 원래 취지대로
    작은 로컬 모델(ollama) 답변에만 적용해야 한다."""
    if _claude_cli_available():
        try:
            return call_claude_cli(history, context), "claude_cli"
        except Exception as e:
            print(f"⚠️  claude CLI 실패, 폴백: {e}", file=sys.stderr)

    if os.environ.get("ANTHROPIC_API_KEY"):
        return call_claude(history, context), "claude_api"
    if _ollama_available():
        return call_ollama(history, context, model or DEFAULT_OLLAMA_MODEL), "ollama"
    return (
        "⚠️ ANTHROPIC_API_KEY도 없고 로컬 Ollama 서버도 응답하지 않습니다. "
        "`ollama serve`를 실행하거나 ANTHROPIC_API_KEY를 설정해주세요.\n\n"
        "--- 참고로 찾은 자료 ---\n" + context
    ), "none"


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/wiki-images/<path:filename>")
def wiki_images(filename):
    return send_from_directory(DOCS_IMAGES_DIR, filename)


@app.route("/confluence-images/<path:filename>")
def confluence_images(filename):
    return send_from_directory(LIVE_DIAGRAM_DIR, filename)


# 로컬 위키 이미지(images/파일명)와 라이브 Confluence에서 받아온 이미지
# (confluence-images/파일명) 두 경로 프리픽스를 모두 인식한다
_IMG_MD = re.compile(r'!\[[^\]]*\]\((?:\./)?(?:images|confluence-images)/[^)]+\)')
_IMG_MD_PATH = re.compile(r'(!\[[^\]]*\]\()(?:\./)?(images|confluence-images)/([^)]+)(\))')
_NO_IMAGE_CLAIM = re.compile(r'^.*위키에?\s*저장된?\s*그림은?\s*없어[^\n]*\n?', re.MULTILINE)

_IMG_ROUTE = {"images": "wiki-images", "confluence-images": "confluence-images"}


def _fix_image_paths(answer):
    """답변 속 이미지 마크다운(images/, confluence-images/)을 실제로 서빙되는 경로로 변환."""
    return _IMG_MD_PATH.sub(lambda m: f"{m.group(1)}/{_IMG_ROUTE[m.group(2)]}/{m.group(3)}{m.group(4)}", answer)


def _ensure_real_images_shown(answer, context):
    """모델이 컨텍스트에 실제로 있는 이미지를 놓치고 mermaid로 지어내는 경우가 있어
    (특히 작은 로컬 모델), 실제 위키 이미지가 컨텍스트에 있으면 모델 판단에 기대지 않고
    프로그래밍적으로 항상 보여준다."""
    real_images = list(dict.fromkeys(_IMG_MD.findall(context)))
    if not real_images:
        return answer
    if _IMG_MD.search(answer):
        return answer  # 모델이 이미 실제 이미지를 포함시킴
    answer = _NO_IMAGE_CLAIM.sub('', answer)
    shown = "\n".join(real_images[:2])
    return f"{answer.rstrip()}\n\n관련 이미지:\n{shown}"


@app.route("/api/models")
def models():
    return jsonify({
        "models": list_ollama_models(),
        "default": DEFAULT_OLLAMA_MODEL,
        "claude_available": bool(os.environ.get("ANTHROPIC_API_KEY")),
    })


@app.route("/api/chat", methods=["POST"])
def chat():
    body = request.get_json(force=True) or {}
    history = body.get("history", [])
    model = body.get("model") or None
    conversation_id = body.get("conversation_id") or None
    # 그림 문법 오류로 같은 turn을 더 큰 모델로 재시도하는 호출(프론트 pickEscalationModel) —
    # 이때는 사용자 메시지를 또 저장하거나 답변 행을 새로 쌓지 않고 직전 답변만 덮어쓴다.
    retry = bool(body.get("retry"))
    if not history or history[-1].get("role") != "user":
        return jsonify({"error": "history의 마지막 메시지는 role=user 여야 합니다."}), 400

    if not conversation_id:
        conversation_id = chat_history.create_conversation(history[-1]["content"])
    if not retry:
        chat_history.add_message(conversation_id, "user", history[-1]["content"])

    search_query = _build_search_query(history)
    context, sources = build_context(search_query, history)
    answer, backend = generate_answer(history, context, model)
    if backend == "ollama":
        answer = _ensure_real_images_shown(answer, context)
    answer = _fix_image_paths(answer)

    if retry:
        chat_history.replace_last_message(conversation_id, "assistant", answer, sources)
    else:
        chat_history.add_message(conversation_id, "assistant", answer, sources)

    return jsonify({"answer": answer, "sources": sources, "conversation_id": conversation_id})


@app.route("/api/conversations")
def conversations():
    return jsonify({"conversations": chat_history.list_conversations()})


@app.route("/api/conversations/<conversation_id>")
def conversation_detail(conversation_id):
    conv = chat_history.get_conversation(conversation_id)
    if conv is None:
        return jsonify({"error": "대화를 찾을 수 없습니다."}), 404
    return jsonify(conv)


@app.route("/api/conversations/<conversation_id>", methods=["DELETE"])
def conversation_delete(conversation_id):
    chat_history.delete_conversation(conversation_id)
    return jsonify({"ok": True})


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


def main():
    port = 8010
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    print(f"🤖 Qcells EMS 위키봇 서버 시작 → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
