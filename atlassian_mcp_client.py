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
CONFLUENCE_SPACES = ["EnergySW", "ACGEN2", "CWS", "GDRI", "MAG", "HP", "SIACS", "GSP1"]


def _confluence_space_of(url):
    m = re.search(r"/wiki/spaces/([^/]+)/", url or "")
    return m.group(1) if m else None
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
    cloud_id, page_id = m.group(1), m.group(2)
    result = _call_tool_in_session(token, session_id, "getConfluencePage", {
        "cloudId": cloud_id,
        "pageId": page_id,
        "contentFormat": "markdown",
    })
    payload = _tool_text_payload(result)
    if not payload:
        return None
    body = payload.get("body") if isinstance(payload, dict) else None
    if isinstance(body, dict):
        body = body.get("value") or body.get("markdown")
    if not body or not isinstance(body, str):
        return None
    body = _repair_markdown_tables(_clean_confluence_markup(body))
    return body[:MAX_PAGE_CHARS]


def rovo_search(query, limit=5, fetch_full_pages=True, timeout=20):
    """Rovo Search(Jira+Confluence 통합 검색)로 query를 검색해
    [{"title":..., "url":..., "text":..., "type": "page"|"issue"}, ...] 형태로 반환.
    fetch_full_pages=True면 Confluence 결과는 스니펫 대신 getConfluencePage로
    페이지 전체 본문을 받아온다(더 정확하지만 결과당 MCP 호출 1회씩 추가됨).
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
    # EnergySW는 실제 임베디드 EMS 구현을 다루는 메인 스페이스, GSP1(Global SW PM) 등 나머지는
    # 클라우드/웹 콘솔이나 조직 관리 같은 다른 레이어를 다룰 때가 있다(실측: TOU 질문에서 GSP1의
    # "PRD - Time of Use"가 Rovo 관련도 상위로 나와 EnergySW의 실제 구현 문서를 밀어내고, 답변이
    # Fleet 웹 콘솔 권한/워크플로우 위주로 나온 사고 발생). 필터 통과한 결과 중 EnergySW 소속
    # Confluence 페이지를 안정 정렬로 맨 앞에 오도록 재배치해서 limit 안에 우선 들어가게 한다 —
    # 다른 스페이스가 완전히 배제되는 건 아니고, EnergySW에 관련 문서가 없을 때만 밀려서 들어온다.
    filtered.sort(key=lambda r: 0 if (
        r.get("type") == "page" and _confluence_space_of(r.get("url")) == "EnergySW"
    ) else 1)
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
    return items


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "TOU 로직"
    for item in rovo_search(q):
        print(f"- [{item['type']}] {item['title']} :: {item['url']}")
