"""Central configuration.

Every setting comes from an environment variable so that no secret ever lands in
the repository. Import the singleton ``settings``; do not read os.environ elsewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # .../backend
DATA_DIR = BASE_DIR / "data"


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # --- LLM -------------------------------------------------------------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    anthropic_fast_model: str = "claude-haiku-4-5-20251001"
    anthropic_version: str = "2023-06-01"
    anthropic_base_url: str = "https://api.anthropic.com"
    llm_timeout_seconds: float = 45.0
    llm_max_retries: int = 2

    # --- uploads ---------------------------------------------------------
    max_upload_mb: int = 10
    max_uploads_per_day: int = 40
    allowed_upload_types: tuple[str, ...] = (
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "application/pdf",
        "text/plain",
    )

    # --- http ------------------------------------------------------------
    allowed_origins: list[str] = field(default_factory=list)

    # --- trust signals ---------------------------------------------------
    helpline_bn: str = "16135 (যাচাই করা হয়নি)"
    helpline_verified: bool = False
    partner_name_bn: str = ""
    partner_logo_url: str = ""

    # --- data ------------------------------------------------------------
    data_dir: Path = DATA_DIR
    agency_data_is_official: bool = False

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def agencies_csv(self) -> Path:
        return self.data_dir / "agencies.csv"

    @property
    def fee_caps_json(self) -> Path:
        return self.data_dir / "fee_caps.json"

    @property
    def rule_cards_md(self) -> Path:
        return self.data_dir / "rule_cards.md"

    @property
    def act_chunks_dir(self) -> Path:
        return self.data_dir / "act_chunks"

    @property
    def mode(self) -> str:
        return "llm" if self.llm_enabled else "offline"


def _load() -> Settings:
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip(),
        anthropic_fast_model=os.environ.get(
            "ANTHROPIC_FAST_MODEL", "claude-haiku-4-5-20251001"
        ).strip(),
        anthropic_version=os.environ.get("ANTHROPIC_VERSION", "2023-06-01").strip(),
        anthropic_base_url=os.environ.get(
            "ANTHROPIC_BASE_URL", "https://api.anthropic.com"
        ).strip(),
        llm_timeout_seconds=_float("LLM_TIMEOUT_SECONDS", 45.0),
        llm_max_retries=_int("LLM_MAX_RETRIES", 2),
        max_upload_mb=_int("MAX_UPLOAD_MB", 10),
        max_uploads_per_day=_int("MAX_UPLOADS_PER_DAY", 40),
        allowed_origins=_list(
            "ALLOWED_ORIGINS", ["http://localhost:8000", "http://127.0.0.1:8000"]
        ),
        helpline_bn=os.environ.get("HELPLINE_BN", "16135 (যাচাই করা হয়নি)").strip(),
        helpline_verified=_bool("HELPLINE_VERIFIED", False),
        partner_name_bn=os.environ.get("PARTNER_NAME_BN", "").strip(),
        partner_logo_url=os.environ.get("PARTNER_LOGO_URL", "").strip(),
        data_dir=Path(os.environ.get("FRAUD_SHIELD_DATA_DIR", str(DATA_DIR))),
        agency_data_is_official=_bool("AGENCY_DATA_IS_OFFICIAL", False),
    )


settings = _load()
