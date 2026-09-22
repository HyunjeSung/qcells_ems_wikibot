#!/usr/bin/env python3
"""Rovo Search 직전에 검색어를 보강하는 `claude -p` 프롬프트 엔지니어링 전담 모듈.

wiki_chat_server.py의 답변 합성 로직과는 독립적인 관심사(검색어 확장)라 별도
파일로 분리했다 — 검색어 확장 규칙이 늘어날 때마다 wiki_chat_server.py를
더 거대하게 만들지 않기 위함.
"""

import functools
import json
import re
import shutil
import subprocess

from usage_guard import looks_like_usage_limit_error, mark_usage_exhausted


def _claude_cli_available():
    return shutil.which("claude") is not None


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
# claude -p는 같은 prompt를 넣어도 매번 같은 키워드를 뽑아주지 않는다(LLM 샘플링 비결정성) —
# 실측: "Hayool Kim이 누구야"를 다른 시점에 두 번 물었는데 확장 키워드가 달라져서 Rovo
# Search 상위 3개가 완전히 다르게 잡히고, 그 결과 풍부한 답변 vs "참고 자료 없음" 답변으로
# 크게 갈리는 사고가 재현됨(2026-09-11). 같은 질문(+같은 대화 맥락)엔 항상 같은 확장
# 키워드를 쓰도록 prompt 문자열을 키로 캐시한다 — 사용자가 "매번 답이 달라지는 것보다
# 일관된 게 낫다"고 확정. 캐시는 프로세스 메모리에만 있어서 서비스 재시작 시 초기화된다
# (디스크 영속화는 지금은 불필요 — 서비스가 거의 재시작되지 않고, 재시작 후 최신 문서가
# 반영된 새 캐시가 쌓이는 쪽이 오히려 낫다).
@functools.lru_cache(maxsize=1000)
def _expand_search_query_llm_call(prompt, timeout=30):
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        # 시스템 프롬프트를 안 주면 기본 Claude Code 에이전트 페르소나가 실행돼서
        # 단순 키워드 추출 대신 "이 요청을 어떻게 처리할까" 하고 헤매다 느려지거나
        # (실측: 20초+ 타임아웃) 엉뚱한 응답(가상의 Bash 실행 서술 등)을 내놓는다.
        # 최소한의 역할 지정 + 도구 완전 비활성화로 순수 JSON 완성만 하게 만든다.
        "--system-prompt",
        '너는 검색어 JSON만 출력하는 도구다. 반드시 '
        '{"core": "...", "keywords": ["...", ...], "person": "..." 또는 null, '
        '"wants_person_stats": true 또는 false} '
        "형식 하나만 출력하고, 코드펜스나 다른 설명은 절대 붙이지 마라.",
        "--tools", "",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if data.get("is_error"):
            if looks_like_usage_limit_error(data.get("result")):
                mark_usage_exhausted(data.get("result"))
            return None
        return _parse_expansion(data["result"])
    except Exception:
        return None


# person 필드 조사 제거 안전망(2026-09-18 추가) — <instructions>에 조사 제거
# 규칙을 명시하고 few-shot도 추가했지만, claude -p는 같은 지시에도 매번 순종하지
# 않는다(이 파일 전체에 반복되는 샘플링 비결정성 문제, 위 주석들 참고). 실측:
# "심철로된 jira티켓 검색..."을 5번 반복하면 4번은 person="심철"로 정확히
# 나오지만 1번은 person="심철로"(조사 "로"가 안 떨어짐)로 나와서 그 계정을 영영
# 못 찾았다(사용자가 "오타 아닌데"라며 재현, 2026-09-18). **정규식으로 조사를
# 떼는 안전망을 처음엔 추가했다가 사용자가 반려함**("이렇게 하드코딩하지말고,
# 프롬프트엔지니어링을 잘해서 키워드를 뽑아내는 라이브러리있니?") — 실제로
# "은/는/이/가/을/를" 같은 흔한 조사는 "지은"/"수은"처럼 실제 이름 끝음절과도
# 겹쳐서, 정규식으로 떼면 없던 오탐을 새로 만들 위험이 있었다(이 파일 위쪽
# person 필드 지시문/few-shot 개선으로 대응 — 실측 케이스를 <examples>에 추가).
# 그래도 claude -p는 확률적이라 100% 보장은 안 되므로, 실패 시엔 아래
# find_author_id_by_title() 호출부(wiki_chat_server.py)에서 **같은 LLM을 캐시
# 우회하고 한 번 더 호출하는 재시도**로 대응한다 — 문자열을 우리가 손대는 대신
# LLM 자체의 샘플링 비결정성을 재시도로 활용하는 것("정규식으로 고치기"가 아니라
# "다시 물어보기").
def _expand_search_query_llm_call_nocache(prompt, timeout=30):
    """_expand_search_query_llm_call()과 동일하지만 lru_cache를 우회한다 — 재시도
    전용. 같은 prompt로 그냥 다시 부르면 캐시가 그대로 돌려줘서(같은 실패를 또
    반환) 재시도 의미가 없으므로, 데코레이터가 감싸기 전의 원본 함수
    (`.__wrapped__`)를 직접 호출해 새 claude -p 샘플을 받는다."""
    return _expand_search_query_llm_call.__wrapped__(prompt, timeout)


