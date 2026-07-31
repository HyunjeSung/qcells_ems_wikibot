#!/usr/bin/env python3
"""
Qcells EMS 위키봇 — 라이브 Confluence 검색을 배경지식으로 쓰는 ChatGPT 스타일 웹 챗봇 서버.
(로컬 위키 docs/*.md는 답변 소스에서 제외 — 사용자 확정, 느슨하게만 연관된 범용
아키텍처 문서가 섞여 들어가 답변 품질을 해쳤음)

사용법: python3 wiki_chat_server.py [--port 8010]
접속:  http://localhost:8010  (WSL2 -> Windows 브라우저 자동 포워딩)
"""

import os
import re
import sys
import json
import base64
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory

sys.path.insert(0, str(Path(__file__).parent))
from search_query_utils import _clean_query, _tech_query, _extract_terms, _KO_STOP  # CQL 검색어 정제용 헬퍼
from confluence_to_text import render as render_storage_html
from bs4 import BeautifulSoup
from atlassian_mcp_client import rovo_search
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
CONFLUENCE_SPACES = ["EnergySW", "ACGEN2", "CWS", "GDRI", "MAG", "HP", "SIACS"]
_CQL_SPACE_CLAUSE = "space in (" + ", ".join(f'"{s}"' for s in CONFLUENCE_SPACES) + ")"

app = Flask(__name__)

SYSTEM_PROMPT = """당신은 "Qcells EMS 위키봇"입니다. QCells EMS(Energy Management System) 팀의
내부 위키(docs/*.md)와 Confluence(EnergySW 스페이스) 문서를 배경지식으로 삼아 답하는
개발 어시스턴트입니다.

답변 규칙:
- 아래 제공된 "참고 자료" 안의 내용만 근거로 답하세요. 참고 자료에 없으면 "위키/Confluence에서 해당 내용을 찾지 못했습니다"라고 말하세요
- 코드·함수명·설정값은 참고 자료의 표현을 그대로 인용하세요
- 한국어로 답변하되 기술 용어는 원문 그대로 사용하세요
- 이전 대화 맥락을 참고해서 자연스럽게 이어서 답하세요
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
- 답변 마지막에 참고한 출처를 "[[출처명]]" 형식으로 나열하세요"""


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


def _rovo_search_query(query):
    """Rovo Search는 짧은 키워드 질의에서 훨씬 정확함(실측: "Advanced TOU 로직"은
    정확 매치, 자연어 문장 그대로는 노이즈↑). 한글 내용어까지 포함해서 뽑되
    (_extract_terms), 의미 없는 접속/의문 단어는 걸러낸다."""
    terms = [t for t in _extract_terms(query) if t not in _GENERIC_KO_WORDS]
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
        return f"{question} {extra}" if extra else question
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
    # 폴백한다. limit=5는 getConfluencePage 전체 본문 fetch와 합쳐지면 표 하나가
    # 주간업무 노이즈 여러 개 사이에 묻혀서 작은 모델이 못 찾는 문제가 실측됨 -> 3으로 축소
    expanded_question = _expand_search_query(question, history)
    live_pages = rovo_search(_rovo_search_query(expanded_question), limit=3)
    using_rovo = bool(live_pages)
    if not using_rovo:
        # 스페이스를 7개로 넓힌 뒤로 동일 키워드 매치 건수가 훨씬 많아져서(예: "TOU" 20+건)
        # limit=3이면 진짜 관련 문서가 순위 밖으로 밀릴 위험이 커짐 -> 여유 있게 5개
        live_pages = search_confluence_live(expanded_question, limit=5)

    parts = []
    sources = []

    for p in live_pages:
        kind = "Jira" if p.get("type") == "issue" else "Confluence"
        label_type = "jira" if p.get("type") == "issue" else "confluence"
        parts.append(f"[출처: {kind}(live, Rovo Search) - {p['title']}]\n{p['text']}"
                     if using_rovo else f"[출처: Confluence(live) - {p['title']}]\n{p['text']}")
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
