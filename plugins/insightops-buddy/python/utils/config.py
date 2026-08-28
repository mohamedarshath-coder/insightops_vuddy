import os
from pathlib import Path

from dotenv import load_dotenv

_loaded = False


def load_config() -> None:
    global _loaded
    if _loaded:
        return
    env_path = Path(__file__).parents[2] / ".env"
    if env_path.exists():
        # override=True: without it, python-dotenv silently skips any key already present in
        # os.environ, even a stale/placeholder one set by something else in the shell -- this
        # repo's own .env should always win.
        load_dotenv(env_path, override=True)
    _loaded = True


def require(key: str) -> str:
    load_config()
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. Check your .env file."
        )
    return value


def get(key: str, default: str = "") -> str:
    load_config()
    return os.getenv(key, default)
