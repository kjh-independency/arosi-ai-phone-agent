"""웹 데모 서버. 실행: uvicorn app.server:app  → 브라우저에서 http://localhost:8000

브라우저는 이 서버에만 요청한다. GMS 키와 GMS 주소는 서버 안에서만 쓴다.
응답은 한 줄에 사건 하나씩(JSON) 차례로 흘려보낸다. 화면은 줄이 올 때마다 바로 그린다.
주소 뒤에 ?tts=1 을 붙이면 봇의 말을 음성으로도 보낸다(음성 모드).
"""
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app import agent, tools, voice
from app.keypad import press
from app.prompt import OPENING, TODAY

app = FastAPI()
INDEX = Path(__file__).parent / "static" / "index.html"
SESSIONS = {}  # 참고: 메모리에만 둔다. 서버를 끄면 통화가 사라진다. 실서비스는 저장소가 필요하다


class Key(BaseModel):
    key: str


class Say(BaseModel):
    text: str


def _line(kind, value=None):
    return json.dumps({"type": kind, "value": value}, ensure_ascii=False, default=str) + "\n"


def _state(session):
    return {
        "buffer": session.keypad_buffer,
        "verified": sorted(session.verified_orders),
        "requests": [r for r in session.requests if r["type"] != "transfer"],
        "transfer": next((r["reason"] for r in session.requests if r["type"] == "transfer"), None),
    }


def _session(call_id):
    session = SESSIONS.get(call_id)
    if not session:
        raise HTTPException(404, "없는 통화입니다. 통화를 다시 시작해 주세요.")
    if session.transferred:
        raise HTTPException(409, "상담원에게 연결되어 끝난 통화입니다.")
    return session


def _turn(session, channel, text, tts=False, audio=None, content_type=None):
    """입력 한 번을 처리하며 사건을 흘려보낸다. audio가 있으면 먼저 STT로 글로 바꾼다."""
    def lines():
        nonlocal channel, text
        started = time.perf_counter()
        since = lambda: round((time.perf_counter() - started) * 1000)  # noqa: E731
        timing = {}
        if audio is not None:
            # 받아쓰기가 이상할 때 실제로 무엇이 녹음됐는지 들어 볼 수 있게 남긴다
            recording = agent.LOG_DIR.parent / "recordings" / f"{session.id}_{session.turn + 1}.{voice.extension(content_type)}"
            recording.parent.mkdir(parents=True, exist_ok=True)
            recording.write_bytes(audio)
            try:
                heard = voice.stt(audio, content_type)
            except Exception as e:
                agent.log(session, "error", where="stt", message=f"{type(e).__name__} {e}")
                heard = ""
            timing["stt_ms"] = since()
            agent.log(session, "stt", text=heard, ms=timing["stt_ms"])
            yield _line("stt", {"text": heard, "ms": timing["stt_ms"]})
            channel, text = ("voice", heard) if heard else ("system", "음성 인식 실패")
        if text is not None:
            yield _line("input", {"channel": channel, "text": text})
            for kind, value in agent.handle(session, channel, text):
                yield _line(kind, value)
                if tts and kind in ("filler", "reply"):
                    for clip in voice.speak(value):
                        timing.setdefault("first_audio_ms", since())
                        if clip.get("start") or clip.get("error"):  # 문장마다 한 번만 기록한다
                            agent.log(session, "tts", text=clip["text"], ms=clip.get("ms"), cached=clip.get("cached"), error=clip.get("error"))
                        yield _line("audio", clip)
            if timing:
                yield _line("voice", timing)
        yield _line("state", _state(session))

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/orders")
def orders():
    """화면 아래 '테스트용 주문번호' 표."""
    return [
        {"order_number": o["order_number"], "items": [i["name"] for i in o["items"]], "channel": o["channel"],
         "status": tools.order_status(o), "fulfillment": o["fulfillment"], "gift": o["gift"]["eligible"],
         "boxes": [s["status"] for s in o["shipments"]]}
        for o in tools.ORDERS.values()
    ]


@app.post("/api/calls")
def start_call(tts: bool = False):
    session = agent.new_session()
    SESSIONS[session.id] = session
    call = {"id": session.id, "opening": OPENING, "model": agent.AGENT["model"], "today": TODAY}
    if tts:
        voice.warm()  # 고정 문구 음성이 없으면 이때 한 번 만든다
    call["audio"] = list(voice.speak(OPENING))  # 시작 멘트는 음성 모드가 아니어도 읽어 준다 (저장된 음성을 쓴다)
    return call


@app.post("/api/calls/{call_id}/key")
def key(call_id: str, body: Key, tts: bool = False):
    session = _session(call_id)
    return _turn(session, "keypad", press(session, body.key), tts)


@app.post("/api/calls/{call_id}/say")
def say(call_id: str, body: Say, tts: bool = False):
    return _turn(_session(call_id), "voice", body.text.strip() or None, tts)


@app.post("/api/calls/{call_id}/speak")
async def speak(call_id: str, request: Request, tts: bool = True):
    """마이크 녹음을 그대로(요청 본문) 받는다."""
    session = _session(call_id)
    return _turn(session, "voice", None, tts, audio=await request.body(),
                 content_type=request.headers.get("content-type", "audio/webm"))