def _parse_expansion(raw_text):
    """모델 출력에서 {"core": "...", "keywords": [...], "person": "..."/null,
    "wants_person_stats": true/false}를 뽑아 (core, keywords_str, person,
    wants_person_stats)로 반환한다. core가 비어있으면 keywords 첫 항목으로 대체
    (모델이 core를 빠뜨려도 완전히 못 쓰게 되지 않도록). person은 질문이 특정
    인물 한 명을 콕 집어 묻는 게 아니면 빈 문자열/null/누락 다 None으로 정규화한다
    (build_context()가 이 필드만 보고 작성자 검색 앵커를 잡으므로, 모호하면 None 쪽이
    안전 — 아래 person 필드 설명 참고). wants_person_stats는 누락/비boolean이면
    False로 정규화한다(안전한 기본값 — 이 필드가 False로 잘못 나와도 기존 정규식
    안전망(wiki_chat_server.py의 _PERSON_DOC_COUNT_RE 등)이 여전히 병행 체크되므로
    완전히 못 잡히지는 않는다, 아래 wants_person_stats 필드 설명 참고).

    지시에도 불구하고 ```json 코드펜스나 앞뒤 설명을 붙이는 경우가 있어서,
    첫 '{'~마지막 '}' 구간만 잘라 파싱한다(관대한 파싱 — 실패하면 그냥 폴백)."""
    start, end = raw_text.find("{"), raw_text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(raw_text[start:end + 1])
    except Exception:
        return None
    keywords = obj.get("keywords")
    if not isinstance(keywords, list):
        return None
    cleaned = [str(k).strip() for k in keywords if str(k).strip()]
    if not cleaned:
        return None
    core = str(obj.get("core") or "").strip() or cleaned[0]
    person = str(obj.get("person") or "").strip() or None
    wants_person_stats = obj.get("wants_person_stats") is True
    return core, " ".join(cleaned), person, wants_person_stats


