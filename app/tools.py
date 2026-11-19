"""툴 정의와 실행. 넘으면 안 되는 선은 전부 여기서 막는다."""
import json
import uuid
from datetime import datetime, timedelta

from app.config import CONFIG, ROOT

ORDERS = {o["order_number"]: o for o in json.loads((ROOT / "data" / "orders.json").read_text(encoding="utf-8"))}
NOW = datetime.fromisoformat(CONFIG["demo"]["now"]).replace(tzinfo=None)  # 데모의 "지금"
SHIP_DELAY = timedelta(hours=CONFIG["demo"]["ship_delay_hours"])
ORDER_NUMBER_LENGTH = CONFIG["keypad"]["order_number_length"]
REQUEST_LOG = ROOT / "logs" / "requests.jsonl"  # 테스트는 None으로 바꿔 파일을 쓰지 않는다
PRE_SHIP = {"결제완료", "상품준비중"}


def _order_number(description):
    return {"type": "string", "description": description}


def _schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS = [
    {
        "name": "verify_order",
        "description": "고객이 키패드로 입력한 주문번호로 주문을 확인한다. 주문 정보를 말하거나 다른 툴을 쓰기 전에 "
        "반드시 먼저 성공해야 한다. [키패드]로 들어온 번호만 받는다. [음성]으로 들은 번호는 거절된다.",
        "strict": True,
        "input_schema": _schema({"order_number": _order_number("[키패드]로 들어온 주문번호")}, ["order_number"]),
    },
    {
        "name": "get_delivery_status",
        "description": "확인된 주문의 현재 상태를 조회한다: 주문 상태, 구매 채널, 박스별 택배사·송장번호·도착 예정일, "
        "출고 지연 여부, 사은품 대상 여부. 배송·송장·사은품 문의에 쓴다.",
        "strict": True,
        "input_schema": _schema({"order_number": _order_number("확인된 주문번호")}, ["order_number"]),
    },
    {
        "name": "submit_request",
        "description": "취소·반품·사은품 누락·파손/불량 요청을 접수한다. 두 번에 나눠 부른다. "
        "1) customer_confirmed=false: 접수 가능한지만 확인한다(접수되지 않는다). 가능하면 고객에게 접수할지 묻는다. "
        "2) 고객이 '네'라고 답한 바로 다음 턴에 customer_confirmed=true로 다시 부르면 접수된다.",
        "strict": True,
        "input_schema": _schema(
            {
                "request_type": {
                    "type": "string",
                    "enum": ["cancel", "return", "gift_missing", "damaged"],
                    "description": "cancel=취소, return=반품, gift_missing=사은품 누락, damaged=파손·불량",
                },
                "order_number": _order_number("확인된 주문번호"),
                "customer_confirmed": {"type": "boolean", "description": "고객이 접수에 '네'라고 답했으면 true"},
            },
            ["request_type", "order_number", "customer_confirmed"],
        ),
    },
    {
        "name": "transfer_to_agent",
        "description": "상담원에게 연결한다. 문의 내용과 조회 결과가 함께 기록된다. 부른 뒤 연결 안내를 말하면 통화가 넘어간다.",
        "strict": True,
        "input_schema": _schema(
            {
                "reason": {
                    "type": "string",
                    "enum": [
                        "customer_request", "strong_complaint", "recognition_failed", "verification_failed",
                        "sensitive_topic", "exception_case", "unsupported_request", "other",
                    ],
                    "description": "customer_request=고객 요청, strong_complaint=강한 불만, recognition_failed=말을 2번 못 알아들음, "
                    "verification_failed=주문번호 조회 실패 또는 주문번호 모름, sensitive_topic=피부 트러블·성분·재입고, "
                    "exception_case=출고 지연·배송 완료인데 못 받음·세트가 전부 배송 완료인데 일부만 받음,unsupported_request=주문 관련이지만 처리할 수 없는 요청",
                },
                "summary": {"type": "string", "description": "상담원이 읽을 문의 내용 한두 문장"},
            },
            ["reason", "summary"],
        ),
    },
]


def _fail(error, message, **extra):
    return {"ok": False, "error": error, "message": message, **extra}, True


def order_status(order):
    statuses = {s["status"] for s in order["shipments"]}
    if not statuses:
        return "조회 불가"
    if len(statuses) == 1:
        return statuses.pop()
    return "상품준비중" if statuses <= PRE_SHIP else "배송중"


def _summary(order):
    return {
        "order_number": order["order_number"],
        "ordered_at": order["ordered_at"][:10],
        "items": [item["name"] for item in order["items"]],
        "channel": order["channel"],
    }


def _verified(session, order_number):
    if order_number in session.verified_orders:
        return ORDERS[order_number]
    return None


NOT_VERIFIED = ("not_verified", "본인 확인이 되지 않은 주문입니다. 고객에게 주문번호를 키패드로 입력해 달라고 안내하세요.")


