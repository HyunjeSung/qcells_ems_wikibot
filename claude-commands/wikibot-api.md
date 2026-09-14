# /wikibot-api

`/wikibot` 웹앱(포트 8010, `claude -p` 합성)을 거치지 않고, **이 Claude Code 세션에 이미 연결된
`atlassian` MCP를 직접 써서** Confluence(EnergySW/ACGEN2/CWS/GDRI/MAG/HP/SIACS/GSP1 8개 스페이스)를
검색하고, 그 결과를 지금 하고 있는 코드 작업에 바로 반영합니다. "위키봇에 물어보고 → 답변 복사 →
여기 붙여넣기"의 2단계를 1단계로 줄이는 것이 목적입니다.

User provided arguments: $ARGUMENTS

---

## 실행 절차

$ARGUMENTS가 비어있으면 무엇을 찾아서 어떤 코드 작업에 반영할지 되물어보고 종료하세요.

### 1단계: 검색

`mcp__atlassian__search`(Rovo Search)로 `$ARGUMENTS`를 검색하세요. `cloudId`는 액세스 토큰에서
자동 유도되므로 별도로 조회할 필요 없습니다.

결과가 부실하거나 검색 대상을 이 8개 스페이스로 명시적으로 좁혀야 할 때만 CQL로 폴백하세요:
1. `mcp__atlassian__getAccessibleAtlassianResources`로 `cloudId` 조회
2. `mcp__atlassian__searchConfluenceUsingCql`에 아래 CQL로 검색 (title 우선, 결과 없으면 text로 재시도):
   ```
   space in ("EnergySW","ACGEN2","CWS","GDRI","MAG","HP","SIACS","GSP1") and type = page and title ~ "<검색어>"
   ```

### 2단계: 본문 확보

상위 결과(보통 2~3개)에 대해 `mcp__atlassian__getConfluencePage`(`contentFormat: "markdown"`)로
전체 본문을 가져오세요.

**주의**: Confluence의 세로 병합 셀(rowspan) 표는 markdown 변환 시 헤더보다 열이 부족한 행으로
깨져 나올 수 있습니다(위키봇 개발 중 실측된 이슈, `project_wikibot_architecture` 메모리 참고) —
그런 행을 만나면 비어있는 앞쪽 칸은 직전 완전한 행의 같은 위치 값을 이어받는 것으로 해석하세요.

### 3단계: 코드 작업에 반영

가져온 문서 내용을 그대로 근거로 삼아 사용자가 요청한 코드 작업(버그 수정/리팩터/신규 구현 등)을
바로 수행하세요 — `/wikibot`처럼 별도의 챗봇식 답변을 합성하는 게 목적이 아니라, 검색된 원문을
읽고 Edit/Write 등으로 코드에 직접 반영하는 것이 목적입니다.

### 4단계: 출처 명시

작업 결과를 보고할 때 참고한 문서 제목과 URL을 출처로 함께 밝히세요.

---

## 주의사항

- **답변 근거는 Confluence 검색 결과로만 한정합니다.** 검색(Rovo Search)과 CQL 폴백까지 다
  해봤는데도 답을 못 찾았다면, 로컬 저장소의 소스코드를 grep/Read해서 대신 채우지 말고 **"Confluence
  문서에서 근거를 찾지 못했다"**고 그대로 보고하고 종료하세요. 소스코드는 Confluence 문서가 최신이
  아니거나 실제 구현과 다를 수 있어 신뢰 기준이 다르므로, 섞어서 답하면 어느 쪽 근거인지 불명확해짐
  (사용자가 2026-09-14 확정한 방침). 코드 작업(3단계)에 반영할 때도 소스코드는 "어디에 반영할지"
  찾는 용도로만 Read/Grep하고, "무엇을 반영할지"의 근거는 항상 Confluence 문서 쪽이어야 합니다.
- **로컬 위키(`docs/*.md`)는 쓰지 않습니다** — `/wikibot`과 동일하게 Confluence 라이브 검색만
  근거로 삼습니다(로컬 위키가 무관한 범용 문서를 끌어와 답변을 오염시키는 문제 때문에 사용자가
  답변 소스에서 배제하기로 확정한 결정, `project_wikibot_architecture` 메모리 참고).
- 이 경로는 위키봇 웹앱의 `atlassian_mcp_client.py`(OAuth 토큰 재사용, 병렬화 롤백 등)를 전혀
  거치지 않는 별개의 경로입니다 — 이 세션 자체의 `atlassian` MCP 연결을 씁니다. 같은 Atlassian
  계정 권한 범위를 쓰지만 코드 경로도, 알려진 버그(동시성 이슈 등)도 서로 무관합니다.
- 용도 구분: `/wikibot`은 "브라우저에서 빠르게 물어보고 보기"용, `/wikibot-api`는 "찾은 내용을
  지금 세션의 코드 작업에 바로 반영"용입니다.