# 프롬프트를 <섹션> 태그로 구조화하고, 규칙을 줄글로 설명하는 대신 실제 실패
# 사례 기반 few-shot 예시로 보여준다 — 정형화된 변형 생성 작업은 모델이 규칙
# 문장을 "해석"하는 것보다 예시를 "패턴 매칭"할 때 더 안정적으로 따라온다
# (일반 프롬프트 엔지니어링 관행, 2026-09-14 재작성). 예시 자체가 문서화 역할도
# 해서 "왜 이렇게 뽑아야 하는지"의 사고 사례를 코드 옆에 남겨둠:
# - DeviceManager/BMS: 언어·약어 축 (동의어/영문 확장)
# - 홍길동: 인물 표기 축 (로마자/사내 이메일 규칙/붙여쓰기 — "프로/님/씨" 같은 범용
#   호칭 접미사는 절대 붙이지 않는다, 아래 참고)
# - gem net id: 도메인 모호성 축 — 예전에 SECS/GEM·GPON 등 엉뚱한 산업표준으로
#   새서 검색을 망쳤던 사고(2026-08-19)의 재발 방지용 반례
# - 로테이션(대화맥락 포함): 맥락 의존 중의성 해소 축 — 맥락 없이 로그 로테이션
#   기능으로 오매칭됐던 사고의 재발 방지용 반례
# - 장승혁(슬래시 표기+호칭+의문사): "core" 필드가 문법적 잡음(조사/호칭/의문사/
#   구분자)을 걸러내는 것을 보여주는 예시. 2026-09-17 이전엔 이런 잡음을
#   wiki_chat_server.py의 정규식 불용어 목록(_GENERIC_KO_WORDS)에 하나씩
#   손으로 추가하는 방식이었는데("프로"/"님"/"씨"/"누구야"/"/" 등), 사용자가
#   "이거 하나하나 룰베이스로 하면 끝도 없어, 좋은 방법 없니?"라고 직접 지적
#   해서 이 잡음 제거 자체를 LLM에게 맡기는 쪽으로 구조를 바꿨다 — 새로운
#   잡음 패턴이 나올 때마다 정규식을 또 추가하는 대신, 아래 <instructions>의
#   "core" 정의(문법적 잡음만 제거, 의미 변형 없음)를 모델이 일반화해서
#   따르게 한다.
#
# "홍길동 프로" 변형을 뺀 이유(2026-09-17): 예전엔 인물 이름에 "프로" 호칭을 붙이는
# 변형을 항상 만들게 했는데, 실측해보니 이 회사 문서(특히 주간업무 보고서)는 거의
# 모든 사람 이름 뒤에 "프로"를 습관적으로 붙여서 이 단어 자체가 특정 인물을 전혀
# 구분해주지 못한다 — 오히려 "프로"가 수십 번 반복되는 다른 사람들 명단 문서가 Rovo
# 관련도 상위로 올라와 진짜 그 인물이 나온 문서를 밀어냈다("장승혁 프로" 검색 상위
# 5개 전부 무관한 주간보고 vs "장승혁" 단독 검색 상위 5개는 실제로 그가 등장하는
# 특허/자산/휴가 문서). "장승혁 부장"처럼 실제 정확한 직함을 붙이면 오히려 아주
# 정확했지만, 그 직함을 미리 알 방법이 없어 매번 추측하는 건 근본적으로 도박이라
# 채택 안 함 — 아래 <instructions>에도 이 축을 명시.
_FEWSHOT_EXAMPLES = """<example>
<question>DeviceManager 동작원리 알려줘</question>
<output>{"core": "DeviceManager 동작원리", "keywords": ["DeviceManager", "동작원리", "architecture", "구조", "design"], "person": null, "wants_person_stats": false}</output>
</example>
<example>
<question>BMS가 뭐야</question>
<output>{"core": "BMS", "keywords": ["BMS", "Battery Management System", "배터리 관리 시스템"], "person": null, "wants_person_stats": false}</output>
</example>
<example>
<question>홍길동이 뭐야</question>
<output>{"core": "홍길동", "keywords": ["홍길동", "Gildong Hong", "gildong.hong", "gildonghong"], "person": "홍길동", "wants_person_stats": false}</output>
</example>
<example>
<question>gem net id 가 ffff 가 아닌 예시 찾아줘</question>
<output>{"core": "gem net id ffff 아닌 예시", "keywords": ["GEM", "Net ID", "GEM Net ID", "GEM-NET-ID", "gem_net_id", "non-FFFF"], "person": null, "wants_person_stats": false}</output>
</example>
<example>
<context>[사용자] Energy SW 인원 정보 알려줘
[위키봇] (인원 명단 답변)</context>
<question>담당업무 로테이션으로 바꾸고 싶은데</question>
<output>{"core": "Energy SW 담당업무 로테이션", "keywords": ["Energy SW", "담당업무", "로테이션", "Job Rotation", "직무순환", "인사이동"], "person": null, "wants_person_stats": false}</output>
</example>
<example>
<question>jack jang/장승혁 프로가 누구야?</question>
<output>{"core": "jack jang 장승혁", "keywords": ["장승혁", "Jack Jang", "jack.jang", "jackjang", "Seunghyuk Jang", "seunghyuk.jang"], "person": "장승혁", "wants_person_stats": false}</output>
</example>
<example>
<question>장승혁이 문서 몇 개 썼어</question>
<output>{"core": "장승혁 문서", "keywords": ["장승혁", "Jack Jang", "jack.jang", "jackjang"], "person": "장승혁", "wants_person_stats": true}</output>
</example>
<example>
<question>confluence에서, EnergySW 파트 인원을 한정하여, 각각의 인원이 얼마나 많은 page를 생성했는지 정리부탁합니다.</question>
<output>{"core": "EnergySW 파트 인원 page 생성", "keywords": ["EnergySW", "Energy SW", "인원", "page", "생성", "작성", "페이지 수", "created pages", "author"], "person": null, "wants_person_stats": true}</output>
</example>
<example>
<question>energy sw에서 confluence에 페이지를 가장많이 작성한 사람은 누구야?</question>
<output>{"core": "EnergySW 페이지 가장 많이 작성한 사람", "keywords": ["EnergySW", "Energy SW", "페이지", "작성", "최다", "created pages", "author"], "person": null, "wants_person_stats": true}</output>
</example>
<example>
<question>심철로된 jira티켓 검색, 시간별로 급한것 정렬</question>
<output>{"core": "심철 jira 티켓 시간별 급한것 정렬", "keywords": ["심철", "Cheol Sim", "sim.cheol", "Jira", "티켓", "우선순위", "priority", "마감일", "due date"], "person": "심철", "wants_person_stats": false}</output>
</example>"""

