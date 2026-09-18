#!/usr/bin/env python3
"""
Jira 연동 전담 모듈(2026-09-18 분리) — atlassian_mcp_client.py는 원래 Confluence
검색/작성자 판별 위주였는데, 사용자 요청("jira를 연동하면 사람-계정이
매칭될수있을것 같애", 이어서 "Jira 티켓도 연동할수있어?")으로 Jira 쪽 기능이
늘면서 그 파일이 다시 거대해지지 않도록 별도 파일로 뺐다 — query_expansion.py를
분리한 것과 같은 이유(wiki_chat_server.py의 관련 주석 참고).

세션/토큰 관리(액세스 토큰 발급, MCP 세션 시작, 도구 호출, 응답 파싱)는 Confluence와
Jira가 완전히 공유하는 저수준 배관이라 중복 구현하지 않고 atlassian_mcp_client.py
것을 그대로 가져다 쓴다 — 이 모듈은 "Jira에 어떤 질문을 던지고 결과를 어떻게 해석할지"
에만 집중한다.
"""

from atlassian_mcp_client import (
    _get_access_token, _start_session, _call_tool_in_session, _tool_text_payload, SITE_URL,
)


def lookup_person_account_id(name, timeout=15):
    """Atlassian 사용자 디렉터리(lookupJiraAccountId 도구)에서 name과 정확히 매칭되는
    계정을 찾는다. (account_id, display_name) 리스트 반환 — 실패/0건이면 빈 리스트.

    atlassian_mcp_client.find_author_id_by_title()의 Confluence CQL 제목검색보다
    훨씬 정확하다 — 그 사람이 쓴 것으로 "추정"되는 문서 패턴을 역산하는 게 아니라
    조직 계정 디렉터리 자체를 직접 찾기 때문이다. 실측(2026-09-18): "정지석"으로 이
    함수는 정확히 1명(실제 EnergySW 최다 작성자로 이미 별도 확인된 그 계정)을
    정확히 찾아내는데, 옛 CQL 제목검색+다수결 방식은 같은 이름에 대해 전혀 무관한
    사람을 잘못 골랐다(사용자가 "정지석 프로가 creator인 페이지 몇 건인지
    확인해줘"로 재현).

    다만 전원이 이걸로 찾아지진 않는다 — 실측: "노은철"/"박민석"/"정지석"은 정확히
    1건이 찾아지지만 "신동진"/"고윤석"/"장승혁"은 0건(해당 계정의 Atlassian
    프로필에 한글 이름/닉네임이 등록 안 돼 있는 것으로 추정 — 선택 입력 필드라
    사람마다 채웠는지가 다름). 그래서 0건이면 find_author_id_by_title()이 CQL
    제목검색 경로로 넘어간다."""
    try:
        token = _get_access_token()
        session_id = _start_session(token)
        result = _call_tool_in_session(token, session_id, "lookupJiraAccountId", {
            "cloudId": SITE_URL,
            "searchString": name,
        })
    except Exception as e:
        import sys
        print(f"⚠️  사용자 디렉터리 조회 실패({name}): {e}", file=sys.stderr)
        return []
    payload = _tool_text_payload(result)
    users = (((payload or {}).get("data") or {}).get("users") or {}).get("users") or []
    return [(u.get("accountId"), u.get("displayName")) for u in users if u.get("accountId")]


def search_jira_mentions(name, limit=25, timeout=15):
    """Jira 이슈 전문(제목+본문+댓글)에서 name을 검색해(JQL `text ~`), 매치된
    이슈들의 assignee를 계정별로 묶는다 — [(account_id, display_name,
    [issue_summary, ...]), ...] 반환(실패/0건이면 빈 리스트).

    atlassian_mcp_client.find_author_id_by_title()의 Confluence 제목검색이 못
    찾는 사람을 상당수 메워준다 — Confluence는 title ~ 만 훑어서 "그 사람 이름이
    제목에 박힌 문서"가 없으면 증거 자체가 없는데, Jira의 text ~ 는 본문/댓글까지
    훑고 한글 검색도 잘 된다(사용자 제안, 2026-09-18: "jira를 연동하면 사람-계정이
    매칭될수있을것 같애" — 실측으로 확인: "신동진"/"이영웅"/"이선정"/"김아영"은
    검색된 이슈의 assignee가 압도적으로 본인 계정에 몰려서 바로 판별됨, "이선정"은
    이 방법으로 처음 계정을 찾음).

    **다만 이 신호도 다수결로 그냥 믿으면 안 된다** — 실측: "고윤석"으로 검색하면
    assignee 1위가 본인(Yunseok Ko)이 아니라 한영웅(Youngwoong Han)이었다. 그
    이름이 이슈 안에 언급되는 이유가 "assignee 본인 얘기"만이 아니라 "다른 사람
    티켓에 리뷰어/멘션 대상으로 코멘트를 남김"일 수도 있어서다(예: DevOps/CI
    업무처럼 여러 사람 티켓을 가로질러 리뷰하는 역할). 그래서 여기서 최댓값을
    바로 채택하지 않고, find_author_id_by_title()이 이 결과를 Confluence 후보와
    합쳐서 query_expansion.pick_person_account()의 LLM 판단에 넘긴다."""
    try:
        token = _get_access_token()
        session_id = _start_session(token)
        result = _call_tool_in_session(token, session_id, "searchJiraIssuesUsingJql", {
            "cloudId": SITE_URL,
            "jql": f'text ~ "{name}"',
            "maxResults": limit,
            "fields": ["summary", "assignee"],
        })
    except Exception as e:
        import sys
        print(f"⚠️  Jira 언급 검색 실패({name}): {e}", file=sys.stderr)
        return []
    payload = _tool_text_payload(result)
    issues = payload.get("issues", []) if isinstance(payload, dict) else []
    names = {}
    summaries_by_account = {}
    for issue in issues:
        fields = issue.get("fields", {}) or {}
        assignee = fields.get("assignee") or {}
        account_id = assignee.get("accountId")
        if not account_id:
            continue
        names[account_id] = assignee.get("displayName")
        summaries_by_account.setdefault(account_id, []).append(fields.get("summary", ""))
    return [(aid, names[aid], summaries_by_account[aid]) for aid in names]


