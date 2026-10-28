"""Settings for the engine (FastAPI + Pydantic AI).

All secrets come from the environment / repo-root ``.env``. Nothing here is ever
sent to the browser.
"""

from __future__ import annotations

import functools
import json
import pathlib
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Supabase
    supabase_url: str = ""
    next_public_supabase_publishable_key: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_storage_bucket: str = "documents"
    database_url: str = ""

    # Engine
    engine_env: Literal["development", "test", "production"] = "development"
    engine_api_url: str = "http://127.0.0.1:8088"
    engine_service_token: str = "dev-engine-service-token"
    engine_product_credentials: dict[str, str] = Field(default_factory=dict)

    # Models
    openai_api_key: str = ""
    openai_base_url: str = "https://freebuff.com/api/v1/paid"
    extraction_model: str = "gpt-4o-mini"
    vision_model: str = "gpt-4o"

    # Inngest
    inngest_event_key: str = ""
    inngest_signing_key: str = ""
    inngest_dev: bool = True

    # Langfuse
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # GSTN through an empanelled GSP (Appendix B)
    gsp_provider: str = ""
    gsp_client_id: str = ""
    gsp_client_secret: str = ""
    gsp_base_url: str = ""
    gsp_lookup_timeout_seconds: int = 8
    gsp_use_fixture: bool = True

    # Bridge agent (Appendix E)
    agent_command_signing_key: str = ""
    agent_command_public_key: str = ""
    agent_command_ttl_seconds: int = 900
    agent_heartbeat_grace_seconds: int = 300

    # Autonomy defaults (section 12a — settings, not hard-coded gates)
    default_confidence_threshold: Decimal = Decimal("0.93")
    default_autopost_value_cap: Decimal = Decimal("50000")
    default_daily_autopost_cap: int = 200
    default_error_rate_limit: Decimal = Decimal("0.005")

    packs_dir: pathlib.Path = ROOT / "packages" / "packs"

    @field_validator("engine_product_credentials", mode="before")
    @classmethod
    def _parse_product_credentials(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return {}
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:  # pragma: no cover - config error
                raise ValueError(f"ENGINE_PRODUCT_CREDENTIALS is not valid JSON: {exc}") from exc
        return value

    @property
    def llm_enabled(self) -> bool:
        """True when a model is configured, so the Pydantic AI agents can run."""
        return bool(self.openai_api_key)

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