_INSTRUCTIONS = """사내 기술 위키/Confluence 검색에 쓸 검색어를 만드는 도구다.
<context>가 있으면 그 맥락에 맞춰 <question> 속 모호한 단어의 의미부터 확정해라.

출력은 두 필드다:
- "core": 원본 질문에서 조사/구두점/구분자("/" 등)·"프로"/"님"/"씨" 같은 범용
  호칭·"뭐야"/"누구야"/"찾아줘" 같은 의문형 어미와 요청 동사만 문법적으로
  제거한, 최대한 원문에 가까운 검색어. 동의어로 바꾸거나 언어를 번역하거나
  의미를 확장하지 마라 — 오직 "질문이 아니라 검색어라면 어떻게 썼을까"만
  적용해라(대화 맥락으로 모호한 대상을 확정하는 것은 예외적으로 허용). 이
  필드는 검색 정확도의 기준선(anchor)으로 쓰이므로 잘못된 추측이 섞이면 안
  된다.
- "keywords": "core"를 포함해서, 질문의 핵심 검색 대상(기술 용어/약어/시스템·
  제품·프로젝트명/인물 이름 등 무엇이든)이 실제 문서에서 어떻게 다르게
  표기될 수 있는지 판단한 동의어/변형 후보 3~8개. 고려할 축(대상 성격에 맞는
  것만 적용):
  - 언어: 한글 표현과 영어 표현을 둘 다
  - 약어↔풀네임: 약어만 있으면 풀네임을, 풀네임만 있으면 약어를 추정
  - 표기 규칙: 띄어쓰기 유무, 대소문자, 구분자(마침표/하이픈/붙여쓰기) — 사내
    문서·이메일 계정은 같은 대상을 여러 방식으로 섞어 표기함
  - 도메인 모호성: 여러 분야에 걸쳐 쓰이는 용어는 확신 없으면 무리해서
    확장하지 마라 — 원본 표현은 확장이 틀리더라도 항상 keywords에 남겨야 한다
  - 인물 이름: 로마자 표기·사내 이메일 표기(소문자.마침표)·붙여쓰기 소문자는
    적극 추가하되, "프로"/"님"/"씨" 같은 범용 호칭 접미사는 keywords에도
    붙이지 마라 — 거의 모든 사람 이름에 똑같이 붙는 표현이라 특정 인물을
    구분하는 데 전혀 도움이 안 되고, 그 호칭이 잔뜩 들어간 다른 사람 관련
    문서(주간업무 인원 명단 등)만 검색 상위로 끌어올려 정작 찾는 사람의
    문서를 밀어낸다. 직함(부장/팀장 등)도 실제 직함을 모르면 추측해서
    붙이지 마라 — 틀린 추측은 물론 정확한 추측이어도 검증할 방법이 없다
- "person": 질문이 특정 인물 "한 명"을 콕 집어 묻고 있으면(예: 그 사람이
  누구인지, 무엇을 했는지, 문서를 몇 개/무엇을 작성했는지) 그 사람의 이름을
  문자열로, 아니면 반드시 null로. "EnergySW 파트 인원 전체가 각각 몇 개
  썼는지"처럼 여러 사람/그룹을 묻는 질문, 또는 사람 이름이 전혀 없는 질문은
  null이다 — "파트", "인원", "각각", "생성" 같은 일반 명사를 사람 이름으로
  착각해서 넣으면 안 된다(이 필드가 하는 일이 정확히 그 착각을 막는 것 —
  예전엔 이 판단을 정규식(한글 2~4음절이면 사람 이름일 것)으로 대충
  했었는데, "파트"/"인원" 같은 흔한 단어까지 걸려서 전혀 무관한 사람이
  작성자로 잘못 지목되는 사고가 실측됨, 2026-09-18). 확신이 없으면 null을
  택해라 — 잘못 채우면 엉뚱한 사람의 문서가 답변에 섞여 들어간다.
  **이름 뒤에 붙는 조사("로/으로/가/이/를/을/은/는/에게/한테" 등)는 반드시
  떼고 순수한 이름만 넣어라** — "심철로 된 jira 티켓"처럼 조사가 이름에
  바로 붙는 문장에서 "심철로"를 그대로 person에 넣으면(조사 미제거) 그
  계정을 영영 못 찾는다(실측: person="심철로"로 나온 요청은 계정 조회가
  0건으로 실패, "심철"이면 정상 조회됨 — 사용자가 "오타 아닌데"라며 재현,
  2026-09-18). "core" 필드의 조사 제거 규칙과 동일한 기준을 person에도
  똑같이 적용해라.
- "wants_person_stats": 질문이 "누가 문서를 몇 개/가장 많이/가장 적게 썼는지",
  "1위/최다/최소가 누구인지", "전체 목록을 다 보여달라" 같이 사람(1명이든 여러
  명이든)이 작성한 문서의 **개수·순위·전체 목록 자체**를 답으로 요구하면 true.
  단순히 그 사람/주제에 대한 문서 내용을 묻는 질문(예시 문서 몇 개만 곁들이면
  충분한 경우)은 false. "몇 개"/"얼마나 많이"라는 표현이 없어도 "가장 많이
  작성한 사람이 누구야"처럼 순위·최댓값을 묻는 질문이면 true로 판단해라 —
  이 필드는 정확한 키워드 매칭이 아니라 질문의 의도(개수/순위/전체목록을
  원하는가)로 판단하는 것이 핵심이다.
아래 <example>들을 참고해라. 출력은 반드시
{"core": "...", "keywords": [...], "person": "..." 또는 null,
 "wants_person_stats": true 또는 false}
JSON 하나만, 다른 설명·코드펜스는 절대 붙이지 마라."""


