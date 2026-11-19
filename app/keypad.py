"""키패드 입력 모듈. 웹 버튼도, 나중의 전화 키패드(DTMF)도 press()만 부르면 된다."""


def press(session, key: str) -> str | None:
    """키 하나를 받는다. 제출이 일어나면 제출된 숫자를, 아니면 None을 돌려준다."""
    if key == "*":
        session.keypad_buffer = ""
    elif key == "back":  # 한 자리 지우기. 데모 화면 전용 (전화 키패드에는 없다)
        session.keypad_buffer = session.keypad_buffer[:-1]
    elif key == "#":
        entry, session.keypad_buffer = session.keypad_buffer, ""
        if entry:
            session.keypad_entries.append(entry)
            return entry
    elif key == "9" and not session.keypad_buffer:
        return "9"  # 행사·상품 문의 메뉴. 주문번호는 9로 시작하지 않는다
    elif key.isdigit():
        session.keypad_buffer += key
    return None
