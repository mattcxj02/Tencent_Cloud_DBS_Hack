"""Per-service settings with least-privilege credential loading.

Each service loads ONLY the secrets it needs. The gateway gets TRTC + Langfuse;
the agent and validator get LLM + Langfuse. The gateway and validator also share
VALIDATOR_SERVICE_TOKEN; the agent never receives it. Variable names are aliased
to match the user's existing .env exactly.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)


class _BaseSettings(BaseSettings):
    """Common base — reads from .env, is case-insensitive, ignores extra vars."""

    model_config = _ENV_CONFIG


class LangfuseSettings(_BaseSettings):
    """Langfuse credentials. Mixed into services that need observability."""

    langfuse_public_key: str | None = Field(default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(default=None, alias="LANGFUSE_SECRET_KEY")
    langfuse_base_url: str | None = Field(default=None, alias="LANGFUSE_BASE_URL")

    @property
    def langfuse_enabled(self) -> bool:
        return bool(
            self.langfuse_public_key and self.langfuse_secret_key and self.langfuse_base_url
        )


LLMProvider = Literal["tokenhub", "deepseek"]

LLM_PROVIDER_URLS: dict[str, str] = {
    "tokenhub": "https://tokenhub-intl.tencentcloudmaas.com/v1",
    "deepseek": "https://api.deepseek.com",
}


class LLMSettings(_BaseSettings):
    """OpenAI-compatible provider keys. Each service selects one provider via `llm_provider`.

    In local dev both keys sit in the shared .env; T-04 containers receive only the key
    for their configured provider.
    """

    tokenhub_key: str | None = Field(default=None, alias="tokenhub_key")
    deepseek_key: str | None = Field(default=None, alias="deepseek_key")
    llm_provider: LLMProvider = "tokenhub"

    @property
    def llm_api_key(self) -> str | None:
        return self.tokenhub_key if self.llm_provider == "tokenhub" else self.deepseek_key

    @property
    def llm_base_url(self) -> str:
        return LLM_PROVIDER_URLS[self.llm_provider]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)


class TRTCSettings(_BaseSettings):
    """Gateway-only TRTC ASR credentials."""

    trtc_sdk_app_id: str | None = Field(default=None, alias="SDKAppID")
    trtc_sdk_secret_key: str | None = Field(default=None, alias="SDKSecretKey")
    trtc_asr_host: str = Field(default="asr-intl.cloud-rtc.com", alias="TRTC_ASR_HOST")

    @property
    def trtc_enabled(self) -> bool:
        return bool(self.trtc_sdk_app_id and self.trtc_sdk_secret_key)


#: Minimum length of the gateway <-> validator shared secret. Shorter values count as unset.
MIN_SERVICE_TOKEN_LEN = 32


class ValidatorServiceTokenSettings(_BaseSettings):
    """Shared secret the gateway presents to the validator (``Authorization: Bearer``).

    Only the gateway and the validator receive it; the agent never does. A missing or
    short (< 32 chars) value means "unset": the validator then refuses every request.
    """

    validator_service_token: SecretStr | None = Field(default=None, alias="VALIDATOR_SERVICE_TOKEN")

    @property
    def service_token_configured(self) -> bool:
        token = self.validator_service_token
        return token is not None and len(token.get_secret_value()) >= MIN_SERVICE_TOKEN_LEN


class AgentSettings(LLMSettings, LangfuseSettings):
    """Agent service settings: LLM + Langfuse only. No ledger, no signing, no TRTC."""

    llm_provider: LLMProvider = Field(default="tokenhub", alias="EXTRACT_PROVIDER")
    extract_model: str = Field(default="hy3", alias="EXTRACT_MODEL")
    # hy3 is a reasoning model: reasoning tokens count against max_tokens, and at 200 the
    # answer came back empty. Keep this generous.
    # hy3 reasons before answering; a 2-leg request already used ~1,500 tokens.
    extract_max_tokens: int = Field(default=4096, alias="EXTRACT_MAX_TOKENS")
    gateway_base_url: str = Field(default="http://localhost:8002", alias="GATEWAY_BASE_URL")


class ValidatorSettings(LLMSettings, LangfuseSettings, ValidatorServiceTokenSettings):
    """Validator service settings: LLM + Langfuse + the inbound service token.

    No ledger, no payment tools, no signing keys.
    """

    # DeepSeek direct until deepseek-v4-flash is activated on TokenHub (it returns 400/402 there).
    llm_provider: LLMProvider = Field(default="deepseek", alias="VALIDATE_PROVIDER")
    validate_model: str = Field(default="deepseek-flash", alias="VALIDATE_MODEL")
    # Measured: 2 of 56 eval calls spent all 2,048 tokens on reasoning (no answer).
    validate_max_tokens: int = Field(default=4096, alias="VALIDATE_MAX_TOKENS")
    # Wall-clock limit for one model call; on expiry the result is ERROR, never MATCH.
    validate_timeout_seconds: float = Field(
        default=30.0, gt=0, le=120, alias="VALIDATE_TIMEOUT_SECONDS"
    )


class GatewaySettings(LangfuseSettings, TRTCSettings, ValidatorServiceTokenSettings):
    """Gateway service settings: TRTC + Langfuse + DB + policy defaults + validator access."""

    db_path: str = Field(default="data/dcta.db", alias="DCTA_DB_PATH")
    rp_id: str = Field(default="localhost", alias="WEBAUTHN_RP_ID")
    rp_origin: str = Field(default="http://localhost:5173", alias="WEBAUTHN_RP_ORIGIN")

    # Delegation token secret for service-to-gateway identity (interim, until T-05).
    gateway_delegation_secret: str | None = Field(default=None, alias="GATEWAY_DELEGATION_SECRET")

    # Internal semantic validator (T-10). Compose must point this at http://validator:8003.
    validator_base_url: str = Field(default="http://localhost:8003", alias="VALIDATOR_BASE_URL")
    agent_base_url: str = Field(default="http://localhost:8001", alias="AGENT_BASE_URL")
    # hy3 reasons before answering: 4–12 s per extraction measured, longer on long
    # conversations. 15 s timed out a live follow-up turn.
    agent_timeout_seconds: float = Field(default=45.0, gt=0, le=120, alias="AGENT_TIMEOUT_SECONDS")
    # Must exceed the validator's own model timeout (VALIDATE_TIMEOUT_SECONDS, default 30).
    validator_client_timeout_seconds: float = Field(
        default=40.0, gt=0, le=180, alias="VALIDATOR_CLIENT_TIMEOUT_SECONDS"
    )

    # Policy defaults (prototype values, not regulatory statements)
    nonce_ttl_seconds: int = Field(default=120, alias="NONCE_TTL_SECONDS")
    mandate_per_leg: str = Field(default="1000.00", alias="MANDATE_PER_LEG")
    mandate_daily: str = Field(default="3000.00", alias="MANDATE_DAILY")
    risk_threshold: str = Field(default="500.00", alias="RISK_THRESHOLD")
    cooling_hours: int = Field(default=12, alias="COOLING_HOURS")
    timezone: str = Field(default="Asia/Singapore", alias="DCTA_TIMEZONE")


def load_agent_settings() -> AgentSettings:
    return AgentSettings()


def load_validator_settings() -> ValidatorSettings:
    return ValidatorSettings()


def load_gateway_settings() -> GatewaySettings:
    return GatewaySettings()
