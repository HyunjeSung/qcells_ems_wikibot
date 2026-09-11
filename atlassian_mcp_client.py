#!/usr/bin/env python3
"""
Atlassian Rovo MCP Server(https://mcp.atlassian.com/v1/mcp)를 위한 최소 Python 클라이언트.

Claude Code가 `claude mcp login atlassian`으로 이미 받아둔 OAuth 토큰을
(~/.claude/.credentials.json)을 그대로 재사용한다 — 별도 앱 등록/로그인 없이 무료.
Rovo Search는 Atlassian 자체 검색 엔진이라 우리가 CQL로 직접 짠 검색보다 관련도가
훨씬 높다(실측: "Advanced TOU 로직" 질의에서 원하는 문서를 1, 2순위로 정확히 찾음).

주의: refresh_token은 1회용(rotation)이라, Claude Code 본인 세션과 이 클라이언트가
서로 다른 토큰을 들고 있으면 한쪽이 갱신할 때 다른 쪽 토큰이 무효화된다. 그래서
항상 같은 credentials.json 파일을 읽고 갱신 결과를 그 자리에 다시 써서 공유한다.
"""

import itertools
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 예전에 getConfluencePage fetch를 ThreadPoolExecutor로 병렬화했다가 동시 요청 간 응답이
# 뒤섞이는 사고가 나서 순차 방식으로 되돌렸다(rovo_search 안 주석 참고). 요청 id를 고유값으로
# 주는 것 자체는 여전히 안전한 습관이라 유지 — 고정값(예: 항상 1)은 절대 쓰지 말 것.
_next_request_id = itertools.count(1)

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
TOKEN_ENDPOINT = "https://cf.mcp.atlassian.com/v1/token"

# Rovo Search(MCP "search" 도구) 자체엔 스페이스로 좁히는 파라미터가 없어서 같은 Atlassian
# 테넌트(growingenergylabs.atlassian.net)에 있는 Qcells EMS와 무관한 다른 제품 스페이스(NHA,
# RAT 등) 문서까지 검색 결과에 섞여 들어온다(실측: "TOU가 뭔지 설명해줘" 질문에서 RAT의
# "Time Of Use(TOU) KR" 같은 무관 문서가 출처로 나옴). Confluence 결과만 URL의 스페이스 키로
# 걸러낸다 — Jira 이슈는 스페이스 개념이 없으므로 그대로 통과시킨다.
# GSP1은 이름 그대로 "Global SW PM"(Global Software Product Management) 스페이스로, PRD/FRD
# 같은 요구사항 문서가 여기서 관리된다(사용자 확인, 2026-07-31) — 처음엔 무관한 다른 제품
# 스페이스로 오판해서 제외했었는데, 실제로는 정식으로 포함해야 하는 스페이스였음.
# GSP("Development PM", 현행)/DP1("Development PM (old)", 구버전)은 하드웨어/시스템 PM
# 조직도·R&R·주간회의록을 다루는 스페이스로, 이름이 비슷한 GSP1("Global SW PM", 소프트웨어
# PM의 PRD/FRD)과는 완전히 다른 스페이스다. "김하율(Hayool Kim)의 R&R" 질문에서 이 둘이
# 스코프 밖이라 시스템 PM 조직 R&R 문서(DP1의 "R&R" 페이지)를 못 찾은 게 실측되어(2026-09-11)
# 사용자 확정으로 둘 다 추가.
CONFLUENCE_TEAM_SPACES = ["EnergySW", "ACGEN2", "CWS", "GDRI", "MAG", "HP", "SIACS", "GSP1", "GSP", "DP1"]

# 개인 스페이스(팀 스페이스와 별도로 관리 — 개별 확인 후 화이트리스트로 추가):
# - ~712020fbdcf344af074f33bf0d76cfe893cd15 (AhyoungKim): 처음엔 개별 페이지 4개만 뚫었었는데
#   (인턴 과제/드래프트 등 무관 문서가 44개 중 섞여 있어서), 사용자가 스페이스 전체를 학습
#   대상에 넣기로 확정(2026-08-21) — 노이즈 위험을 감수하고 스페이스 단위로 통째 포함.
# - ~63c74eb4e28ec74364cc217b (Hayool Kim): HUB-Generator/US AC System/FCAS 등 EMS 관련 노트
#   포함 확인 후 학습 대상에 추가(2026-09-11, 사용자 지시).
CONFLUENCE_PERSONAL_SPACES = [
    "~712020fbdcf344af074f33bf0d76cfe893cd15",
    "~63c74eb4e28ec74364cc217b",
]

