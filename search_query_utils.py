#!/usr/bin/env python3
"""검색어 정제용 순수 정규식 헬퍼 (CQL 폴백 검색에서 사용).

원래 로컬 위키 RAG 검색 도구(wiki_ask.py)의 일부였으나, 이 봇은 답변 소스를 라이브
Confluence로만 한정하면서 로컬 벡터 검색 자체는 더 이상 쓰지 않는다. 다만 자연어 질문에서
검색용 기술 키워드를 뽑는 로직은 CQL 폴백 검색에 여전히 필요해서, 무거운 ML 의존성
(numpy/hnswlib/sentence-transformers)을 끌어오지 않도록 이 부분만 별도 모듈로 분리했다.
"""

import re

# 한국어 질문어·조사 제거 → 기술 용어만 남긴 쿼리 생성
_KO_STOP = re.compile(
    r'(뭐야|뭔가요|무엇인가요|무엇이야|어떻게\s?동작|어떻게\s?작동|어떤\s?거야'
    r'|설명해\s?줘|설명해\s?주세요|알려\s?줘|알려\s?주세요|찾아\s?줘|찾아\s?주세요'
    r'|검색해\s?줘|검색해\s?주세요'
    r'|그림\s?포함해서|포함해서|해서|이란|이란\?'
    # 매체/형식 요청 문구 — 문서 주제와 무관해서 검색을 엉뚱한 방향으로 끌고 가는
    # 원인이 됨. 검색어에서는 제거하고 답변 생성 시에만 반영
    r'|사진|그림|이미지|도표|다이어그램|drawio|png|jpg'
    r'|등을\s?통해서|을\s?통해서|를\s?통해서|를\s?통해|을\s?통해'
    # 검색 대상 자체를 가리키는 메타 단어 — "TOU 관련 confluence 페이지 찾아줘"처럼
    # 섞여 들어가면 _tech_query가 "confluence"까지 기술 용어로 오인해서 CQL
    # 검색어를 "TOU confluence"로 만들어버려 title 매치가 아예 안 됨
    r'|confluence|wiki|위키|페이지)'
    , re.IGNORECASE
)
_KO_PARTICLE = re.compile(r'(?<=[가-힣\w])([이가은는을를의에서에로와과]\s)')
_TECH_TERMS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_./\-]*')  # 영문·숫자 기술 용어
_KOREAN_TERMS = re.compile(r'[가-힣]{2,}')

# 사람 이름의 한글 표기 -> Confluence 상 실제 영문 표기. Rovo/CQL 검색은 원문 문자열
# 그대로 매치하므로, 한글 질문("김하율")이 본문/작성자 필드의 영문 표기("Hayool Kim")와
# 전혀 안 겹치면 검색 결과가 0건이 된다(실측, 2026-09-11 — 김하율 개인 스페이스를
# 학습 목록에 추가한 직후에도 "김하율프로가 누구야"에 답을 못 찾음). 한글 이름의 로마자
# 표기는 규칙적으로 유추가 안 되므로("하율" -> Hayul/Hayool/Ha-yul 등) _expand_search_query의
# LLM 동의어 확장에 맡기지 않고 여기서 명시적으로 고정한다.
#
# **사람마다 여기 새로 추가하지 말 것**(2026-09-17, 사용자가 "Jack Jang"→"장승혁"
# 항목을 넣은 것을 보고 직접 반려: "이렇게 하드코딩하지 말라고"). 소리로 전혀
# 유추 안 되는 영문 별명("Jack Jang" 같은, Seunghyeok과 음성적 연관이 없는 지정
# 영문 이름)은 대신 atlassian_mcp_client.py의 `_person_alias_cache`(런타임에
# Confluence 데이터에서 실제로 찾아낸 결과를 스스로 기억하는 캐시)가 처리한다 —
# 한 번이라도 정확한 이름으로 찾아진 인물은 그 뒤로 별명으로 물어도 코드 수정 없이
# 바로 찾아짐. 이 딕셔너리는 "로마자 음역 자체가 불규칙한 한글 이름"(김하율 같은)
# 전용으로 좁혀서 유지한다.
_PERSON_NAME_ALIASES = {
    "김하율": "Hayool Kim",
}


