"""Persistence layer: SQLAlchemy models. Money columns store exact Decimal strings (SQLite-safe)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.db import Base
from app.schemas import utcnow


class Dec(TypeDecorator):
    """Exact Decimal stored as text, so SQLite never degrades it to float."""
    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class ProposalStatus:
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"          # approved; waiting out the undo window
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    UNDONE = "UNDONE"
    FAILED = "FAILED"


class PayoutStatus:
    CREATED = "CREATED"
    IN_TRANSIT = "IN_TRANSIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CashbackCredit(Base):
    __tablename__ = "cashback_credits"
    id: Mapped[int] = mapped_column(primary_key=True)
    paypal_event_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    capture_amount: Mapped[Decimal] = mapped_column(Dec)
    cashback_amount: Mapped[Decimal] = mapped_column(Dec)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PendingCash(Base):
    """The pot. status is 'available' or 'in_transit' (money on its way out via a payout)."""
    __tablename__ = "pending_cash"
    id: Mapped[int] = mapped_column(primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Dec)
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    source: Mapped[str] = mapped_column(String(80), default="cashback")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Position(Base):
    __tablename__ = "positions"
    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(Dec)
    avg_cost: Mapped[Decimal] = mapped_column(Dec)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TradeProposal(Base):
    __tablename__ = "trade_proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16))
    dollar_amount: Mapped[Decimal] = mapped_column(Dec)
    side: Mapped[str] = mapped_column(String(8), default="buy")
    status: Mapped[str] = mapped_column(String(20), default=ProposalStatus.PROPOSED, index=True)
    policy_reason: Mapped[str] = mapped_column(String(60), default="")
    llm_rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    undo_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LedgerEvent(Base):
    """Append-only log. `description` is the one-line human explanation."""
    __tablename__ = "ledger_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    amount: Mapped[Decimal] = mapped_column(Dec)
    paypal_payout_item_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    paypal_batch_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    sender_item_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    pending_cash_id: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=PayoutStatus.CREATED)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class ProcessedWebhook(Base):
    """Idempotency table: PayPal retries deliveries, so every event key is processed once."""
    __tablename__ = "processed_webhooks"
    event_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(80), default="")
    source: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PortfolioSnapshot(Base):
    """Account value over time (drives the performance chart).

    total_value = cash + sum(positions x quote). invested_cost = money put in (cashback credited minus
    money withdrawn), which equals cash + cost basis when nothing has been sold, so value minus
    invested_cost is the profit/loss.
    """
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    total_value: Mapped[Decimal] = mapped_column(Dec)
    invested_cost: Mapped[Decimal] = mapped_column(Dec)
    cash: Mapped[Decimal] = mapped_column(Dec)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True, default=utcnow)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str] = mapped_column(String(255), default="")
    price: Mapped[Decimal] = mapped_column(Dec)
    category: Mapped[str] = mapped_column(String(60), default="")


class Purchase(Base):
    """A shop checkout. Prices are recomputed server-side; the client never sets the amount."""
    __tablename__ = "purchases"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[str] = mapped_column(String(40))
    product_name: Mapped[str] = mapped_column(String(120))
    merchant: Mapped[str] = mapped_column(String(60))
    price: Mapped[Decimal] = mapped_column(Dec)
    coupon_code: Mapped[str] = mapped_column(String(20), default="")
    cashback_rate: Mapped[Decimal] = mapped_column(Dec)
    cashback_amount: Mapped[Decimal] = mapped_column(Dec, default=Decimal("0"))
    order_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    capture_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    mode: Mapped[str] = mapped_column(String(12), default="simulated")      # paypal | simulated
    status: Mapped[str] = mapped_column(String(12), default="CREATED")      # CREATED | CAPTURED | FAILED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
