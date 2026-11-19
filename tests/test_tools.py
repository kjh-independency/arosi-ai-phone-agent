"""안전선 테스트. AI를 부르지 않는다(크레딧 0). 실행: python tests/test_tools.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import tools  # noqa: E402
from app.guard import facts, violations  # noqa: E402
from app.keypad import press  # noqa: E402
from app.session import Session  # noqa: E402

tools.REQUEST_LOG = None  # 테스트는 접수 기록 파일을 쓰지 않는다


def call(session, name, **args):
    return tools.run_tool(session, name, args)[0]


def verified(order_number):
    """주문번호를 키패드로 넣고 확인까지 마친 통화."""
    s = Session("test")
    for key in order_number + "#":
        press(s, key)
    s.turn = 1
    assert call(s, "verify_order", order_number=order_number)["ok"]
    return s


def submitted(s):
    return [(r["type"], r["order_number"]) for r in s.requests if r["type"] != "transfer"]


# ── 키패드 ──────────────────────────────────────────────

def test_keypad():
    s = Session("test")
    assert [press(s, k) for k in "100001"] == [None] * 6 and s.keypad_buffer == "100001"
    assert press(s, "#") == "100001" and s.keypad_entries == ["100001"] and s.keypad_buffer == ""
    assert press(s, "#") is None                      # 빈 채로 #
    press(s, "1"); press(s, "2"); press(s, "*")
    assert s.keypad_buffer == ""                      # * 은 지우기
    assert press(s, "9") == "9" and s.keypad_entries == ["100001"]   # 빈 상태의 9는 메뉴, 주문번호로 남지 않음
    press(s, "1")
    assert press(s, "9") is None and s.keypad_buffer == "19"          # 숫자 중간의 9는 그냥 숫자
    assert press(s, "back") is None and s.keypad_buffer == "1"        # back 은 한 자리 지우기
    press(s, "back"); press(s, "back")
    assert s.keypad_buffer == ""                                      # 빈 상태에서 눌러도 괜찮다


# ── 안전선 4: 키패드로 확인된 주문번호로만 ─────────────

def test_verify_only_from_keypad():
    s = Session("test")
    r = call(s, "verify_order", order_number="100001")   # 말로 들은 번호
    assert r["error"] == "not_from_keypad" and not s.verified_orders
    assert s.verify_failures == 0                        # 키패드 실패가 아니므로 횟수에 넣지 않음


def test_verify_failures_counted():
    s = Session("test")
    for entry in ("123456", "12345"):
        for key in entry + "#":
            press(s, key)
    r = call(s, "verify_order", order_number="123456")
    assert r["error"] == "not_found" and r["failed_attempts"] == 1
    r = call(s, "verify_order", order_number="12345")
    assert r["error"] == "invalid_format" and r["failed_attempts"] == 2
    assert not s.verified_orders


def test_unverified_order_is_closed():
    s = verified("100002")
    assert call(s, "get_delivery_status", order_number="100001")["error"] == "not_verified"
    assert call(s, "submit_request", request_type="cancel", order_number="100001", customer_confirmed=True)["error"] == "not_verified"


# ── 조회 ────────────────────────────────────────────────

def test_delivery_status():
    r = call(verified("100001"), "get_delivery_status", order_number="100001")
    assert r["order_status"] == "배송중" and r["shipments"][0]["eta"] == "2026-11-26" and not r["shipping_delayed"]
    assert not call(verified("100002"), "get_delivery_status", order_number="100002")["shipping_delayed"]   # 결제 4시간 전
    assert call(verified("100004"), "get_delivery_status", order_number="100004")["shipping_delayed"]       # 결제 5일 전
    assert not call(verified("100006"), "get_delivery_status", order_number="100006")["shipping_delayed"]   # 세트 일부 출고는 지연 아님
    r = call(verified("100008"), "get_delivery_status", order_number="100008")
    assert r["trackable"] is False and "shipments" not in r                                                  # 센터배송
    assert call(verified("100009"), "get_delivery_status", order_number="100009")["gift"]["eligible"]


# ── 안전선 2·3: 접수 ───────────────────────────────────

def test_cancel_needs_question_then_next_turn():
    s = verified("100002")
    s.turn = 2
    # 확인 질문 없이 바로 접수 → 거절
    assert call(s, "submit_request", request_type="cancel", order_number="100002", customer_confirmed=True)["error"] == "confirmation_required"
    # 같은 턴에 다시 → 여전히 거절
    assert call(s, "submit_request", request_type="cancel", order_number="100002", customer_confirmed=True)["error"] == "confirmation_required"
    assert submitted(s) == []
    # 고객이 답한 다음 턴 → 접수
    s.turn = 3
    r = call(s, "submit_request", request_type="cancel", order_number="100002", customer_confirmed=True)
    assert r["submitted"] and submitted(s) == [("cancel", "100002")]
    # 같은 요청 또 → 거절
    assert call(s, "submit_request", request_type="cancel", order_number="100002", customer_confirmed=False)["error"] == "already_submitted"


def test_dry_run_does_not_submit_and_expires():
    s = verified("100002")
    s.turn = 2
    r = call(s, "submit_request", request_type="cancel", order_number="100002", customer_confirmed=False)
    assert r["eligible"] and not r["submitted"] and submitted(s) == []
    s.turn = 4   # 한 턴을 건너뜀 (고객이 딴소리를 했다) → 다시 물어야 함
    assert call(s, "submit_request", request_type="cancel", order_number="100002", customer_confirmed=True)["error"] == "confirmation_required"
    s.turn = 5
    assert call(s, "submit_request", request_type="cancel", order_number="100002", customer_confirmed=True)["submitted"]


def test_confirmation_is_per_type_and_order():
    s = verified("100003")
    s.turn = 2
    call(s, "submit_request", request_type="return", order_number="100003", customer_confirmed=False)
    s.turn = 3   # 반품을 물어 놓고 파손으로 접수 → 거절
    assert call(s, "submit_request", request_type="damaged", order_number="100003", customer_confirmed=True)["error"] == "confirmation_required"


def test_shipped_order_cannot_be_cancelled():
    for number in ("100001", "100003", "100005", "100006"):   # 배송중, 배송완료, 세트 일부 출고 2가지
        s = verified(number)
        for turn, confirmed in ((2, False), (3, True), (4, True)):
            s.turn = turn
            r = call(s, "submit_request", request_type="cancel", order_number=number, customer_confirmed=confirmed)
            assert r["eligible"] is False and r["reason"] == "already_shipped", (number, r)
        assert submitted(s) == []


def test_eligibility_by_type():
    def reason(number, request_type):
        return call(verified(number), "submit_request", request_type=request_type, order_number=number, customer_confirmed=False).get("reason")

    assert reason("100003", "return") is None           # 배송완료 → 반품 가능
    assert reason("100001", "return") == "in_transit"
    assert reason("100002", "return") == "not_shipped"
    assert reason("100005", "return") == "in_transit"   # 세트 일부만 도착
    assert reason("100009", "gift_missing") is None
    assert reason("100003", "gift_missing") == "not_gift_eligible"
    assert reason("100003", "damaged") is None
    assert reason("100005", "damaged") is None           # 받은 박스가 하나라도 있으면 가능
    assert reason("100002", "damaged") == "not_delivered"
    for request_type in ("cancel", "return", "damaged"):
        assert reason("100008", request_type) == "channel_delivery"


def test_transfer_records_context():
    s = verified("100004")
    call(s, "get_delivery_status", order_number="100004")
    assert call(s, "transfer_to_agent", reason="exception_case", summary="출고 지연 문의")["ok"]
    record = s.requests[-1]
    assert s.transferred and record["verified_orders"] == ["100004"]
    assert record["last_status"]["100004"]["shipping_delayed"]   # 조회 결과가 함께 남는다


def test_schemas_are_strict():
    for tool in tools.TOOLS:
        schema = tool["input_schema"]
        assert tool["strict"] and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"]) and tool["name"] in tools._RUN


# ── 안전선 1: 출력 검사 ────────────────────────────────

def test_guard():
    s = verified("100001")
    result = call(s, "get_delivery_status", order_number="100001")
    allowed = facts(str(result)) | facts((tools.ROOT / "data" / "reference.md").read_text(encoding="utf-8"))
    assert violations("CJ대한통운으로 배송 중이고, 11월 26일 도착 예정입니다.", allowed) == set()
    assert violations("송장번호는 6400-1234-5678입니다.", allowed) == set()
    assert violations("송장번호는 6400 1234 5678입니다.", allowed) == set()                 # 띄어 쓴 송장
    assert violations("송장번호는 6400 9999 8888입니다.", allowed) == {"640099998888"}      # 띄어 쓴 가짜 송장
    assert violations("주문번호 100001, 11월 26일 도착 예정입니다.", allowed) == set()        # 주문번호와 날짜가 붙어도 오탐 없음
    assert violations("블랙프라이데이 행사는 11월 30일까지입니다.", allowed) == set()          # 문서의 날짜
    assert violations("11월 27일 도착 예정입니다.", allowed) == {"11월 27일"}                # 지어낸 날짜
    assert violations("11/28에 도착합니다.", allowed) == {"11월 28일"}
    assert violations("송장번호는 640099998888입니다.", allowed) == {"640099998888"}        # 지어낸 송장
    assert violations("고객님 번호는 010-0000-0001입니다.", allowed) == {"01000000001"}     # 조회 결과에 없는 전화번호
    assert violations("보통 1~2영업일 안에 출고되고, 환불은 3~5일 걸립니다.", allowed) == set()


if __name__ == "__main__":
    tests = [f for name, f in list(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"통과  {test.__name__}")
    print(f"\n{len(tests)}개 모두 통과")