# 개인 스페이스 키 -> 소유자 이름. 본인 기술노트엔 보통 본인 이름이 본문에 안 나오므로
# (예: Hayool Kim 개인 스페이스의 HUB-Generator/US AC System 등은 작성자 메타데이터로만
# 존재하고 본문엔 이름이 없음 — 실측, 2026-09-11) "참고 자료에 이름이 없다"로 그냥 답을
# 포기하면 사용자가 보기에 이상하다("본인 페이지인데 왜 모른다는 거야?"). build_context가
# 이 스페이스의 문서를 컨텍스트에 넣을 때 "이 문서는 {owner}님의 개인 스페이스에 있는
# 문서"라고 라벨을 붙여서, 이름이 본문에 없어도 "이 사람이 어떤 주제를 다뤘는지"를 답할
# 근거로 쓰게 한다(사용자 확정).
CONFLUENCE_PERSONAL_SPACE_OWNERS = {
    "~712020fbdcf344af074f33bf0d76cfe893cd15": "AhyoungKim",
    "~63c74eb4e28ec74364cc217b": "Hayool Kim",
}

CONFLUENCE_SPACES = CONFLUENCE_TEAM_SPACES + CONFLUENCE_PERSONAL_SPACES
SITE_URL = "growingenergylabs.atlassian.net"

# 1차 검색이 놓친(따라갈) 연관 문서를 몇 개까지 더 조회할지. 순차 조회라 늘릴수록
# 지연이 그대로 늘어난다 — 실측 기반(getConfluencePage 1회 ~0.5~1초)으로 2개면
# 답변 지연을 크게 늘리지 않으면서 대부분의 "본문 없는 페이지" 케이스를 커버.
MAX_LINKED_FOLLOW = 2
# 2-hop으로 찾아낸 페이지의 본문이 진짜로 비어있을 때(다이어그램 첨부파일만 있는 페이지 등)
# 조용히 버리지 않고 이 문구를 text로 채워서 넘긴다 — 답변 합성 단계가 "그런 페이지가
# 있다"는 사실 자체는 인지하고 "본문 없음"이라고 정직하게 답할 수 있게(CLAUDE.md 원칙:
# 본문 없는 페이지는 지어내지 말고 있는 그대로 명시).
_EMPTY_BODY_NOTE = "(문서화 상태: 본문 없음 — 다이어그램 등 첨부파일만 존재. 원본 페이지에서 직접 확인 필요)"
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
_PAGE_ID_IN_URL_RE = re.compile(r'/pages/(\d+)')


def _confluence_space_of(url):
    m = re.search(r"/wiki/spaces/([^/]+)/", url or "")
    return m.group(1) if m else None


def _extract_referenced_page_links(text):
    """본문 markdown 링크 중 우리 스코프(CONFLUENCE_SPACES) 안의 다른 Confluence 페이지를
    가리키는 것만 {page_id: link_text} 형태로 뽑는다. Rovo Search는 본문 텍스트 매칭이라
    본문이 비어있는 페이지(다이어그램 첨부파일만 있는 페이지 등)를 거의 못 찾는데, 그런
    페이지일수록 다른 문서가 참조 링크로 가리키고 있는 경우가 많다 — 1차 결과 본문에서
    그 링크를 따라가 보완한다(실측: "06_EMS+ MCU-MPU Initialization sequence"가 top-10
    검색으로도 안 나왔지만 EMS Project Encyclopedia 페이지 본문의 링크로는 찾아짐)."""
    found = {}
    for link_text, url in _MD_LINK_RE.findall(text or ""):
        if _confluence_space_of(url) not in CONFLUENCE_SPACES:
            continue
        m = _PAGE_ID_IN_URL_RE.search(url)
        if not m:
            continue
        found.setdefault(m.group(1), link_text.strip())
    return found