def _expand_search_query(question, history=None, timeout=30):
    """(core, keywords_str, person, wants_person_stats) 튜플을 반환한다. core는
    문법적 잡음만 걷어낸 원본 검색어(build_context()의 1차/신뢰 검색어로 사용),
    keywords_str은 동의어/표기 변형까지 포함한 보완 검색어(2차/확장 검색에 사용),
    person은 질문이 특정 인물 한 명을 콕 집어 물을 때만 그 이름(아니면 None) —
    build_context()가 작성자(author) 검색 앵커를 잡을 유일한 근거로 쓴다(정규식으로
    "한글 2~4음절"을 사람 이름 취급하던 예전 방식은 "파트"/"인원" 같은 일반 명사까지
    걸려서 폐기, atlassian_mcp_client.py 근처 주석 참고 대신 여기 <instructions>의
    person 설명 참고). wants_person_stats는 질문이 문서 개수/순위/전체목록 자체를
    원하는지(예: "누가 가장 많이 썼어", "몇 개 썼어", "다 보여줘") — 이 판단을
    wiki_chat_server.py의 정규식(_PERSON_DOC_COUNT_RE 등)에만 맡기면 "가장 많이"처럼
    정규식이 커버 못 하는 새 표현마다 정규식을 또 추가해야 하는 땜빵이 반복되므로
    (사용자가 이전에 이 패턴을 명시적으로 반려한 적 있음, 이 파일 상단 "장승혁"
    예시 설명 참고), 이미 호출 중인 이 LLM 판단에 필드를 추가해 일반화했다
    (2026-09-22, "가장많이 작성한 사람" 질문이 규칙에 안 걸려 놓친 사고로 발견).
    claude -p 실패/타임아웃/미설치 시 (question, question, None, False)로 폴백 —
    검색 자체를 막으면 안 되므로 조용히 원문 그대로 진행하되, 이 경우엔 person도
    wants_person_stats도 추측할 방법이 없으므로 둘 다 꺼진다(일관된 저하 — 다른
    확장 품질도 이미 같이 저하되는 상황이라 여기서만 정규식 안전망을 두지 않음;
    다만 wiki_chat_server.py 쪽 정규식은 이 폴백 경로에서도 여전히 독립적으로
    동작해 최소한의 커버리지를 보장한다)."""
    if not _claude_cli_available():
        return question, question, None, False
    prompt = _build_expand_prompt(question, history)
    result = _expand_search_query_llm_call(prompt, timeout)
    if not result:
        return question, question, None, False
    core, keywords_str, person, wants_person_stats = result
    return core, keywords_str, person, wants_person_stats


