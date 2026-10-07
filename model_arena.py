"""여러 모델로 같은 질문에 답하게 하고, 더 나은 답을 채택하는 "모델 비교" 모드.

동작:
  1. 후보 모델들이 같은 참고 자료(context)로 병렬로 답변을 만든다.
  2. 두 가지 기준으로 채점한다.
     - 규칙 점수(heuristic_score): 참고 자료에 없는 URL을 지어냈는지, 출처를 하나라도 달았는지,
       자료가 있는데 "찾지 못했다"고 답했는지, 코드펜스가 깨졌는지 등 기계적으로 확인 가능한 것.
     - 심사 점수(LLM judge): 어느 모델이 쓴 답인지 가린 채(A/B/C 무작위 배정) 근거성·관련성·
       완결성을 0~10으로 매기게 한다. 심사가 실패하면 규칙 점수만으로 판정한다.
  3. 최종 점수 = 심사 70% + 규칙 30%. 1위를 채택하되, 1위가 기본 모델이 아니고 기본 모델과의
     차이가 ADOPT_MARGIN점 미만이면 "근소차"로 보고 기본 모델을 유지한다 — 샘플링 운으로
     잠깐 이긴 모델로 매번 갈아타지 않기 위한 장치.
"""

import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor

ADOPT_MARGIN = 3

# 작업대/모델 선택 목록에 노출하는 Claude 후보. id의 접두사로 백엔드를 고른다
# (claude:* → claude -p --model <alias>, ollama:* → 로컬 Ollama).
CLAUDE_MODELS = [
    {"id": "claude:sonnet", "label": "Claude Sonnet", "note": "기본 · 균형"},
    {"id": "claude:opus", "label": "Claude Opus", "note": "느림 · 정밀"},
    {"id": "claude:haiku", "label": "Claude Haiku", "note": "빠름"},
]

_CTX_URL_RE = re.compile(r"URL:\s*(\S+?)\]")
_ANSWER_URL_RE = re.compile(r"https?://[^\s)\]>\"'`]+")
_NOT_FOUND_RE = re.compile(r"찾지\s*못했습니다")


def model_label(model_id):
    for m in CLAUDE_MODELS:
        if m["id"] == model_id:
            return m["label"]
    if model_id.startswith("ollama:"):
        return model_id.split(":", 1)[1]
    return model_id


def heuristic_score(answer, context):
    """(0~100 점수, 감점 사유 리스트)."""
    if not answer or not answer.strip():
        return 0, ["빈 답변"]
    score = 100
    notes = []
    ctx_urls = {u.rstrip(".,") for u in _CTX_URL_RE.findall(context or "")}
    ans_urls = {u.rstrip(".,") for u in _ANSWER_URL_RE.findall(answer)}

    fabricated = [u for u in ans_urls if u not in ctx_urls and not any(c.startswith(u) or u.startswith(c) for c in ctx_urls)]
    if fabricated:
        penalty = min(45, 15 * len(fabricated))
        score -= penalty
        notes.append(f"참고 자료에 없는 URL {len(fabricated)}개 (−{penalty})")
    if ctx_urls and not (ans_urls & ctx_urls):
        score -= 15
        notes.append("출처 링크 없음 (−15)")
    if ctx_urls and _NOT_FOUND_RE.search(answer):
        score -= 20
        notes.append("자료가 있는데 '찾지 못했습니다' (−20)")
    if len(answer.strip()) < 60:
        score -= 30
        notes.append("답변이 지나치게 짧음 (−30)")
    elif len(answer) > 8000:
        score -= 10
        notes.append("답변이 지나치게 김 (−10)")
    if answer.count("```") % 2:
        score -= 15
        notes.append("코드블록이 닫히지 않음 (−15)")
    return max(0, score), notes


_JUDGE_SYSTEM = """당신은 사내 위키 챗봇 답변을 채점하는 엄격한 심사위원입니다.
"참고 자료"만이 사실의 근거입니다. 참고 자료에 없는 내용을 단정하거나 URL을 지어낸 답은 근거성 점수를 크게 깎으세요.
답변 길이나 말투가 아니라, 질문에 정확하고 빠짐없이 답했는지를 보세요.
반드시 JSON 객체 하나만 출력하고 다른 텍스트는 쓰지 마세요."""


def _judge_prompt(question, context, labeled_answers):
    ctx = context or "(참고 자료 없음)"
    if len(ctx) > 15000:
        ctx = ctx[:15000] + "\n…(이하 생략)"
    blocks = []
    for label, text in labeled_answers:
        t = text if len(text) <= 6000 else text[:6000] + "\n…(이하 생략)"
        blocks.append(f"=== 답변 {label} ===\n{t}")
    keys = ", ".join(f'"{label}"' for label, _ in labeled_answers)
    return (
        f"=== 참고 자료 ===\n{ctx}\n\n=== 질문 ===\n{question}\n\n" + "\n\n".join(blocks) +
        "\n\n각 답변을 0~10 정수로 채점하세요:\n"
        "- grounded: 참고 자료에 근거했는가(지어낸 사실/URL이 없는가)\n"
        "- relevant: 질문에 직접 답했는가\n"
        "- complete: 필요한 내용을 빠뜨리지 않았는가\n"
        "- reason: 한국어 한 문장 평가\n"
        f"출력 형식(키 {keys}): "
        '{"A": {"grounded": 8, "relevant": 9, "complete": 7, "reason": "..."}, ...}'
    )


