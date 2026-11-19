"""통화 한 건의 상태. 코드가 관리하고, 모델은 툴을 통해서만 바꿀 수 있다."""
from dataclasses import dataclass, field


@dataclass
class Session:
    id: str
    messages: list = field(default_factory=list)        # Claude API 대화 기록 (뒤에 덧붙이기만 한다)
    turn: int = 0                                        # 고객 입력 횟수
    keypad_buffer: str = ""                              # 아직 제출되지 않은 숫자
    keypad_entries: list = field(default_factory=list)   # 키패드로 제출된 숫자
    verified_orders: set = field(default_factory=set)    # 본인 확인이 끝난 주문번호
    verify_failures: int = 0
    pending_confirm: tuple | None = None                 # 접수 가능 확인을 받은 (종류, 주문번호, 턴)
    requests: list = field(default_factory=list)         # 이 통화의 접수·상담원 연결 기록
    last_status: dict = field(default_factory=dict)      # 주문번호 → 마지막 조회 결과
    allowed_facts: set = field(default_factory=set)      # 말해도 되는 날짜·긴 숫자 (출력 검사용)
    transferred: bool = False
