"""Shared types layer: money helpers, policy/ledger value objects and API request/response schemas."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CENT = Decimal("0.01")
UNIT = Decimal("0.000001")

DISCLAIMER = (
    "Penny is a simulation: cashback is simulated, trading is paper trading, payouts use the PayPal "
    "sandbox. Penny is not financial advice and never promises returns."
)


def utcnow() -> datetime:
    """Naive UTC 'now' used everywhere (SQLite stores naive datetimes)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def usd(value: Any) -> Decimal:
    """Quantize any numeric to whole cents. Never goes through float."""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def units(value: Any) -> Decimal:
    """Quantize a share quantity / price to 6 decimals."""
    return Decimal(str(value)).quantize(UNIT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- value objects
@dataclass(frozen=True)
class Quote:
    ticker: str
    price: Decimal
    as_of: datetime


@dataclass(frozen=True)
class Rules:
    """The numeric limits the pure functions need (defaults mirror .env.example)."""
    trade_cap: Decimal = Decimal("25.00")
    quote_staleness_seconds: int = 300
    min_trade: Decimal = Decimal("5.00")
    fee_assumed: Decimal = Decimal("0.50")
    max_fee_drag_pct: Decimal = Decimal("0.05")
    hold_max_days: int = 30
    undo_window_seconds: int = 60

    @classmethod
    def from_settings(cls, s: Any) -> "Rules":
        return cls(
            trade_cap=s.trade_cap_usd,
            quote_staleness_seconds=s.quote_staleness_seconds,
            min_trade=s.min_trade_usd,
            fee_assumed=s.fee_assumed_usd,
            max_fee_drag_pct=s.max_fee_drag_pct,
            hold_max_days=s.hold_max_days,
            undo_window_seconds=s.undo_window_seconds,
        )


@dataclass(frozen=True)
class LedgerSnapshot:
    """Point-in-time facts the pure functions decide from.

    pot_available is net of money already committed to approved/pending proposals;
    positions_value is pro-forma (it includes committed proposals as if executed).
    """
    now: datetime
    pot_available: Decimal
    positions_value: dict[str, Decimal] = field(default_factory=dict)
    quotes: dict[str, Quote] = field(default_factory=dict)
    oldest_pending_age_days: Decimal = Decimal("0")


@dataclass(frozen=True)
class Proposal:
    ticker: str
    dollar_amount: Decimal
    side: str = "buy"
    linked_withdrawal: bool = False
    rationale: str = field(default="", compare=False)


class VerdictStatus(str, Enum):
    APPROVED = "APPROVED"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Verdict:
    status: VerdictStatus
    code: str
    detail: str = ""
    facts: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- API schemas
class WithdrawRequest(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    include_investments: bool = False      # sell investments (pro rata) if the cash in the pot is not enough


class ProfileRequest(BaseModel):
    profile: Literal["conservative", "balanced", "growth"]


class ProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ticker: str
    dollar_amount: Decimal
    status: str
    policy_reason: str
    llm_rationale: str
    created_at: datetime
    executed_at: datetime | None = None
    undo_deadline: datetime | None = None




class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    description: str
    created_at: datetime


class AgentRunOut(BaseModel):
    mode: str
    iterations: int
    proposal_ids: list[int]
    explanation: str
    skipped: str | None = None
    llm_error: str | None = None      # why the rule-based planner was used instead of the LLM


class ProposalActionOut(BaseModel):
    proposal: ProposalOut
    message: str


class WithdrawOut(BaseModel):
    payout_id: int
    status: str
    amount: Decimal
    message: str


class WebhookAck(BaseModel):
    status: str
    detail: str = ""


# ---------------------------------------------------------------- shop / dashboard schemas



class CreateOrderIn(BaseModel):
    product_id: str
    mode: Literal["paypal", "simulated"] = "paypal"      # simulated = dev fallback with no PayPal login



class CaptureOut(BaseModel):
    status: str
    amount: Decimal
    cashback: Decimal
    cashback_rate: Decimal
    message: str
    duplicate: bool = False




class TotalsOut(BaseModel):
    cashback_total: Decimal
    withdrawn: Decimal
    cash: Decimal
    in_transit: Decimal
    positions_value: Decimal
    total_value: Decimal
    profit_loss: Decimal
    profit_loss_pct: Decimal
    max_withdrawable: Decimal


class HoldingOut(BaseModel):
    ticker: str
    name: str
    quantity: Decimal
    avg_cost: Decimal
    price: Decimal | None
    value: Decimal
    weight: Decimal
    target_weight: Decimal | None
    profit_loss: Decimal
    profit_loss_pct: Decimal


class PurchaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_name: str
    merchant: str
    price: Decimal
    cashback_amount: Decimal
    status: str
    created_at: datetime


class ActivityOut(BaseModel):
    kind: str
    description: str
    created_at: datetime


# ---------------------------------------------------------------- store / state schemas
class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    description: str
    image_url: str
    price: Decimal
    category: str


class DealOfferOut(BaseModel):
    title: str
    merchant: str
    price: Decimal
    delta: Decimal                 # offer price minus the store price (negative = cheaper elsewhere)
    link: str | None = None


class DealsOut(BaseModel):
    product_id: str
    store_price: Decimal
    sample: bool                   # True when these are canned sample offers, not live Channel3 results
    offers: list[DealOfferOut]
    note: str | None = None


class CreateOrderOut(BaseModel):
    order_id: str
    mode: str
    amount: Decimal
    merchant: str
    product_name: str
    cashback_rate: Decimal


class HealthOut(BaseModel):
    status: str
    broker: str
    broker_label: str
    paypal_configured: bool
    llm_configured: bool
    channel3_configured: bool
    seeded: bool


class BrokerOut(BaseModel):
    mode: str
    label: str
    simulated: bool


class PotOut(BaseModel):
    available: Decimal
    in_transit: Decimal
    committed: Decimal
    uncommitted: Decimal


class PayoutOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    amount: Decimal
    status: str
    detail: str
    created_at: datetime


class StateOut(BaseModel):
    """Everything the Penny widget shows, in one poll."""
    broker: BrokerOut
    profile: str
    profile_targets: dict[str, Decimal]
    core_ticker: str
    core_band: list[Decimal]
    pot: PotOut
    totals: TotalsOut
    holdings: list[HoldingOut]
    purchases: list[PurchaseOut]
    activity: list[ActivityOut]
    queued_orders: list[ProposalOut]
    pending_approvals: list[ProposalOut]
    recent_proposals: list[ProposalOut]
    latest_payout: PayoutOut | None = None
    status_message: str
    invest_threshold: Decimal
    disclaimer: str = DISCLAIMER


class HistoryPointOut(BaseModel):
    t: datetime
    value: Decimal
    invested_cost: Decimal