def _apply_person_aliases(query: str) -> str:
    """알려진 인물의 한글 이름 또는 영문 별칭이 질문에 있으면 검색용으로 반대쪽
    표기를 덧붙인다(원문 치환이 아니라 부가 — 원래 표기로 매치되는 다른 문서가
    있으면 그것도 유지). 양방향("김하율"->추가 "Hayool Kim", "Hayool Kim"->추가
    "김하율")이라 어느 쪽으로 물어도 동일하게 보강된다."""
    query_lower = query.lower()
    extra = []
    for ko, en in _PERSON_NAME_ALIASES.items():
        if ko in query and en not in query:
            extra.append(en)
        elif en.lower() in query_lower and ko not in query:
            extra.append(ko)
    return query + " " + " ".join(extra) if extra else query


def _clean_query(query: str) -> str:
    q = _KO_STOP.sub(' ', query)
    q = _KO_PARTICLE.sub(' ', q)
    q = re.sub(r'\s+', ' ', q).strip()
    # 정제 결과가 너무 짧으면(필터·조사 제거로 알맹이가 거의 안 남았으면) 정제를
    # 못 믿고 원문을 쓴다. 예전엔 이 기준이 "> 3"이라 정확히 3음절인 한글 이름
    # ("장승혁", "홍길동" 등 가장 흔한 한국 이름 길이)이 조사·의문어 제거 후 딱 그
    # 이름만 남으면 "너무 짧다"고 오판해서 원문("장승혁이 뭐야")을 그대로 검색어로
    # 써버렸다 — 조사가 안 떨어진 "장승혁이"가 Rovo Search에서 0건이 되어 원본 질의
    # 검색 자체가 실패하고, build_context가 이를 "Rovo 응답 없음"으로 오인해 더 나은
    # 결과를 주는 확장 질의 Rovo 검색까지 건너뛰고 레거시 CQL 폴백으로 빠지는 연쇄
    # 실패가 실측됨(2026-09-17, "장승혁이 뭐야" 케이스). 2글자 이상이면 신뢰하도록 완화.
    return q if len(q) >= 2 else query


def _tech_query(query: str) -> str:
    """영문·숫자 기술 용어만 추출 (예: 'EMS Case3 BackupConfiguration')"""
    query = _KO_STOP.sub(' ', query)  # drawio/png 같은 매체 요청어가 기술 용어로 오인되는 것 방지
    terms = _TECH_TERMS.findall(query)
    return ' '.join(terms) if terms else query


def _extract_terms(query: str) -> list:
    """키워드 매치 채점용 용어 추출. 영문·숫자뿐 아니라 한글 기술 용어도 함께 뽑는다
    (순수 한글 질문에서 키워드 보너스가 0점이 되는 것을 방지).

    "/"는 먼저 공백으로 바꾼다 — _TECH_TERMS가 "."/"-"와 함께 "/"도 토큰 내부 문자로
    허용해서("Q.OMMAND"/"GEM-NET-ID" 같은 기술 용어를 살리려는 의도), "Jack Jang/장승혁"
    처럼 사용자가 이름 표기를 "/"로 나열한 질문에서 "jang/"이 슬래시가 붙은 채로 한
    토큰이 되어 Rovo Search에서 정상적인 "Jang" 매치를 방해하는 게 실측됨(2026-09-17,
    "jack jang/장승혁 프로가 누구야?" 케이스 — 슬래시 없는 "장승혁 프로가 누구야"는
    같은 파이프라인에서 정상 동작했는데 이 케이스만 실패해서 추적함)."""
    query = query.replace("/", " ")
    ascii_terms = _TECH_TERMS.findall(query)
    korean_terms = _KOREAN_TERMS.findall(_clean_query(query))
    return ascii_terms + korean_terms
