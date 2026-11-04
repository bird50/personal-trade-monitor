import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    webhook_token: str = field(repr=False)
    api_token: str = field(repr=False)
    database_path: Path = ROOT / "data/trading.db"
    allowed_symbols: tuple[str, ...] = ("OANDA:XAUUSD",)
    max_signal_age_seconds: int = 300
    max_future_seconds: int = 30
    max_body_bytes: int = 16384
    webhook_rate_per_minute: int = 120
    ai_enabled: bool = False
    lm_studio_url: str = "http://172.17.185.101:1234"
    lm_studio_model: str = "qwen/qwen3.8-27b"
    ai_timeout_seconds: int = 90
    ai_max_attempts: int = 3
    ai_max_signal_age_seconds: int = 300
    ai_reasoning_effort: str | None = None
    ai_provider: str = "lm_studio"
    codex_model: str = "gpt-6-astra"
    codex_cli_path: str | None = None
    risk_policy_path: Path = ROOT / "config/risk-paper.json"

    def __post_init__(self):
        for token in (self.webhook_token, self.api_token):
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
                raise ValueError("Configure separate URL-safe tokens (32–128 characters)")
        if self.webhook_token == self.api_token:
            raise ValueError("WEBHOOK_TOKEN and API_TOKEN must be different")
        if not self.allowed_symbols:
            raise ValueError("ALLOWED_SYMBOLS must not be empty")
        if min(self.max_signal_age_seconds, self.max_body_bytes, self.webhook_rate_per_minute) <= 0:
            raise ValueError("Age, body size and rate limits must be positive")
        if self.max_future_seconds < 0:
            raise ValueError("Future tolerance must not be negative")
        endpoint = urlsplit(self.lm_studio_url)
        if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ("", "/"):
            raise ValueError("LM_STUDIO_URL must be a base HTTP(S) URL without credentials or path")
        if not self.lm_studio_model or min(self.ai_timeout_seconds, self.ai_max_attempts, self.ai_max_signal_age_seconds) <= 0:
            raise ValueError("AI model, timeout, attempts and signal age must be configured")
        if self.ai_reasoning_effort not in (None, "none", "low", "medium", "high"):
            raise ValueError("AI_REASONING_EFFORT must be none, low, medium, high or server_default")
        if self.ai_provider not in ('lm_studio','codex'):
            raise ValueError('AI_PROVIDER must be lm_studio or codex')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,99}',self.codex_model):
            raise ValueError('Invalid CODEX_MODEL')
        if self.codex_cli_path is not None and not Path(self.codex_cli_path).is_absolute():
            raise ValueError('CODEX_CLI_PATH must be an absolute path')

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        db = Path(os.getenv("DATABASE_PATH", "data/trading.db"))
        risk = Path(os.getenv("RISK_POLICY_PATH", "config/risk-paper.json"))
        return cls(
            webhook_token=os.getenv("WEBHOOK_TOKEN", ""),
            api_token=os.getenv("API_TOKEN", ""),
            database_path=db if db.is_absolute() else ROOT / db,
            allowed_symbols=tuple(s.strip() for s in os.getenv("ALLOWED_SYMBOLS", "OANDA:XAUUSD").split(",") if s.strip()),
            max_signal_age_seconds=int(os.getenv("MAX_SIGNAL_AGE_SECONDS", "300")),
            max_future_seconds=int(os.getenv("MAX_FUTURE_SECONDS", "30")),
            max_body_bytes=int(os.getenv("MAX_BODY_BYTES", "16384")),
            webhook_rate_per_minute=int(os.getenv("WEBHOOK_RATE_PER_MINUTE", "120")),
            ai_enabled=os.getenv("AI_ENABLED", "false").lower() == "true",
            lm_studio_url=os.getenv("LM_STUDIO_URL", "http://172.17.185.101:1234").rstrip("/"),
            lm_studio_model=os.getenv("LM_STUDIO_MODEL", "qwen/qwen3.8-27b"),
            ai_timeout_seconds=int(os.getenv("AI_TIMEOUT_SECONDS", "90")),
            ai_max_attempts=int(os.getenv("AI_MAX_ATTEMPTS", "3")),
            ai_max_signal_age_seconds=int(os.getenv("AI_MAX_SIGNAL_AGE_SECONDS", "300")),
            ai_reasoning_effort=None if os.getenv("AI_REASONING_EFFORT", "none").lower() == "server_default" else os.getenv("AI_REASONING_EFFORT", "none").lower(),
            ai_provider=os.getenv('AI_PROVIDER','lm_studio'),
            codex_model=os.getenv('CODEX_MODEL','gpt-6-astra'),
            codex_cli_path=os.getenv('CODEX_CLI_PATH') or None,
            risk_policy_path=risk if risk.is_absolute() else ROOT / risk,
        )