def _fetch_confluence_page_by_id(token, session_id, page_id, max_chars=None):
    """ARI 파싱 없이 page_id로 직접 getConfluencePage 호출. (title, body) 반환,
    실패/본문없음 시 body는 None. max_chars 생략 시 MAX_PAGE_CHARS(검색 결과 스니펫
    기본 상한) 적용 — ToC 매핑 페이지처럼 전체를 다 읽어야 하는 특수 페이지는
    호출부에서 더 큰 값을 넘긴다(실측 버그: 기본 8000자 상한에 걸려 ToC 후반부
    섹션(17.1 "CAN Map" 등)이 통째로 안 읽혀서 _toc_entries가 못 찾음)."""
    result = _call_tool_in_session(token, session_id, "getConfluencePage", {
        "cloudId": SITE_URL,
        "pageId": page_id,
        "contentFormat": "markdown",
    })
    payload = _tool_text_payload(result)
    if not isinstance(payload, dict):
        return None, None
    title = payload.get("title")
    body = payload.get("body")
    if isinstance(body, dict):
        body = body.get("value") or body.get("markdown")
    if not body or not isinstance(body, str):
        return title, None
    body = _repair_markdown_tables(_clean_confluence_markup(body))
    cap = MAX_PAGE_CHARS if max_chars is None else max_chars
    return title, body[:cap]


# 사용자가 직접 만든 "EMS Project Encyclopedia ToC ↔ Confluence 소스" 매핑 페이지(개인
# 스페이스에 있어 CONFLUENCE_SPACES 필터에는 걸리지만, 저자 본인이 만든 신뢰 가능한
# 네비게이션 인덱스라 예외적으로 항상 로드해서 보조 검색에 쓴다). 실측: "MCU 초기화
# 시퀀스" 질의에서 본문이 빈 페이지(다이어그램 첨부파일만 있음)를 텍스트 검색으로
# top-10까지도 못 찾았는데, 이 ToC의 "[CWS] 06_EMS+ MCU-MPU Initialization sequence"
# 항목으로는 정확히 찾아짐 — 실제 Atlassian Rovo 챗봇도 이 페이지를 참고해 찾아낸 것으로
# 확인(사용자 확정, 2026-08-12: 항상 적용).
_TOC_PAGE_ID = "11427874216"
_TOC_CACHE_TTL = 6 * 3600
_toc_cache = {"entries": None, "ts": 0.0}
_TOC_ENTRY_RE = re.compile(r'^\\?\[([A-Za-z0-9]+)\\?\]\s*(.+)$')
_TOC_ESCAPE_RE = re.compile(r'\\([_\[\]()*.])')


def _toc_entries(token, session_id):
    now = time.time()
    if _toc_cache["entries"] is not None and now - _toc_cache["ts"] < _TOC_CACHE_TTL:
        return _toc_cache["entries"]
    try:
        _, body = _fetch_confluence_page_by_id(token, session_id, _TOC_PAGE_ID, max_chars=200000)
    except Exception:
        body = None
    entries = []
    for line in (body or "").splitlines():
        m = _TOC_ENTRY_RE.match(line.strip())
        if not m:
            continue
        space, title = m.group(1), _TOC_ESCAPE_RE.sub(r'\1', m.group(2).strip()).strip()
        if title:
            entries.append((space, title))
    _toc_cache["entries"] = entries
    _toc_cache["ts"] = now
    return entries


_TOC_WORD_RE = re.compile(r'[a-z0-9가-힣]+')


def _toc_tokenize(text):
    return set(_TOC_WORD_RE.findall((text or "").lower()))


def _toc_idf(entries):
    """토큰별 역문서빈도. ToC 자체가 온통 "AC Gen2" 프로젝트 얘기라 "ac"/"gen2"류
    토큰은 절반 가까운 항목에 다 껴 있어서, 단순 겹침 개수로 스코어링하면 이런 흔한
    토큰이 "can"/"map"처럼 진짜 구별력 있는 토큰을 눌러버린다(실측: "AC Gen2 CAN
    map 목록" 질의에서 "CAN Map" 문서가 top-10 밖으로 밀리고 "ac"/"gen2"만 걸리는
    범용 문서들이 상위를 차지함). 흔한 토큰의 가중치를 낮춰서 이 문제를 없앤다."""
    n = len(entries)
    df = {}
    for _, title in entries:
        for t in _toc_tokenize(title):
            df[t] = df.get(t, 0) + 1
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def _toc_candidates(token, session_id, query, exclude_titles, limit=2):
    """ToC 항목 제목과 질의어 사이 IDF 가중 토큰 겹침으로 관련 후보를 뽑는다(임베딩/LLM
    호출 없음 — 항목 수가 수백 개 수준이라 이 정도로 충분하고 빠르다)."""
    entries = _toc_entries(token, session_id)
    if not entries:
        return []
    q_tokens = {t for t in _toc_tokenize(query) if len(t) >= 2}
    if not q_tokens:
        return []
    idf = _toc_idf(entries)
    scored = []
    seen_titles = set()
    for space, title in entries:
        if space not in CONFLUENCE_SPACES or title in exclude_titles or title in seen_titles:
            continue
        overlap = q_tokens & _toc_tokenize(title)
        if not overlap:
            continue
        seen_titles.add(title)
        scored.append((sum(idf.get(t, 1.0) for t in overlap), space, title))
    scored.sort(key=lambda x: -x[0])
    return [(space, title) for _, space, title in scored[:limit]]


