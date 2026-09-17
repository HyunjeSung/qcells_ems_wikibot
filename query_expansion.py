#!/usr/bin/env python3
"""Rovo Search 직전에 검색어를 보강하는 `claude -p` 프롬프트 엔지니어링 전담 모듈.

wiki_chat_server.py의 답변 합성 로직과는 독립적인 관심사(검색어 확장)라 별도
파일로 분리했다 — 검색어 확장 규칙이 늘어날 때마다 wiki_chat_server.py를
더 거대하게 만들지 않기 위함.
"""

import functools
import json
import shutil
import subprocess


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
        '너는 검색어 키워드 JSON만 출력하는 도구다. 반드시 {"keywords": ["...", ...]} '
        "형식 하나만 출력하고, 코드펜스나 다른 설명은 절대 붙이지 마라.",
        "--tools", "",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if data.get("is_error"):
            return None
        return _parse_keywords(data["result"])
    except Exception:
        return None


def _parse_keywords(raw_text):
    """모델 출력에서 {"keywords": [...]}를 뽑아 공백 구분 문자열로 합친다.

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
    return " ".join(cleaned) if cleaned else None


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
<output>{"keywords": ["DeviceManager", "동작원리", "architecture", "구조", "design"]}</output>
</example>
<example>
<question>BMS가 뭐야</question>
<output>{"keywords": ["BMS", "Battery Management System", "배터리 관리 시스템"]}</output>
</example>
<example>
<question>홍길동이 뭐야</question>
<output>{"keywords": ["홍길동", "Gildong Hong", "gildong.hong", "gildonghong"]}</output>
</example>
<example>
<question>gem net id 가 ffff 가 아닌 예시 찾아줘</question>
<output>{"keywords": ["GEM", "Net ID", "GEM Net ID", "GEM-NET-ID", "gem_net_id", "non-FFFF"]}</output>
</example>
<example>
<context>[사용자] Energy SW 인원 정보 알려줘
[위키봇] (인원 명단 답변)</context>
<question>담당업무 로테이션으로 바꾸고 싶은데</question>
<output>{"keywords": ["Energy SW", "담당업무", "로테이션", "Job Rotation", "직무순환", "인사이동"]}</output>
</example>"""

_INSTRUCTIONS = """사내 기술 위키/Confluence 검색에 쓸 키워드를 뽑는 도구다.
<context>가 있으면 그 맥락에 맞춰 <question> 속 모호한 단어의 의미부터 확정해라.
그다음 질문의 핵심 검색 대상(기술 용어/약어/시스템·제품·프로젝트명/인물 이름 등
무엇이든)이 실제 문서에서 어떻게 다르게 표기될 수 있는지 판단해서 키워드
후보를 3~8개 만들어라. 고려할 표기 변형 축(대상 성격에 맞는 것만 적용):
- 언어: 한글 표현과 영어 표현을 둘 다
- 약어↔풀네임: 약어만 있으면 풀네임을, 풀네임만 있으면 약어를 추정
- 표기 규칙: 띄어쓰기 유무, 대소문자, 구분자(마침표/하이픈/붙여쓰기) — 사내
  문서·이메일 계정은 같은 대상을 여러 방식으로 섞어 표기함
- 도메인 모호성: 여러 분야에 걸쳐 쓰이는 용어는 확신 없으면 무리해서 확장하지
  마라 — 원본 표현은 확장이 틀리더라도 항상 keywords에 남겨야 한다
- 인물 이름: 로마자 표기·사내 이메일 표기(소문자.마침표)·붙여쓰기 소문자는
  적극 추가하되, "프로"/"님"/"씨" 같은 범용 호칭 접미사는 절대 붙이지 마라 —
  거의 모든 사람 이름에 똑같이 붙는 표현이라 특정 인물을 구분하는 데 전혀
  도움이 안 되고, 그 호칭이 잔뜩 들어간 다른 사람 관련 문서(주간업무 인원
  명단 등)만 검색 상위로 끌어올려 정작 찾는 사람의 문서를 밀어낸다. 직함
  (부장/팀장 등)도 실제 직함을 모르면 추측해서 붙이지 마라 — 틀린 추측은
  물론 정확한 추측이어도 검증할 방법이 없다
아래 <example>들을 참고해라. 출력은 반드시 {"keywords": [...]} JSON 하나만,
다른 설명·코드펜스는 절대 붙이지 마라."""


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
        context_block = f"<context>\n{chr(10).join(lines)}\n</context>\n\n"
    prompt = (
        f"<instructions>\n{_INSTRUCTIONS}\n</instructions>\n\n"
        f"<examples>\n{_FEWSHOT_EXAMPLES}\n</examples>\n\n"
        f"{context_block}"
        f"<question>{question}</question>"
    )
    # 정제된 키워드가 나왔으면 그것만 검색어로 쓴다(원문에 붙이지 않음) — Rovo Search는
    # 짧은 키워드 질의에서 훨씬 정확한데(기존에 검증됨), 잡음 섞인 원문 문장을 그대로
    # 이어붙이면 그 원칙과 반대로 가서 관련도가 흔들린다(실측: "Energy SW"를 물어본
    # 대화의 후속 질문에 무관한 문장이 잔뜩 섞이자 엉뚱한 파트의 R&R 문서가 나온 사례).
    extra = _expand_search_query_llm_call(prompt, timeout)
    return extra if extra else question