def _parse_judge(text, labels):
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise ValueError("심사 결과에서 JSON을 찾지 못함")
    data = json.loads(m.group(0))
    out = {}
    for label in labels:
        row = data.get(label)
        if not isinstance(row, dict):
            raise ValueError(f"심사 결과에 {label} 항목 없음")
        g, r, c = (max(0, min(10, int(row.get(k, 0)))) for k in ("grounded", "relevant", "complete"))
        out[label] = {
            "score": round((g * 0.5 + r * 0.3 + c * 0.2) * 10),
            "grounded": g, "relevant": r, "complete": c,
            "reason": str(row.get("reason") or "")[:200],
        }
    return out


def run_arena(question, context, candidates, generate_fn, judge_fn, tracer, judge_model_label="Claude Haiku"):
    """candidates: 모델 id 리스트(첫 번째가 기본 모델). generate_fn(model_id) -> 답변 문자열.
    judge_fn(prompt, system) -> 심사 원문. 반환: (채택 답변, 채택 모델 id, 후보 결과 리스트, 판정 dict)."""
    results = {m: {"model": m, "label": model_label(m), "status": "running"} for m in candidates}

    def _one(model_id):
        tracer.push({"type": "candidate", "model": model_id, "label": model_label(model_id), "status": "running"})
        started = time.monotonic()
        try:
            answer = generate_fn(model_id)
        except Exception as e:
            ms = int((time.monotonic() - started) * 1000)
            results[model_id].update(status="error", error=str(e)[:300], ms=ms)
            tracer.push({"type": "candidate", "model": model_id, "label": model_label(model_id),
                         "status": "error", "error": str(e)[:300], "ms": ms})
            return
        ms = int((time.monotonic() - started) * 1000)
        results[model_id].update(status="done", answer=answer, ms=ms, chars=len(answer))
        tracer.push({"type": "candidate", "model": model_id, "label": model_label(model_id),
                     "status": "done", "ms": ms, "chars": len(answer)})

    with tracer.step("generate", f"답변 생성 · 모델 {len(candidates)}개 병렬") as st:
        with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
            list(pool.map(_one, candidates))
        ok = [m for m in candidates if results[m]["status"] == "done"]
        st.detail = f"{len(ok)}/{len(candidates)}개 성공"
        if not ok:
            st.status = "error"
            raise RuntimeError("모든 후보 모델이 답변 생성에 실패했습니다: " +
                               "; ".join(f"{model_label(m)}: {results[m].get('error')}" for m in candidates))

    with tracer.step("judge", "답변 채점 · 채택 판정") as st:
        for m in ok:
            h, notes = heuristic_score(results[m]["answer"], context)
            results[m]["heuristic"] = h
            results[m]["heuristic_notes"] = notes

        judged_by = "heuristic"
        if len(ok) >= 2:
            shuffled = ok[:]
            random.shuffle(shuffled)
            labels = [chr(ord("A") + i) for i in range(len(shuffled))]
            label_of = dict(zip(shuffled, labels))
            try:
                raw = judge_fn(_judge_prompt(question, context, [(label_of[m], results[m]["answer"]) for m in shuffled]),
                               _JUDGE_SYSTEM)
                judged = _parse_judge(raw, labels)
                for m in ok:
                    j = judged[label_of[m]]
                    results[m]["blind_label"] = label_of[m]
                    results[m]["judge"] = j
                    results[m]["score"] = round(0.7 * j["score"] + 0.3 * results[m]["heuristic"])
                judged_by = judge_model_label
            except Exception as e:
                st.items = [f"심사 모델 실패 → 규칙 점수로만 판정 ({str(e)[:120]})"]
        for m in ok:
            results[m].setdefault("score", results[m]["heuristic"])

        incumbent = candidates[0]
        best = max(ok, key=lambda m: (results[m]["score"], -candidates.index(m)))
        rule = "최고점 채택"
        adopted = best
        if (best != incumbent and results[incumbent]["status"] == "done"
                and results[best]["score"] - results[incumbent]["score"] < ADOPT_MARGIN):
            adopted = incumbent
            rule = f"근소차(<{ADOPT_MARGIN}점) — 기본 모델 유지"
        elif results[incumbent]["status"] != "done":
            rule = "기본 모델 실패 — 다음 최고점 채택"
        elif best == incumbent and any(
                m != incumbent and results[m]["score"] == results[incumbent]["score"] for m in ok):
            rule = "동점 — 기본 모델 유지"

        for m in candidates:
            r = results[m]
            if r["status"] != "done":
                r["decision"] = "탈락"
                r["decision_reason"] = "생성 실패"
            elif m == adopted:
                r["decision"] = "채택"
            else:
                r["decision"] = "탈락"

        margin = None
        if len(ok) >= 2:
            ranked = sorted(ok, key=lambda m: -results[m]["score"])
            margin = results[ranked[0]]["score"] - results[ranked[1]]["score"]
        verdict = {
            "adopted": adopted,
            "adopted_label": model_label(adopted),
            "rule": rule,
            "judged_by": judged_by,
            "margin": margin,
            "required_margin": ADOPT_MARGIN,
            "incumbent": incumbent,
        }
        st.detail = f"{model_label(adopted)} 채택 · {rule}"
        tracer.push({"type": "verdict", **verdict,
                     "scores": {m: results[m].get("score") for m in candidates}})

    # 탈락한 답변 원문도 run 기록에 남긴다 — 작업대에서 펼쳐 채택안과 나란히 비교할 수 있게.
    candidates_out = [dict(results[m]) for m in candidates]
    return results[adopted]["answer"], adopted, candidates_out, verdict