def _resolve_page_id_by_title(token, session_id, space, title):
    try:
        result = _call_tool_in_session(token, session_id, "searchConfluenceUsingCql", {
            "cloudId": SITE_URL,
            "cql": f'space = "{space}" AND title ~ "{title}"',
            "limit": 1,
        })
        payload = _tool_text_payload(result)
        results = payload.get("results") if isinstance(payload, dict) else None
        if results:
            return results[0].get("content", {}).get("id")
    except Exception:
        pass
    return None


MCP_URL = "https://mcp.atlassian.com/v1/mcp"
# Atlassian 엣지(WAF)가 python urllib 기본 User-Agent는 403으로 막는다 — 실측 확인됨
USER_AGENT = "curl/8.5.0"
# credentials.json 안의 키. `claude mcp add ... -s user`로 등록한 서버 이름이 바뀌면 같이 바뀜
_MCP_ENTRY_PREFIX = "atlassian|"


def _find_entry_key(creds):
    for key in creds.get("mcpOAuth", {}):
        if key.startswith(_MCP_ENTRY_PREFIX):
            return key
    return None


def _load_credentials():
    return json.loads(CREDENTIALS_PATH.read_text())


def _save_credentials(creds):
    CREDENTIALS_PATH.write_text(json.dumps(creds, indent=2))


def _refresh_token(entry):
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": entry["refreshToken"],
        "client_id": entry["clientId"],
    }).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        tok = json.load(resp)
    entry["accessToken"] = tok["access_token"]
    entry["refreshToken"] = tok["refresh_token"]
    entry["expiresAt"] = int((time.time() + tok["expires_in"]) * 1000)
    return entry


def _get_access_token():
    creds = _load_credentials()
    key = _find_entry_key(creds)
    if not key:
        raise RuntimeError(
            "credentials.json에 atlassian MCP 토큰이 없습니다. "
            "`claude mcp login atlassian`을 먼저 실행하세요."
        )
    entry = creds["mcpOAuth"][key]

    # 5분 이내로 만료 예정이면 미리 갱신 (매 요청 만료 체크 + 여유 마진)
    if entry.get("expiresAt", 0) - 5 * 60 * 1000 < time.time() * 1000:
        entry = _refresh_token(entry)
        creds["mcpOAuth"][key] = entry
        _save_credentials(creds)

    return entry["accessToken"]


def _parse_sse(body: bytes):
    for line in body.decode("utf-8").splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return None


def _mcp_call(token, method, params=None, session_id=None, is_notification=False):
    payload = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not is_notification:
        payload["id"] = next(_next_request_id)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": USER_AGENT,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(),
                                  headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        sid = resp.headers.get("Mcp-Session-Id")
        body = resp.read()
        return (_parse_sse(body) if body else None), sid


def _start_session(token):
    _, sid = _mcp_call(token, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "qcells-ems-wikibot", "version": "0.1"},
    })
    _mcp_call(token, "notifications/initialized", {}, session_id=sid, is_notification=True)
    return sid


def _call_tool_in_session(token, session_id, tool_name, arguments):
    result, _ = _mcp_call(token, "tools/call", {"name": tool_name, "arguments": arguments}, session_id=session_id)
    return result


def _tool_text_payload(result):
    """tools/call 결과의 content[0].text는 JSON 문자열로 인코딩되어 있다 -> 파싱."""
    if not result or "result" not in result:
        return None
    content = result["result"].get("content", [])
    if not content:
        return None
    try:
        return json.loads(content[0]["text"])
    except Exception:
        return content[0].get("text")


_ARI_CONFLUENCE_PAGE = re.compile(r'ari:cloud:confluence:([^:]+):page/(\d+)$')

