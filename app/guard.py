"""출력 검사: 답에 나온 날짜와 긴 숫자가 툴 결과·응대 기준 문서에 있는 값인지 본다."""
import re

_DATES = [
    re.compile(r"(\d{1,2})월\s*(\d{1,2})일"),                # 11월 26일
    re.compile(r"\d{4}[-./](\d{1,2})[-./](\d{1,2})"),         # 2026-11-26
    re.compile(r"(?<![\d.])(\d{1,2})/(\d{1,2})(?![\d.])"),    # 11/26
]


def facts(text: str) -> set[str]:
    """글에서 날짜("11월 26일" 꼴로 통일)와 8자리 이상 숫자(송장번호·전화번호)를 뽑는다."""
    # 참고: "내일", "26일에"처럼 월이 없는 표현은 못 잡는다. 프롬프트로 막고 평가로 확인한다.
    found = {f"{int(m)}월 {int(d)}일" for pattern in _DATES for m, d in pattern.findall(text)}
    # "6400-1234-5678", "6400 1234 5678"처럼 3자리 이상 묶음으로 끊어 쓴 숫자도 붙여서 본다
    numbers = (re.sub(r"\D", "", n) for n in re.findall(r"\d+(?:[ -]\d{3,})+|\d{8,}", text))
    return found | {n for n in numbers if len(n) >= 8}


def violations(reply: str, allowed: set[str]) -> set[str]:
    return facts(reply) - allowed
