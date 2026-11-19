"""에이전트 루프. 고객 입력 한 번을 받아 "내보낼 것"을 차례로 돌려준다.

터미널(cli.py), 웹 서버, 평가 러너가 모두 handle()을 그대로 쓴다.
"""
import json
import time
import uuid
from datetime import datetime

import anthropic

from app.config import CONFIG, ROOT, claude_client
from app.guard import facts, violations
from app.prompt import SYSTEM, TODAY
from app.session import Session
from app.tools import TOOLS, run_tool

AGENT = CONFIG["agent"]
LOG_DIR = ROOT / "logs" / "calls"  # None이면 로그를 쓰지 않는다
LABELS = {"keypad": "키패드", "voice": "음성", "system": "시스템"}
BASE_FACTS = facts(SYSTEM) | facts(TODAY)  # 응대 기준 문서의 날짜와 오늘 날짜는 말해도 된다

# 모델이 툴을 부르면서 아무 말도 쓰지 않았을 때 대신 내보내는 채움 멘트
FILLERS = {
    "verify_order": "확인해 드릴게요. 잠시만 기다려 주세요.",
    "get_delivery_status": "조회해 드릴게요. 잠시만 기다려 주세요.",
    "submit_request": "확인 후 진행 도와드리겠습니다. 잠시만 기다려 주세요.",
}
SAFE_REPLY = "죄송합니다. 해당 내용은 확인 후 안내드리겠습니다."
TRANSFER_REPLY = "상담원에게 연결해 드리겠습니다. 잠시만 기다려 주세요."
ERROR_REPLY = "죄송합니다. 잠시 문제가 생겼습니다. 상담원에게 연결해 드리겠습니다."

_client = None


def new_session(session_id=None) -> Session:
    session = Session(session_id or f"c-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}")
    session.allowed_facts = set(BASE_FACTS)
    log(session, "call_start", model=AGENT["model"])
    return session


def log(session, kind, **data):
    if not LOG_DIR:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now().isoformat(timespec="milliseconds"), "turn": session.turn, "type": kind, **data}
    with (LOG_DIR / f"{session.id}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


def _create(session, model):
    global _client
    _client = _client or claude_client()
    extra = {"cache_control": {"type": "ephemeral"}} if AGENT["prompt_cache"] else {}
    return _client.messages.create(
        model=model,
        max_tokens=AGENT["max_tokens"],
        output_config={"effort": AGENT["effort"]},
        system=SYSTEM,
        tools=TOOLS,
        messages=session.messages,
        **extra,
    )


def handle(session, channel, text, model=None):
    """고객 입력 한 번을 처리한다. ("filler" | "reply" | "timing" | "end", 값)을 차례로 돌려준다."""
    model = model or AGENT["model"]
    session.turn += 1
    content = f"[{LABELS[channel]}] {text}"
    if not session.messages:
        content = "[시스템] 통화 시작. 시작 멘트가 재생되었습니다.\n" + content
    session.messages.append({"role": "user", "content": content})
    log(session, "input", channel=channel, text=text)

    started = time.perf_counter()
    elapsed = lambda since=started: round((time.perf_counter() - since) * 1000)  # noqa: E731
    first_ms = None
    reply = None
    guard_retried = False

    for _ in range(AGENT["max_tool_rounds"]):
        call_started = time.perf_counter()
        yield "thinking", None  # 화면에 "AI 판단 중"을 띄우기 위한 신호
        try:
            response = _create(session, model)
        except anthropic.APIStatusError as e:
            log(session, "error", where="model_call", status=e.status_code, message=e.message)
            break
        except anthropic.APIConnectionError as e:
            log(session, "error", where="model_call", message=str(e))
            break
        usage = response.usage
        log(session, "model_call", ms=elapsed(call_started), model=response.model, stop_reason=response.stop_reason,
            tokens_in=usage.input_tokens, tokens_out=usage.output_tokens,
            tokens_cache_write=usage.cache_creation_input_tokens, tokens_cached=usage.cache_read_input_tokens)
        yield "model", {"ms": elapsed(call_started), "decided": "tool" if response.stop_reason == "tool_use" else "answer"}
        if response.stop_reason not in ("end_turn", "tool_use"):  # 거절(refusal), 길이 초과(max_tokens) 등
            log(session, "error", where="stop_reason", stop_reason=response.stop_reason)
            break

        session.messages.append({"role": "assistant", "content": response.content})  # 생각 블록까지 그대로
        said = " ".join(b.text for b in response.content if b.type == "text").strip()
        calls = [b for b in response.content if b.type == "tool_use"]

        if calls:
            filler = said if said and not violations(said, session.allowed_facts) else ""
            if not filler and first_ms is None:
                filler = next((FILLERS[c.name] for c in calls if c.name in FILLERS), "")
            if filler:
                first_ms = first_ms if first_ms is not None else elapsed()
                log(session, "filler", text=filler)
                yield "filler", filler
            results = []
            for call in calls:
                tool_started = time.perf_counter()
                result, is_error = run_tool(session, call.name, call.input)
                result_json = json.dumps(result, ensure_ascii=False)
                session.allowed_facts |= facts(result_json)
                log(session, "tool_call", name=call.name, input=call.input, result=result, ok=not is_error, ms=elapsed(tool_started))
                yield "tool", {"name": call.name, "input": call.input, "result": result, "ok": not is_error}
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": result_json, "is_error": is_error})
            session.messages.append({"role": "user", "content": results})
            continue

        bad = violations(said, session.allowed_facts)
        if bad:
            log(session, "guard", blocked=sorted(bad), text=said, retried=guard_retried)
            yield "guard", sorted(bad)
            if not guard_retried:
                guard_retried = True
                session.messages.append({"role": "user", "content": (
                    f"[시스템] 방금 답은 고객에게 전달되지 않았습니다. 조회 결과나 응대 기준 문서에 없는 값({', '.join(sorted(bad))})이 "
                    "들어 있습니다. 확인된 값만 써서 다시 답하세요."
                )})
                continue
            said = SAFE_REPLY
        else:
            yield "checked", None  # 출력 검사 통과
        if said:
            reply = said
        elif session.transferred:
            # 연결 안내를 이미 말했으면(툴 호출 앞에 쓴 말) 같은 안내를 또 붙이지 않는다
            reply = "" if first_ms is not None else TRANSFER_REPLY
        break

    if reply is None:  # API 오류, 거절, 툴 호출이 너무 많음, 빈 답
        if not session.transferred:
            run_tool(session, "transfer_to_agent", {"reason": "other", "summary": "시스템 오류로 응대를 마치지 못함"})
        reply = ERROR_REPLY
    first_ms = first_ms if first_ms is not None else elapsed()
    timing = {"first_response_ms": first_ms, "total_ms": elapsed()}  # 내보내기 전에 잰다 (받는 쪽의 TTS 시간이 섞이지 않게)
    if reply:
        log(session, "reply", text=reply, **timing)
        yield "reply", reply
    yield "timing", timing
    if session.transferred:
        log(session, "call_end", reason="transferred")
        yield "end", "transferred"