# 4000이었을 때 "Energy SW Part R&R" 같은 긴 인원 표가 중간에 잘려서 뒷쪽 인원이
# 답변에서 통째로 누락되는 사고가 실측됨(실제 페이지 길이 4941자). 답변 합성을 이제
# claude -p(큰 컨텍스트 창, 노이즈에 강함)로 전량 전환했으니 로컬 소형 모델 시절 정한
# 이 좁은 값을 유지할 이유가 없어 여유 있게 올림 — limit=3페이지 기준 최대
# 3*8000=24000자로, claude -p 컨텍스트 창엔 여전히 작은 양.
MAX_PAGE_CHARS = 8000

_CUSTOM_TAG_RE = re.compile(r'<custom[^>]*>(.*?)</custom>\s*', re.DOTALL)
_EMOJI_TAG_TEXT_RE = re.compile(r':\w+:')


def _clean_confluence_markup(text):
    """Confluence storage->markdown 변환 결과에 남는 <custom data-type="mention/emoji/date"
    ...>내용</custom> 래퍼를 내용만 남기고 벗겨낸다. 이모지 커스텀 태그(:flag_kr: 등)는 답변에
    의미 없는 노이즈라 통째로 제거한다."""
    def _repl(m):
        inner = m.group(1).strip()
        if _EMOJI_TAG_TEXT_RE.fullmatch(inner):
            return ''
        return inner + ' '
    return _CUSTOM_TAG_RE.sub(_repl, text)


_TABLE_SEP_RE = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')


def _repair_markdown_tables(text):
    """Confluence 표에 세로 병합 셀(rowspan)이 있으면 markdown 변환 시 그 칸이 빈칸도 아니고
    통째로 빠져서, 헤더는 3열인데 병합된 그룹의 멤버 행은 2열만 나오는 식으로 행마다 열 개수가
    달라진다. 게다가 셀 안에 줄바꿈이 있으면 한 논리적 행이 물리적으로 여러 줄에 걸쳐 나온다
    (예: "Energy Control \\n& Monitoring |"). 사람도 헷갈리는 표라 claude -p가 같은 질문에도
    답이 들쭉날쭉했던 것(실측: "Energy SW Part R&R" 인원 수를 물으면 14명/못찾음/18명이 랜덤하게
    나옴)의 근본 원인으로 파악됨 -> 파싱 전에 정규화한다:
    1) 표 영역 안에서 '|'로 시작하지 않는 줄은 이전 줄에 이어붙임(셀 내부 개행 복구)
    2) 헤더보다 열이 부족한 행은 직전 '완전한' 행의 앞쪽 칸으로 채움(병합 셀 forward-fill)
    """
    lines = text.split('\n')
    merged = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('|'):
            merged.append(line)
            in_table = True
        elif in_table and stripped and merged:
            merged[-1] = merged[-1].rstrip() + ' ' + stripped
        else:
            in_table = False
            merged.append(line)

    out = []
    header_cols = None
    prev_full_cells = None
    in_table = False
    for line in merged:
        stripped = line.strip()
        if stripped.startswith('|') and stripped.endswith('|'):
            cells = [c.strip() for c in stripped[1:-1].split('|')]
            if _TABLE_SEP_RE.match(stripped):
                header_cols = (
                    len(out[-1].strip()[1:-1].split('|'))
                    if out and out[-1].strip().startswith('|') else len(cells)
                )
                in_table = True
                prev_full_cells = None
                out.append(line)
                continue
            if in_table and header_cols and prev_full_cells and len(cells) < header_cols:
                missing = header_cols - len(cells)
                cells = prev_full_cells[:missing] + cells
                line = '| ' + ' | '.join(cells) + ' |'
            if in_table and header_cols and len(cells) == header_cols:
                prev_full_cells = cells
            out.append(line)
        else:
            in_table = False
            header_cols = None
            prev_full_cells = None
            out.append(line)
    return '\n'.join(out)


def _fetch_full_confluence_page(token, session_id, ari_id):
    """search 결과의 짧은 스니펫 대신 페이지 전체 본문(markdown)을 가져온다.
    Jira issue나 ARI 파싱 실패 시 None(호출부에서 스니펫으로 폴백)."""
    m = _ARI_CONFLUENCE_PAGE.match(ari_id or "")
    if not m:
        return None
    _, page_id = m.group(1), m.group(2)
    _, body = _fetch_confluence_page_by_id(token, session_id, page_id)
    return body