def _build_expand_prompt(question, history=None):
    context_block = ""
    if history and len(history) > 1:
        recent = history[:-1][-4:]  # 최신 질문 이전 최근 2턴 정도
        lines = []
        for m in recent:
            speaker = "사용자" if m.get("role") == "user" else "위키봇"
            content = (m.get("content") or "")[:300]
            lines.append(f"[{speaker}] {content}")
        context_block = f"<context>\n{chr(10).join(lines)}\n</context>\n\n"
    return (
        f"<instructions>\n{_INSTRUCTIONS}\n</instructions>\n\n"
        f"<examples>\n{_FEWSHOT_EXAMPLES}\n</examples>\n\n"
        f"{context_block}"
        f"<question>{question}</question>"
    )


def retry_person_extraction(question, history=None, timeout=30):
    """find_author_id_by_title()이 person 필드로 끝내 계정을 못 찾았을 때만 호출
    하는 재시도 전용 함수 — 같은 claude -p 프롬프트를 캐시 우회(nocache)로 한 번
    더 불러서 person만 새로 뽑는다. 실측: "심철로된 jira티켓 검색..."을 5번
    반복하면 4번은 person="심철"로 정확히 나오지만 1번은 person="심철로"(조사
    "로" 미제거)로 나와서 계정을 못 찾았다(사용자가 "오타 아닌데"라며 재현,
    2026-09-18). 문자열을 정규식으로 고치는 대신(사용자가 명시적으로 반려 —
    "은/는/이/가" 등은 실제 이름 끝음절과 겹쳐 오탐 위험) claude -p 자체의
    샘플링 비결정성을 재시도로 활용한다: 같은 질문을 다시 물으면 5번 중 4번
    꼴로 정확한 답이 나오므로, 실패했을 때만 한 번 더 물어보는 것으로 충분히
    커버된다. 실패 시 None(호출부가 안전하게 폴백하도록)."""
    if not _claude_cli_available():
        return None
    prompt = _build_expand_prompt(question, history)
    result = _expand_search_query_llm_call_nocache(prompt, timeout)
    if not result:
        return None
    _, _, person, _ = result
    return person


