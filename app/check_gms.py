"""GMS 키와 모델 연결 확인. 실행: python -m app.check_gms  (크레딧 소량 사용)
"""
import json
import sys
import urllib.request

import anthropic

from app.config import CONFIG, api_key, claude_client

sys.stdout.reconfigure(errors="replace")  # 콘솔이 못 찍는 글자가 나와도 멈추지 않게

MODEL = CONFIG["agent"]["model"]
MAX_TOKENS = CONFIG["agent"]["max_tokens"]
GMS = CONFIG["provider"]["gms_base"]

TOOL = {
    "name": "get_order_status",
    "description": "주문번호로 주문의 배송 상태를 조회한다.",
    "input_schema": {
        "type": "object",
        "properties": {"order_number": {"type": "string", "description": "6자리 주문번호"}},
        "required": ["order_number"],
        "additionalProperties": False,
    },
}
QUESTION = "주문번호 100001 배송 상태를 조회해서 알려 주세요."
FAKE_RESULT = {"order_number": "100001", "status": "배송중", "carrier": "CJ대한통운", "eta": "2026-11-26"}
LONG_SYSTEM = "아로시 전화 상담 검증용 안내문입니다.\n" + "\n".join(
    f"{i}. 이 줄은 프롬프트 캐싱을 확인하려고 넣은 채움 글입니다." for i in range(200)
)


def get_json(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as res:
        return json.load(res)


def find(data, name):
    """응답 모양을 모르므로 이름으로 값을 찾는다."""
    if isinstance(data, dict):
        if name in data:
            return data[name]
        data = list(data.values())
    if isinstance(data, list):
        for item in data:
            if (found := find(item, name)) is not None:
                return found
    return None


def text_of(response):
    return " ".join(b.text for b in response.content if b.type == "text").strip()


def check_headers(client):
    headers = {k.lower(): v for k, v in client.default_headers.items()}
    assert "x-api-key" in headers and headers.get("anthropic-version") == "2023-06-01", sorted(headers)
    return "x-api-key + anthropic-version 2023-06-01"


def credit(key):
    info = get_json(f"{GMS}/key-info", {"Authorization": f"Bearer {key}"})
    return {name: find(info, name) for name in ("totalCredit", "usedCredit", "remainCredit", "expiredDate")}


def check_plain(client):
    r = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS, messages=[{"role": "user", "content": "한 문장으로 인사해 주세요."}]
    )
    return f"응답 모델 {r.model}, 입력 {r.usage.input_tokens} / 출력 {r.usage.output_tokens} 토큰 | {text_of(r)}"


def check_tools(client, strict):
    tool = {**TOOL, "strict": True} if strict else TOOL
    messages = [{"role": "user", "content": QUESTION}]
    called = False
    for _ in range(3):
        r = client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, tools=[tool], messages=messages)
        if r.stop_reason != "tool_use":
            break
        called = True
        messages.append({"role": "assistant", "content": r.content})  # 생각 블록까지 그대로 돌려보낸다
        messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(FAKE_RESULT, ensure_ascii=False)}
                for b in r.content if b.type == "tool_use"
            ],
        })
    answer = text_of(r)
    assert called, f"모델이 툴을 부르지 않음: {answer}"
    assert "CJ" in answer or "26" in answer, f"툴 결과가 답에 없음: {answer}"
    return answer


def check_effort(client):
    effort = CONFIG["agent"]["effort"]
    r = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS, output_config={"effort": effort},
        messages=[{"role": "user", "content": "한 문장으로 인사해 주세요."}],
    )
    return f"effort={effort} 통과, 출력 {r.usage.output_tokens} 토큰"


def check_cache(client):
    usages = []
    for _ in range(2):
        r = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, cache_control={"type": "ephemeral"}, system=LONG_SYSTEM,
            messages=[{"role": "user", "content": "한 문장으로 인사해 주세요."}],
        )
        usages.append(r.usage)
    written, read = usages[0].cache_creation_input_tokens, usages[1].cache_read_input_tokens
    assert read, f"두 번째 호출에서 캐시를 읽지 못함 (1차 쓰기 {written}, 2차 읽기 {read})"
    return f"1차 쓰기 {written} 토큰, 2차 읽기 {read} 토큰"


def check_openai_models(key):
    data = get_json(f"{GMS}/api.openai.com/v1/models", {"Authorization": f"Bearer {key}"})
    return sorted(m["id"] for m in data["data"])


def check_gemini_models(key):
    data = get_json(f"{GMS}/generativelanguage.googleapis.com/v1beta/models", {"x-goog-api-key": key})
    return sorted(m["name"] for m in data["models"])


def run(name, fn, *args):
    try:
        result = fn(*args)
        print(f"[OK]   {name}: {result}")
        return result
    except anthropic.APIStatusError as e:
        print(f"[FAIL] {name}: HTTP {e.status_code} {e.message}")
    except anthropic.APIConnectionError as e:
        print(f"[FAIL] {name}: 연결 실패 {e}")
    except Exception as e:  # urllib 오류, assert 실패 등
        print(f"[FAIL] {name}: {type(e).__name__} {e}")
    return None


if __name__ == "__main__":
    key = api_key()
    client = claude_client(key)
    print(f"공급자 주소: {client.base_url} / 모델: {MODEL}\n")

    run("1. SDK 헤더", check_headers, client)
    before = run("5. 크레딧(전)", credit, key)
    run("2. 기본 호출", check_plain, client)
    run("3. 툴 호출", check_tools, client, False)
    run("4a. strict 툴", check_tools, client, True)
    run("4b. effort", check_effort, client)
    run("4c. 프롬프트 캐싱", check_cache, client)
    after = run("5. 크레딧(후)", credit, key)
    if before and after and None not in (before["remainCredit"], after["remainCredit"]):
        print(f"       이번 검증에 쓴 크레딧: {before['remainCredit'] - after['remainCredit']}")
    run("6. OpenAI 모델 목록", check_openai_models, key)
    run("6. Gemini 모델 목록", check_gemini_models, key)