def _record(session, entry):
    entry = {"request_id": f"R-{uuid.uuid4().hex[:8]}", "session_id": session.id,
             "created_at": datetime.now().isoformat(timespec="seconds"), **entry}
    session.requests.append(entry)
    if REQUEST_LOG:
        REQUEST_LOG.parent.mkdir(exist_ok=True)
        with REQUEST_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def verify_order(session, order_number):
    if order_number not in session.keypad_entries:
        return _fail("not_from_keypad", "키패드로 입력된 번호가 아닙니다. 주문번호를 키패드로 입력하고 #을 눌러 달라고 안내하세요.")
    retry = "실패가 1회면 다시 입력을 요청하고, 2회 이상이면 상담원에게 연결하세요."
    if len(order_number) != ORDER_NUMBER_LENGTH:
        session.verify_failures += 1
        return _fail("invalid_format", f"주문번호는 {ORDER_NUMBER_LENGTH}자리입니다. {retry}", failed_attempts=session.verify_failures)
    if order_number not in ORDERS:
        session.verify_failures += 1
        return _fail("not_found", f"없는 주문번호입니다. {retry}", failed_attempts=session.verify_failures)
    session.verified_orders.add(order_number)
    order = ORDERS[order_number]
    return {"ok": True, "order": {**_summary(order), "status": order_status(order)}}, False


def get_delivery_status(session, order_number):
    order = _verified(session, order_number)
    if not order:
        return _fail(*NOT_VERIFIED)
    result = {"ok": True, **_summary(order), "fulfillment": order["fulfillment"], "gift": order["gift"]}
    if order["fulfillment"] == "센터배송":
        result["trackable"] = False
    else:
        not_shipped = all(s["status"] in PRE_SHIP for s in order["shipments"])
        result |= {
            "order_status": order_status(order),
            "paid_at": order["paid_at"],
            "shipping_delayed": not_shipped and NOW - datetime.fromisoformat(order["paid_at"]) > SHIP_DELAY,
            "shipments": order["shipments"],
        }
    session.last_status[order_number] = result
    return result, False


def _ineligible(request_type, order):
    """접수할 수 없으면 이유를, 접수할 수 있으면 None을 돌려준다. 상태는 매번 더미 DB에서 읽는다."""
    statuses = [s["status"] for s in order["shipments"]]
    if request_type == "gift_missing":
        return None if order["gift"]["eligible"] else "not_gift_eligible"
    if order["fulfillment"] == "센터배송":
        return "channel_delivery"
    if request_type == "cancel":
        return None if all(s in PRE_SHIP for s in statuses) else "already_shipped"
    if request_type == "return":
        if all(s == "배송완료" for s in statuses):
            return None
        return "not_shipped" if all(s in PRE_SHIP for s in statuses) else "in_transit"
    return None if "배송완료" in statuses else "not_delivered"  # damaged


def submit_request(session, request_type, order_number, customer_confirmed):
    order = _verified(session, order_number)
    if not order:
        return _fail(*NOT_VERIFIED)
    if any(r["type"] == request_type and r.get("order_number") == order_number for r in session.requests):
        return _fail("already_submitted", "이 통화에서 이미 접수한 요청입니다.")
    reason = _ineligible(request_type, order)
    if reason:
        return {"ok": True, "eligible": False, "submitted": False, "reason": reason, "channel": order["channel"]}, False
    key = (request_type, order_number)
    pending, session.pending_confirm = session.pending_confirm, (*key, session.turn)
    ask = {"ordered_at": order["ordered_at"][:10], "items": [item["name"] for item in order["items"]]}
    if not customer_confirmed:
        return {"ok": True, "eligible": True, "submitted": False, "ask_customer": ask,
                "message": "아직 접수되지 않았습니다. 주문일과 상품명을 넣어 접수할지 고객에게 물으세요."}, False
    if pending != (*key, session.turn - 1):
        return _fail("confirmation_required", "접수 전에 고객 확인이 필요합니다. 주문일과 상품명을 넣어 접수할지 묻고, "
                     "고객이 '네'라고 답한 다음 턴에 다시 부르세요.", ask_customer=ask)
    session.pending_confirm = None
    entry = _record(session, {"type": request_type, "order_number": order_number, "status": "접수"})
    return {"ok": True, "submitted": True, "request_id": entry["request_id"], "status": "접수",
            "message": "접수되었습니다. 처리 결과는 확인 후 안내된다고 말하세요."}, False


def transfer_to_agent(session, reason, summary):
    _record(session, {"type": "transfer", "reason": reason, "summary": summary,
                      "verified_orders": sorted(session.verified_orders), "last_status": session.last_status})
    session.transferred = True
    return {"ok": True, "message": "연결 안내를 말하면 통화가 상담원에게 넘어갑니다."}, False


_RUN = {f.__name__: f for f in (verify_order, get_delivery_status, submit_request, transfer_to_agent)}


def run_tool(session, name, args):
    """툴을 실행해 (결과, 오류 여부)를 돌려준다."""
    if name not in _RUN:
        return _fail("unknown_tool", f"없는 툴입니다: {name}")
    try:
        return _RUN[name](session, **args)
    except TypeError as e:  # 입력 이름이 틀렸을 때
        return _fail("invalid_input", str(e))