def rovo_search(query, limit=5, fetch_full_pages=True, two_hop=True, timeout=20):
    """Rovo Search(Jira+Confluence 통합 검색)로 query를 검색해
    [{"title":..., "url":..., "text":..., "type": "page"|"issue"}, ...] 형태로 반환.
    fetch_full_pages=True면 Confluence 결과는 스니펫 대신 getConfluencePage로
    페이지 전체 본문을 받아온다(더 정확하지만 결과당 MCP 호출 1회씩 추가됨).
    two_hop=False면 아래 2-hop 문서 보완(ToC 역조회 + 링크 팔로우)을 건너뛴다 —
    같은 질문에 원본/확장 두 검색어로 rovo_search를 두 번 부르는 호출부(build_context,
    2026-08-19 "gem net id" 케이스 수정)에서 매 호출마다 2-hop까지 돌면 요청이
    5분 타임아웃까지 늘어지는 게 실측되어, 보완 검색 쪽은 이 옵션으로 끈다.
    실패 시(토큰 없음/네트워크 오류 등) 빈 리스트."""
    try:
        token = _get_access_token()
        session_id = _start_session(token)
        result = _call_tool_in_session(token, session_id, "search", {"query": query})
    except Exception as e:
        import sys
        print(f"⚠️  Rovo Search 실패: {e}", file=sys.stderr)
        return []

    inner = _tool_text_payload(result)
    if not isinstance(inner, dict):
        return []

    # fail-safe 방향으로 판단: Jira 이슈는 스페이스 개념이 없으니 항상 통과, Confluence
    # 페이지는 URL에서 스페이스 키를 확인할 수 있고 그게 7개 안에 들 때만 통과시킨다.
    # 오래된 페이지는 `/wiki/pages/viewpage.action?pageId=...` 같은 구형 permalink URL을
    # 써서 스페이스를 URL만으로 못 가릴 때가 있는데(실측: EnergySW 소속인 "2024 CW42 주간
    # 업무"도 이 형식으로 나옴), 이런 경우는 정말로 EnergySW여도 그냥 제외한다 — 가끔 유효한
    # 문서를 놓치는 것보다, 다른 제품 스페이스 문서가 답변에 섞여 들어가는 쪽이 훨씬 위험하다.
    all_results = inner.get("results", [])
    filtered = [
        r for r in all_results
        if r.get("type") == "issue"
        or (r.get("type") == "page" and _confluence_space_of(r.get("url")) in CONFLUENCE_SPACES)
    ]
    # GSP1(Global SW PM)은 클라우드/웹 콘솔이나 조직 관리 같은 다른 레이어를 다룰 때가 있다
    # (실측: TOU 질문에서 GSP1의 "PRD - Time of Use"가 Rovo 관련도 상위로 나와 EnergySW의 실제
    # 구현 문서를 밀어내고, 답변이 Fleet 웹 콘솔 권한/워크플로우 위주로 나온 사고 발생) — GSP1만
    # 안정 정렬로 맨 뒤로 미룬다.
    #
    # 처음엔 "EnergySW를 맨 앞으로 승격"으로 고쳤었는데, 이건 관련성과 무관하게 EnergySW 소속이면
    # 무조건 앞세우는 방식이라 부작용이 실측됨: "gem net id ffff" 질의에서 Rovo가 1위로 정확히
    # 찾아준 HP 스페이스의 "GEM / MI Controller"가, 질의어에 "gem"만 어쩌다 걸린 EnergySW 주간
    # 업무 보고서 3개에 밀려 limit=5 밖으로 잘려나가는 문제가 재현됨(2026-08-19). GSP1만 최소한으로
    # 뒤로 미루고 나머지는 Rovo의 원래 관련도 순서를 그대로 신뢰하는 쪽으로 수정.
    filtered.sort(key=lambda r: 1 if (
        r.get("type") == "page" and _confluence_space_of(r.get("url")) == "GSP1"
    ) else 0)
    results = filtered[:limit]

    # getConfluencePage를 ThreadPoolExecutor로 병렬화했다가(순차 ~3.6초 -> 병렬 ~1.5초로
    # 단축은 됐음) 같은 세션에서 동시에 여러 요청을 보내면 요청/응답이 서로 뒤섞이는 사고가
    # 실측됨 — JSON-RPC 요청 id를 고유값으로 바꿔도(itertools.count) 완전히 해결되지 않고
    # "Energy SW Part R&R"이라고 라벨은 맞는데 본문 내용은 "Cloud SW Part R&R" 것이 섞여
    # 들어오는 등 재발함(원인: 우리 코드 밖, MCP 서버 또는 세션 자체의 동시 요청 처리 문제로
    # 추정 — 더 파기 전에 일단 안전한 순차 방식으로 되돌림). 인원/조직 데이터처럼 실수가
    # 그대로 신뢰 문제로 이어지는 내용을 다루므로, 2초 안팎의 속도 이득보다 정확성이 우선.
    items = []
    for r in results:
        text = r.get("text", "")
        if fetch_full_pages and r.get("type") == "page":
            try:
                full_text = _fetch_full_confluence_page(token, session_id, r.get("id", ""))
                if full_text:
                    text = full_text
            except Exception as e:
                import sys
                print(f"⚠️  getConfluencePage 실패({r.get('title')}): {e}", file=sys.stderr)
        items.append({
            "title": r.get("title", "(제목 없음)"),
            "url": r.get("url", ""),
            "text": text,
            "type": r.get("type", "page"),
        })

    # 2-hop: 1차 결과가 놓친 문서를 최대 MAX_LINKED_FOLLOW개까지 보완한다(사용자 확정,
    # 항상 적용). 두 경로를 합쳐서 예산을 공유하되 ToC 매핑 조회(①)를 먼저 채운다 —
    # 사용자가 직접 만든 큐레이션 인덱스라 관련도가 더 높고(실측: "CAN Map" 질의에서
    # ToC는 정확히 찾는데 링크 팔로우는 매번 다른 무관한 페이지 2개로 예산을 다 써버려
    # ToC가 차례를 못 받는 문제가 있었음), 남는 자리를 링크 팔로우(②)가 채운다:
    #   ① 사용자가 직접 만든 ToC 매핑 페이지에서 질의어와 겹치는 항목을 찾아 제목으로
    #      역조회한다(_toc_candidates/_resolve_page_id_by_title 함수 docstring 참고).
    #   ② 1차 결과 본문 안에 실제 markdown 링크로 언급된 다른 페이지를 따라간다.
    if fetch_full_pages and two_hop:
        existing_ids = {
            m.group(1) for it in items
            if (m := _PAGE_ID_IN_URL_RE.search(it.get("url", "")))
        }
        existing_titles = {it["title"] for it in items}
        added = 0

        for space, toc_title in _toc_candidates(token, session_id, query, existing_titles, limit=MAX_LINKED_FOLLOW):
            pid = _resolve_page_id_by_title(token, session_id, space, toc_title)
            if not pid or pid in existing_ids:
                continue
            try:
                real_title, body = _fetch_confluence_page_by_id(token, session_id, pid)
            except Exception as e:
                import sys
                print(f"⚠️  ToC 연관 문서 조회 실패({toc_title}): {e}", file=sys.stderr)
                continue
            if real_title is None:
                continue
            items.append({
                "title": real_title or toc_title,
                "url": f"/wiki/pages/viewpage.action?pageId={pid}",
                "text": body or _EMPTY_BODY_NOTE,
                "type": "page",
            })
            existing_ids.add(pid)
            existing_titles.add(real_title or toc_title)
            added += 1

        remaining = MAX_LINKED_FOLLOW - added
        if remaining > 0:
            referenced = {}
            for it in items:
                for pid, link_text in _extract_referenced_page_links(it.get("text", "")).items():
                    if pid not in existing_ids and pid not in referenced:
                        referenced[pid] = link_text
            for pid, link_text in list(referenced.items())[:remaining]:
                try:
                    real_title, body = _fetch_confluence_page_by_id(token, session_id, pid)
                except Exception as e:
                    import sys
                    print(f"⚠️  연관 문서 후속 조회 실패({link_text}): {e}", file=sys.stderr)
                    continue
                if real_title is None:
                    continue  # 페이지 자체를 못 가져옴(삭제/권한 등) — 본문만 빈 경우와 구분
                items.append({
                    "title": real_title or link_text,
                    "url": f"/wiki/pages/viewpage.action?pageId={pid}",
                    "text": body or _EMPTY_BODY_NOTE,
                    "type": "page",
                })
                existing_ids.add(pid)
    return items


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "TOU 로직"
    for item in rovo_search(q):
        print(f"- [{item['type']}] {item['title']} :: {item['url']}")
