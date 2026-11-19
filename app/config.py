"""config.toml(설정)과 .env(비밀값)를 읽는다. 공급자·모델·주소는 여기로만 들어온다."""
import os
import tomllib
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
CONFIG = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))


def api_key() -> str:
    name = CONFIG["provider"]["key_env"]
    key = os.environ.get(name)
    if not key:
        raise SystemExit(f".env에 {name} 값이 없습니다. .env.example을 .env로 복사한 뒤 키를 넣어 주세요.")
    return key


def claude_client(key: str | None = None) -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=key or api_key(),
        base_url=CONFIG["provider"]["anthropic_base_url"],
        timeout=CONFIG["agent"]["timeout_sec"],
        max_retries=1,
    )