# atlassian_mcp_client.py의 find_author_id_by_title()이 쓰던 예전 로직: 이름이 제목에
# 들어간 문서들을 CQL로 모아서 "가장 많이 매칭된 계정"을 다수결로 채택했다. 그런데
# 실측으로 이게 틀렸다 — "정지석"으로 검색된 문서들의 creator 다수결이 실제로는
# 무관한 사람(Heela Park, "정지석"이 참석자/언급자로만 등장하는 회의록·명단성 문서를
# 다수 작성한 계정으로 추정)을 잘못 골라냄(사용자가 "정지석 프로가 creator인 페이지
# 몇 건인지 확인해줘"로 재현시킴, 2026-09-18). 앞서 "(장승혁, 김다빈)"류 공동 표기
# 문서 사고 때 추가했던 "단독 표기 우선" 규칙(atlassian_mcp_client.py 참고)만으로는
# 부족했던 셈 — 단독 표기 후보가 하나도 없으면 여전히 다수결로 떨어진다.
#
# 사용자가 명시적으로 요청(2026-09-18): "사람이름에 대해 하드코딩된 키워드
# 사용하지말고, llm으로 추론할수있어?" — 그래서 다수결(count 최댓값)을 LLM
# 판단으로 대체한다. 후보 각각이 창작한, 이름이 제목에 들어간 문서 제목들을 보여주고
# "이 중 누가 실제 그 사람인지" 판단하게 한다 — 제목에 이름이 하나만 단독으로 있는
# 문서는 강한 증거, 여러 명이 공동으로 표기된 문서는 약한 증거라는 걸 규칙으로
# 명시했다(예전 코드가 정확히 이 두 신호를 분간하려다 다수결에서 실패했던 지점).
#
# **처음엔 "후보가 1개뿐이면 LLM 안 부르고 그냥 채택"했었는데(불필요한 지연/비용을
# 피하려는 의도), 이게 또 다른 사고를 냈다**(2026-09-18, EnergySW 파트 18명 일괄
# 집계 실측 중 재현): "이선정"으로 CQL title~ 검색을 하면 "US AC System
# Overview(by 이선정)" 딱 한 건만 나오는데, 이 문서의 실제 creator는 이선정이
# 아니라 고윤석(Yunseok Ko)이었다 — "(by 누구)" 표기는 그 사람 얘기를 다룬다는
# 뜻이지 그 사람이 작성했다는 뜻이 아닌데, 후보가 1개뿐이라는 이유만으로 판단 없이
# 그대로 채택해버려서 "김건우"까지 똑같은 패턴("EU DC system Overview(by
# 김건우)")으로 엉뚱하게 같은 사람(고윤석)에게 잘못 배정됐다. 그래서 후보가
# 1개여도 반드시 LLM 판단을 거친다 — "후보 수"가 아니라 "증거의 질"로 신뢰도를
# 매기는 게 애초 목적이었는데 1개일 때만 그 목적을 건너뛰고 있었던 셈.
# Jira 증거 축 추가(2026-09-18, 사용자 제안: "jira를 연동하면 사람-계정이
# 매칭될수있을것 같애") — search_jira_mentions()가 JQL text~ 검색(본문/댓글까지
# 훑음)으로 찾은, 그 이름이 언급된 이슈들의 assignee를 후보로 같이 준다.
# Confluence 제목검색보다 커버리지는 넓지만(제목에 이름이 없어도 찾아짐) 똑같이
# 다수결로 믿으면 안 된다 — 실측: "고윤석"으로 검색된 이슈들의 assignee 1위는
# 본인이 아니라 전혀 다른 사람이었다(추정: 고윤석님이 DevOps/CI 업무 특성상 여러
# 사람 티켓에 리뷰어로 코멘트를 남겨서, "이름이 언급됨"과 "assignee=본인"이
# 오히려 다른 사람 쪽에서 더 자주 겹친 것으로 보임). 그래서 "담당 이슈 개수가
# 많다"는 이유만으로 고르면 안 되고, Confluence와 동일하게 증거 하나하나의 질로
# 판단해야 한다.
_PERSON_PICK_INSTRUCTIONS = """Confluence/Jira 검색으로 어떤 사람의 계정을 찾으려 한다.
아래는 후보(1명 이상)와 각자의 증거 목록이다. 증거는 두 종류다:
- "[Confluence 제목] ..." — 그 이름이 제목에 들어간 Confluence 문서를 실제로
  작성(creator)한 기록. Confluence의 creator는 "그 페이지를 실제로 만든/업로드한
  계정"일 뿐이다 — 문서 제목에 그 이름이 있다고 creator가 본인이라는 뜻은 아니다.
- "[Jira 담당 이슈] ..." — 그 이름이 본문/댓글 어딘가에 언급된 Jira 이슈의
  담당자(assignee) 기록. 담당자라고 해서 그 이슈 안에 언급된 이름의 당사자라는
  뜻은 아니다 — 다른 사람 얘기가 나온 이슈를 그냥 담당하고 있을 뿐일 수도,
  리뷰어/멘션 대상으로 코멘트에 등장했을 뿐일 수도 있다.

후보들의 증거를 보고, 질문의 인물 본인일 가능성이 가장 높은 후보를 번호로 골라라.

판단 기준:
- 제목 맨 앞에 그 이름이 "단독으로만" 표기된 Confluence 문서(예: "(장승혁)
  모니터링 시스템")는 강한 증거다 — 그 사람 본인이 쓴 개인 문서일 가능성이 높다.
- 다음은 모두 약한 증거다(실제 작성/담당 이유가 그 인물 본인이 아닐 수 있음):
  - 제목에 여러 사람 이름이 함께 표기된 공동/집계 Confluence 문서(예: "(장승혁,
    김다빈) 디자인 등록 출원", "OOO 파트 주간 업무", 회의록/명단)
  - "(by 이름)", "이름 정리", "이름 요청" 처럼 그 사람에 "대한"/"위한" 문서라는
    표기 — 작성 주체가 아니라 대상/의뢰인일 수 있다
  - Jira 담당 이슈 증거 단독으로는 항상 약하다 — 같은 이름이 여러 건에서
    반복돼도(빈도가 높아도) 그 자체가 강한 증거는 아니다. 다만 Confluence
    단독표기처럼 강한 증거가 이미 있는 후보를 Jira 담당 이슈가 같이 뒷받침하면
    확신을 더 높여도 된다.
  - 후보가 단 1명뿐이라는 사실 자체는 증거가 아니다 — 후보 수와 무관하게 위
    기준으로 증거 자체의 질을 판단해라
- 증거가 전부 약하거나 판단이 애매하면(후보가 1명뿐이어도) 반드시 null을 골라라
  — 틀린 추측보다 "모르겠다"가 낫다.

출력은 반드시 {"choice": <후보 번호(정수)> 또는 null} JSON 하나만, 다른 설명·
코드펜스 없이."""

