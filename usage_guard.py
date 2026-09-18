#!/usr/bin/env python3
"""
claude -p 사용량 한도(Claude Code/claude.ai 계정 단위 usage limit) 소진 감지 전담 모듈
(2026-09-18 추가, 사용자 요청: "토큰을 다써서... 토큰이 10프로 이하 남았을때는...
토큰을 모두 사용하였습니다 라는 문구로 보여주고 검색 비활성화하자").

**중요한 제약**: claude CLI에는 "사용량 몇 % 남았는지"를 알려주는 API가 없다
(claude auth status/doctor 등 확인했으나 usage 수치 자체가 없음, 2026-09-18). 그래서
"10% 이하일 때" 같은 사전 예측(proactive)은 애초에 구현할 방법이 없고, 대신 실제로
확인 가능한 유일한 신호 — claude -p 호출이 사용량 한도 초과로 **실패하는 순간** —
을 감지하는 반응형(reactive) 방식으로 만든다. 한 번 감지되면 이후 요청은 claude -p를
아예 다시 시도하지 않고(같은 한도를 또 깎아먹거나 매번 똑같이 실패하는 낭비를 막음)
고정 메시지로 즉시 답한다. 사용자가 실제로 본 시스템 메시지("Your claude.ai usage
limit has reset")처럼 한도는 일정 시간 후 리셋되므로, COOLDOWN_HOURS가 지나면 자동
해제하고 다시 시도한다 — 정확한 리셋 주기를 알 방법이 없어 Claude Code Pro의 표준
세션 윈도우(5시간)를 기본값으로 잡았다(WIKIBOT_USAGE_COOLDOWN_HOURS 환경변수로 조정
가능).
"""

import json
import os
import re
import time
from pathlib import Path

FLAG_PATH = Path(__file__).parent / ".usage_exhausted.json"
COOLDOWN_HOURS = float(os.environ.get("WIKIBOT_USAGE_COOLDOWN_HOURS", "5"))

# claude -p가 is_error:true와 함께 돌려주는 result 메시지에서 "사용량 한도 초과"를
# 가리키는 걸로 보이는 문구들 — 실제 메시지 문구를 아직 실측하지 못해서(재현이
# 어려움) 영어/한글 양쪽으로 폭넓게 잡았다. 오탐(진짜 한도 초과가 아닌 다른 에러를
# 한도 초과로 착각)보다는, 누락(진짜 한도 초과인데 못 잡아서 계속 실패 호출을
# 반복)이 더 걱정되는 실패 모드라 넓게 잡는 쪽을 택함 — 실측되면 여기 목록을
# 좁히거나 정확한 문구로 교체할 것.
_LIMIT_SIGNAL_RE = re.compile(
    r"usage limit|rate limit|limit reached|quota exceeded|"
    r"사용량\s*한도|사용\s*한도|한도\s*초과|토큰.*(소진|초과)",
    re.IGNORECASE,
)


def looks_like_usage_limit_error(message):
    """claude -p 실패 메시지가 사용량 한도 초과처럼 보이면 True."""
    return bool(message) and bool(_LIMIT_SIGNAL_RE.search(str(message)))


def mark_usage_exhausted(reason=""):
    """사용량 한도 초과를 감지했을 때 호출 — 플래그를 파일로 남긴다(서비스 재시작해도
    유지되게). 이미 최근에 찍힌 플래그가 있으면 시각을 덮어쓰지 않는다(연속 실패마다
    쿨다운이 계속 뒤로 밀리는 걸 방지 — 첫 감지 시각 기준으로 쿨다운이 흘러야 한다)."""
    if FLAG_PATH.exists():
        return
    try:
        FLAG_PATH.write_text(json.dumps({"since": time.time(), "reason": str(reason)[:300]}))
    except Exception:
        pass


def clear_usage_exhausted():
    """수동 해제용(관리자가 한도가 이미 풀린 걸 확인했을 때 등)."""
    try:
        FLAG_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def is_usage_exhausted():
    """현재 "한도 초과로 검색/답변 생성을 건너뛰어야 하는" 상태인지. 플래그가 없으면
    False. 있어도 COOLDOWN_HOURS가 지났으면 자동 해제하고 False(다시 시도해볼 시점이
    됐다고 보고, 이번 요청이 실제로 또 실패하면 mark_usage_exhausted()가 새 시각으로
    다시 플래그를 세운다)."""
    if not FLAG_PATH.exists():
        return False
    try:
        data = json.loads(FLAG_PATH.read_text())
        since = float(data.get("since", 0))
    except Exception:
        # 플래그 파일이 깨졌으면 안전하게 "초과 아님"으로 취급하지 않고 지워서
        # 다음부터 깨끗한 상태로 시작 — 깨진 파일 때문에 영구적으로 검색이 막히는
        # 사고를 막는다.
        clear_usage_exhausted()
        return False
    if (time.time() - since) >= COOLDOWN_HOURS * 3600:
        clear_usage_exhausted()
        return False
    return True


USAGE_EXHAUSTED_MESSAGE = "토큰을 모두 사용하였습니다."
