"""Application settings. Environment is read here and nowhere else."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AI Revenue Recovery"
    version: str = "0.1.0"
    log_level: str = "INFO"

    # Razorpay test mode. Empty secret => signature check is skipped and every
    # request is logged as unverified. Never leave it empty outside local dev.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # MySQL (team choice). The two footguns from docs/DATA_MODEL.md are
    # neutralised at the connection layer: session time_zone pinned to +00:00,
    # charset utf8mb4. See repositories/database.py.
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "recovery"

    caps_path: Path = PROJECT_ROOT / "caps.yaml"

    # Voice slice. Empty => text-mode simulation only (still full pipeline).
    sarvam_api_key: str = ""
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # baseline = industry standard (retry tomorrow x3, fixed ladder).
    # agentic  = diagnose -> gate -> score. Same pipeline; the policy swaps.
    default_strategy: str = "agentic"
    holdout_fraction: float = 0.15


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
