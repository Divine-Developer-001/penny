"""Configuration layer: typed settings loaded from the environment and .env."""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def is_placeholder(value: str) -> bool:
    """True for empty values and the demo/replace-me placeholders from .env.example."""
    v = (value or "").strip().lower()
    return not v or "replace" in v or "demo" in v


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore")

    # PayPal sandbox
    paypal_env: str = "sandbox"
    paypal_api_base: str = "https://api-m.sandbox.paypal.com"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = "WEBHOOK_ID"
    paypal_payout_receiver_email: str = "personal-facilitator@example.com"
    paypal_negative_test: bool = False

    # Broker
    broker_mode: str = "alpaca"
    alpaca_api_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # LLM
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "llama-3.3-70b-versatile"

    # Channel3 product search (optional: sample deals are used when unset)
    channel3_api_key: str = ""

    # Penny rules
    risk_profile: str = "growth"
    cashback_rate: Decimal = Decimal("0.02")
    fee_assumed_usd: Decimal = Decimal("0.50")
    max_fee_drag_pct: Decimal = Decimal("0.05")
    min_trade_usd: Decimal = Decimal("5.00")
    trade_cap_usd: Decimal = Decimal("25.00")
    hold_max_days: int = 30
    undo_window_seconds: int = 60
    agent_interval_minutes: int = 15
    quote_staleness_seconds: int = 300
    auto_agent_on_purchase: bool = True   # run the agent right after a purchase if the pot is ready to invest

    seed_on_start: bool = False                # .env.example turns this on for the demo

    # App
    database_url: str = "sqlite:///./penny.db"
    frontend_origin: str = "http://localhost:5173"
    port: int = 8000
    log_level: str = "INFO"

    @property
    def alpaca_configured(self) -> bool:
        return not is_placeholder(self.alpaca_api_key_id) and not is_placeholder(self.alpaca_secret_key)

    @property
    def llm_configured(self) -> bool:
        return not is_placeholder(self.llm_api_key)

    @property
    def paypal_configured(self) -> bool:
        return not is_placeholder(self.paypal_client_id) and not is_placeholder(self.paypal_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
