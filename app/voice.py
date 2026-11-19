"""음성 입출력. STT와 TTS 모두 서버에서 GMS를 거쳐 부른다 (브라우저는 GMS를 모른다).

나중에 로컬 모델이나 전화 음성으로 바꿀 때는 stt()와 tts() 두 함수만 바꾸면 된다.
"""
import base64
import hashlib
import json
import re
import time
import wave

import httpx2  # anthropic 1.x가 함께 설치하는 HTTP 라이브러리

from app.agent import FILLERS, TRANSFER_REPLY
from app.config import CONFIG, ROOT, api_key
from app.prompt import OPENING

VOICE = CONFIG["voice"]
GMS = CONFIG["provider"]["gms_base"]
CACHE = ROOT / "logs" / "tts_cache"  # 한 번 만든 음성은 다시 쓴다 (TTS가 크레딧을 가장 많이 쓴다)
FIXED = [OPENING, *FILLERS.values(), TRANSFER_REPLY]  # 문장으로 나누지 않고 통째로 읽는 고정 문구
RATE = 24000   # 음성은 16비트, 24kHz, 모노 PCM으로 오간다
CHUNK = 9600   # 브라우저로 넘기는 조각 크기 (0.2초 분량)

_http = httpx2.Client(timeout=30)


def extension(content_type: str) -> str:
    return content_type.split("/")[-1].split(";")[0]  # audio/webm;codecs=opus → webm


def stt(audio: bytes, content_type: str = "audio/webm") -> str:
    """녹음을 글로 바꾼다."""
    r = _http.post(
        f"{GMS}/api.openai.com/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {api_key()}"},
        data={"model": VOICE["stt_model"], "language": "ko", "prompt": VOICE["stt_prompt"]},
        files={"file": (f"speech.{extension(content_type)}", audio, content_type)},
    )
    r.raise_for_status()
    return r.json()["text"].strip()


def speakable(text: str) -> str:
    """글을 소리 내어 읽기 좋게 바꾼다."""
    text = re.sub(r"(?<=\d)\s*~\s*(?=\d)", "에서 ", text)             # 1~2영업일 → 1에서 2영업일
    text = re.sub(r"\d{5,}", lambda m: " ".join(m.group()), text)     # 송장번호·주문번호는 한 자리씩
    return text.replace("#", "샵")


def _cache_path(text):
    key = "|".join((VOICE["tts_model"], VOICE["tts_voice"], VOICE["tts_style"], speakable(text)))
    return CACHE / (hashlib.sha1(key.encode()).hexdigest() + ".wav")


def _cached(text):
    """저장해 둔 음성이 있으면 PCM을, 없으면 None을 돌려준다."""
    try:
        with wave.open(str(_cache_path(text))) as f:
            if (f.getframerate(), f.getnchannels(), f.getsampwidth()) == (RATE, 1, 2):
                return f.readframes(f.getnframes())
    except (OSError, wave.Error, EOFError):
        pass
    return None


def tts(text: str):
    """글을 음성으로 바꿔 PCM 조각을 차례로 돌려준다. 만들어지는 대로 받으므로 첫 조각이 1초 안에 온다.

    두 가지가 지연을 좌우한다(11-18 실측). store를 끄지 않으면 약 2초, 한꺼번에 받으면 음성 전체를 기다리는 시간이 더 든다.
    """
    pcm = _cached(text)
    if pcm is not None:
        yield pcm
        return
    pcm, pending = bytearray(), b""
    with _http.stream(
        "POST", f"{GMS}/generativelanguage.googleapis.com/v1beta/interactions",
        headers={"x-goog-api-key": api_key()},
        json={
            "model": VOICE["tts_model"],
            "stream": True,   # 다 만들 때까지 기다리지 않고 조각조각 받는다
            "store": False,   # 구글 서버에 요청을 저장하지 않는다
            "input": [{
                "type": "user_input",
                "content": [{
                    "type": "text",
                    "text": speakable(text),
                    "annotations": [{"type": "speech_metadata", "style": VOICE["tts_style"]}],
                }],
            }],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": VOICE["tts_voice"]}]},
        },
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                delta = json.loads(line[5:]).get("delta") or {}
            except ValueError:  # 마지막 줄 "data: [DONE]"
                continue
            if delta.get("type") != "audio":
                continue
            pending += base64.b64decode(delta["data"])
            if len(pending) >= CHUNK:
                cut = len(pending) & ~1  # 샘플(2바이트) 경계에서 자른다
                pcm += pending[:cut]
                yield pending[:cut]
                pending = pending[cut:]
    if len(pending) > 1:
        cut = len(pending) & ~1
        pcm += pending[:cut]
        yield pending[:cut]
    if not pcm:
        raise RuntimeError("음성이 오지 않았습니다")
    CACHE.mkdir(parents=True, exist_ok=True)
    with wave.open(str(_cache_path(text)), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(bytes(pcm))


def speak(text: str):
    """봇의 말을 음성 조각으로 차례로 돌려준다. 문장 단위로 저장해 두므로 반복되는 문장은 다시 만들지 않는다."""
    parts = [text] if text in FIXED else [s for s in re.split(r"(?<=[.?!])\s+", text) if s]
    for part in parts:
        cached = _cache_path(part).exists()
        started = time.perf_counter()
        try:
            for i, chunk in enumerate(tts(part)):
                clip = {"text": part, "pcm": base64.b64encode(chunk).decode(), "start": i == 0}
                if i == 0:  # 문장의 첫 조각에만 걸린 시간을 붙인다
                    clip |= {"cached": cached, "ms": round((time.perf_counter() - started) * 1000)}
                yield clip
        except Exception as e:  # 음성이 안 나와도 글은 화면에 남는다
            yield {"text": part, "error": f"{type(e).__name__} {e}"}


def warm():
    """고정 문구의 음성을 미리 만들어 둔다. 이미 있으면 아무 일도 하지 않는다."""
    for text in FIXED:
        try:
            for _ in tts(text):
                pass
        except Exception:
            pass  # 실패하면 그 문구를 실제로 말할 때 다시 시도한다
