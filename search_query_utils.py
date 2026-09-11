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
_PERSON_NAME_ALIASES = {
    "김하율": "Hayool Kim",
}


def _apply_person_aliases(query: str) -> str:
    """알려진 인물의 한글 이름이 질문에 있으면 검색용으로 영문 표기를 덧붙인다
    (원문 치환이 아니라 부가 — 한글 표기로 매치되는 다른 문서가 있으면 그것도 유지)."""
    extra = [en for ko, en in _PERSON_NAME_ALIASES.items() if ko in query]
    return query + " " + " ".join(extra) if extra else query


def _clean_query(query: str) -> str:
    q = _KO_STOP.sub(' ', query)
    q = _KO_PARTICLE.sub(' ', q)
    q = re.sub(r'\s+', ' ', q).strip()
    return q if len(q) > 3 else query


def _tech_query(query: str) -> str:
    """영문·숫자 기술 용어만 추출 (예: 'EMS Case3 BackupConfiguration')"""
    query = _KO_STOP.sub(' ', query)  # drawio/png 같은 매체 요청어가 기술 용어로 오인되는 것 방지
    terms = _TECH_TERMS.findall(query)
    return ' '.join(terms) if terms else query


def _extract_terms(query: str) -> list:
    """키워드 매치 채점용 용어 추출. 영문·숫자뿐 아니라 한글 기술 용어도 함께 뽑는다
    (순수 한글 질문에서 키워드 보너스가 0점이 되는 것을 방지)."""
    ascii_terms = _TECH_TERMS.findall(query)
    korean_terms = _KOREAN_TERMS.findall(_clean_query(query))
    return ascii_terms + korean_terms
