"""터미널 채팅. 실행: python -m app.cli [--model 모델ID]

숫자·#·*만 친 줄은 키패드 입력, 그 밖의 줄은 고객의 말로 받는다.
  100001#        키패드로 주문번호 입력
  9              키패드 9번 (행사·상품 문의)
  v:100001이요   숫자뿐인 말을 "말"로 보내고 싶을 때
  !fail          음성 인식 실패를 흉내 낸다
  q              종료
"""
import argparse
import re
import sys

from app import agent
from app.keypad import press
from app.prompt import OPENING

sys.stdout.reconfigure(errors="replace")


def show(events):
    """에이전트가 내보낸 것을 화면에 찍는다. 통화가 끝났으면 True."""
    for kind, value in events:
        if kind == "filler":
            print(f"  봇(채움)> {value}")
        elif kind == "reply":
            print(f"  봇> {value}")
        elif kind == "timing":
            print(f"     (첫 응답 {value['first_response_ms']}ms, 전체 {value['total_ms']}ms)")
        elif kind == "end":
            print("  [상담원에게 연결됨. 통화 종료]")
            return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None, help="config.toml의 모델 대신 쓸 모델 ID")
    model = parser.parse_args().model

    session = agent.new_session()
    print(f"통화 {session.id} (모델: {model or agent.AGENT['model']})\n  봇> {OPENING}")
    while True:
        try:
            line = input("고객> ").strip()
        except EOFError:
            break
        if line == "q":
            break
        if not line:
            continue
        if re.fullmatch(r"[0-9*#]+", line):
            ended = False
            for key in line:
                entry = press(session, key)
                if entry:
                    ended = show(agent.handle(session, "keypad", entry, model))
            if session.keypad_buffer:
                print(f"     (입력 중: {session.keypad_buffer} — #을 눌러야 제출됩니다)")
        elif line == "!fail":
            ended = show(agent.handle(session, "system", "음성 인식 실패", model))
        else:
            ended = show(agent.handle(session, "voice", line.removeprefix("v:"), model))
        if ended:
            break
    if not session.transferred:
        agent.log(session, "call_end", reason="hangup")
    print(f"로그: logs/calls/{session.id}.jsonl")


if __name__ == "__main__":
    main()
