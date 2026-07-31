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

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
TOKEN_ENDPOINT = "https://cf.mcp.atlassian.com/v1/token"
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
        payload["id"] = 1

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

MAX_PAGE_CHARS = 4000


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

    results = inner.get("results", [])[:limit]

    # getConfluencePage 호출은 서로 독립적인 MCP 요청(같은 session_id를 읽기만 함)이라
    # 순차로 하나씩 기다릴 이유가 없다 — 병렬로 쏴서 가장 느린 것 하나만 기다리면 된다
    # (실측: limit=3 기준 순차 ~3.6초 -> 병렬로 단축).
    def _fetch(r):
        if fetch_full_pages and r.get("type") == "page":
            try:
                return _fetch_full_confluence_page(token, session_id, r.get("id", ""))
            except Exception as e:
                import sys
                print(f"⚠️  getConfluencePage 실패({r.get('title')}): {e}", file=sys.stderr)
        return None

    with ThreadPoolExecutor(max_workers=max(1, len(results))) as pool:
        full_texts = list(pool.map(_fetch, results))

    items = []
    for r, full_text in zip(results, full_texts):
        items.append({
            "title": r.get("title", "(제목 없음)"),
            "url": r.get("url", ""),
            "text": full_text or r.get("text", ""),
            "type": r.get("type", "page"),
        })
    return items


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "TOU 로직"
    for item in rovo_search(q):
        print(f"- [{item['type']}] {item['title']} :: {item['url']}")