def search_jira_assigned(account_id, limit=100, timeout=15):
    """특정 인물(account_id)에게 assignee로 할당된, 아직 안 끝난 Jira 이슈를 찾는다
    — (items, is_complete) 튜플. items는 [{"key","title","status","priority",
    "duedate","url"}, ...], is_complete는 이 목록이 실제로 전부인지(더 잘려나간
    게 없는지) 여부.

    search_jira_mentions()(이름이 본문/댓글에 언급된 이슈, 인물 accountId
    역추적용)와 다른 목적이다 — 저건 text ~ 로 찾아서 다수결/LLM 판단이 필요한
    약한 증거지만, 이건 assignee = accountId로 정확히 매칭하는 강한 필드라서
    다수결/LLM 판단이 필요 없다(사용자 요청, 2026-09-18: "Jira 티켓도
    연동할수있어? 심철로 할당된 jira 티켓 찾아줘"). accountId만 정확하면(이미
    atlassian_mcp_client.find_author_id_by_title()로 확보) 그대로 신뢰 가능.

    **완료/취소 이슈는 뺀다** — 처음엔 상태 필터 없이 최신 업데이트순 25건만
    가져왔는데, 심철 계정으로 실측하니 전체(모든 상태 통산) 1,246건 중 25건만
    보여주면서 "총 25건 확인되었습니다"라고 마치 전체인 것처럼 답해버리는 사고가
    남(사용자가 "너무 적게 표시되는데, 더 많을 것 같은데"로 재현, 2026-09-18).
    "할당된 티켓, 급한 것 정렬"이라는 질문 의도 자체가 "지금 안 끝난 일이
    뭔지"이지 "역대 전체 이력"이 아니므로, `statusCategory != Done AND status !=
    Cancelled`로 좁힌다(이 org에선 "Cancelled" 상태가 Jira 기본 category 분류상
    Done이 아니라 "To Do" 카테고리에 잡혀서 statusCategory 필터만으론 안 빠짐 —
    실측 확인, status 이름으로 따로 빼야 함). 이렇게 좁히면 진짜 "급한 것"만
    남아서(심철 기준 16건) limit=100 한 페이지 안에 거의 항상 다 들어온다.

    정렬은 "급한 순"(우선순위 desc → 마감일 asc → 최신 업데이트순)으로 — 예전
    "최신 업데이트순" 정렬은 "급한것 정렬" 요청과 안 맞았다(사용자 실측 지적).

    끝으로, limit을 다 채워서 돌아오면(더 있을 수 있다는 뜻) is_complete=False로
    표시한다 — search_by_creator()의 200건 cap 때와 같은 원칙: "완전하다"고 확신할
    수 없으면 그렇게 말하지 않는다."""
    try:
        token = _get_access_token()
        session_id = _start_session(token)
        jql = (
            f'assignee = "{account_id}" AND statusCategory != Done AND status != Cancelled '
            f'ORDER BY priority DESC, duedate ASC, updated DESC'
        )
        result = _call_tool_in_session(token, session_id, "searchJiraIssuesUsingJql", {
            "cloudId": SITE_URL,
            "jql": jql,
            "maxResults": limit,
            "fields": ["summary", "status", "priority", "duedate"],
        })
    except Exception as e:
        import sys
        print(f"⚠️  할당된 Jira 이슈 검색 실패: {e}", file=sys.stderr)
        return [], False
    payload = _tool_text_payload(result)
    issues = payload.get("issues", []) if isinstance(payload, dict) else []
    items = []
    for issue in issues:
        fields = issue.get("fields", {}) or {}
        key = issue.get("key", "")
        items.append({
            "key": key,
            "title": fields.get("summary", "(제목 없음)"),
            "status": (fields.get("status") or {}).get("name", ""),
            "priority": (fields.get("priority") or {}).get("name", ""),
            "duedate": fields.get("duedate"),
            "url": f"https://{SITE_URL}/browse/{key}" if key else "",
        })
    is_complete = len(items) < limit
    return items, is_complete
