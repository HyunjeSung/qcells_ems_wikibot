# Qcells EMS 위키봇

Confluence 라이브 검색(Atlassian Rovo Search 우선, CQL 폴백)을 배경지식으로 답하는
ChatGPT 스타일 웹 챗봇. 사이드바에서 과거 대화 기록을 열람할 수 있다.

## 구성

- `wiki_chat_server.py` — Flask 백엔드. 검색 → 컨텍스트 구성 → 답변 합성 파이프라인.
- `atlassian_mcp_client.py` — 공식 [Atlassian Rovo MCP Server](https://mcp.atlassian.com/v1/mcp)
  클라이언트. Claude Code CLI가 `claude mcp login atlassian`으로 받아둔 OAuth 토큰을 재사용한다.
- `wiki_chat_history.py` — SQLite 기반 대화 기록 저장(`conversations`/`messages`).
- `confluence_to_text.py` — Confluence storage format(XHTML) → 텍스트 변환.
- `search_query_utils.py` — 검색어 정제용 정규식 헬퍼(CQL 폴백 검색에서 사용).
- `static/index.html` — 프론트엔드(바닐라 JS, 사이드바 + 마크다운/mermaid 렌더링).

## 답변 합성

`claude` CLI(Claude Code, `claude -p`)가 설치돼 있으면 우선 사용한다 — 로그인된 Claude
Pro/Max 세션을 재사용하므로 별도 API 과금이 없다. CLI가 없거나 실패하면 `ANTHROPIC_API_KEY`
(Anthropic API, 유료)로 폴백하고, 그것도 없으면 로컬 [Ollama](https://ollama.com)로 폴백한다.

검색 단계에서도 `claude -p`로 검색어를 확장한다(동의어/영문 전문용어 추가, 직전 대화 맥락을
반영해 모호한 단어를 구체화) — Rovo Chat이 검색 전에 스스로 검색어를 재구성하는 동작을
모사한 것.

## 실행

```bash
pip install -r requirements.txt
python3 wiki_chat_server.py --port 8010
```

`http://localhost:8010` 접속. Confluence 접근은 `claude mcp login atlassian`으로 미리 OAuth
로그인이 되어 있어야 한다(Claude Code CLI 필요). 위 로그인 없이 CQL 폴백만 쓰려면
`.env.confluence`에 `ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN`/`ATLASSIAN_BASE_URL`을 설정한다
(`.gitignore`에 포함되어 있으니 커밋되지 않는다).

## 참고

- 답변 소스는 라이브 Confluence 페이지로 한정되어 있다(이 리포에는 위키 문서 자체가 포함돼
  있지 않다 — 회사별로 Confluence 스페이스 키만 `wiki_chat_server.py`의 `CONFLUENCE_SPACES`에
  맞게 바꿔서 쓰면 된다).
- 개발 서버(Flask dev server)이며 인증이 없다. 신뢰할 수 있는 네트워크 안에서만 노출할 것.