_PERSON_PICK_FEWSHOT = """<example>
인물: "이선정"
후보:
1. Yunseok Ko — 문서: US AC System Overview(by 이선정)
<output>{"choice": null}</output>
</example>
<example>
인물: "장승혁"
후보:
1. Jaehyeong Lee — 문서: (장승혁, 김다빈) Energy Flow 디자인 등록 출원; (장승혁, 서국영) Energy Flow 디자인 등록 출원
2. Jack Jang — 문서: (장승혁) 모니터링 시스템 설계서
<output>{"choice": 2}</output>
</example>
<example>
인물: "정지석"
후보:
1. Heela Park — 문서: 2026 CW13 Energy SW part 주간 업무; 2025 CW48 Energy SW part 주간 업무
<output>{"choice": null}</output>
</example>
<example>
인물: "신동진"
후보:
1. Kim taehun — 문서: [Jira 담당 이슈] [ECR32] ACCB EMS 진단 에러코드 추가
2. Dongjin.Shin — 문서: [Jira 담당 이슈] [CASE5] Secondary 장비 Hysteresis High 오류 현상; [Jira 담당 이슈] [CASE6] ACCB EMS Startup Sequence 이상; [Jira 담당 이슈] External CT PCS PF계산 오류; [Jira 담당 이슈] HUB만 통신 중단 확인 요청
<output>{"choice": 2}</output>
</example>
<example>
인물: "고윤석"
후보:
1. Youngwoong Han — 문서: [Jira 담당 이슈] EMS+GEM USB업데이트 패키지 구성; [Jira 담당 이슈] Custom filed by user 기능 활성화 검토; [Jira 담당 이슈] Jira Automation Rule 적용 가능성 검토
2. Yunseok Ko — 문서: [Jira 담당 이슈] Gen3 CI 기본 환경 설정
<output>{"choice": null}</output>
</example>"""


@functools.lru_cache(maxsize=1000)
def _person_pick_llm_call(prompt, timeout=20):
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--system-prompt",
        '너는 후보 번호 JSON만 출력하는 도구다. 반드시 {"choice": 번호 또는 null} '
        "형식 하나만 출력하고, 코드펜스나 다른 설명은 절대 붙이지 마라.",
        "--tools", "",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if data.get("is_error"):
            if looks_like_usage_limit_error(data.get("result")):
                mark_usage_exhausted(data.get("result"))
            return None
        raw_text = data["result"]
        start, end = raw_text.find("{"), raw_text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        obj = json.loads(raw_text[start:end + 1])
        choice = obj.get("choice")
        return int(choice) if choice is not None else None
    except Exception:
        return None


def pick_person_account(name, candidates, titles_shown=4, timeout=20):
    """candidates: [(account_id, display_name, [title, ...]), ...] — 후보가 1개뿐이든
    여러 개든 항상 LLM에게 증거를 보여주고 실제 "name" 본인일 가능성이 가장 높은
    (account_id, display_name)을 고른다(확신 없으면 None) — 후보가 1개일 때 판단
    없이 그냥 채택하던 예전 지름길은 실측 사고로 폐기됨(위 모듈 주석 참고). 후보가
    아예 없거나 claude -p 미설치/실패/판단불가 시 None을 반환한다(호출부가 안전하게
    폴백하도록)."""
    if not candidates:
        return None
    if not _claude_cli_available():
        return None
    lines = [
        f'{i}. {display_name or "(이름 없음)"} — 문서: {"; ".join(titles[:titles_shown])}'
        for i, (_, display_name, titles) in enumerate(candidates, 1)
    ]
    prompt = (
        f"<instructions>\n{_PERSON_PICK_INSTRUCTIONS}\n</instructions>\n\n"
        f"<examples>\n{_PERSON_PICK_FEWSHOT}\n</examples>\n\n"
        f'인물: "{name}"\n후보:\n' + "\n".join(lines)
    )
    choice = _person_pick_llm_call(prompt, timeout)
    if not choice or not (1 <= choice <= len(candidates)):
        return None
    return candidates[choice - 1][:2]
