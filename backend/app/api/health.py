"""API layer: GET /api/health (what is configured, which broker is live)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import ledger
from app.api import Services, broker_info, get_services, get_session
from app.schemas import HealthOut

router = APIRouter()


@router.get("/health", response_model=HealthOut)
def health(services: Services = Depends(get_services), session: Session = Depends(get_session)) -> HealthOut:
    kind, label, _ = broker_info(services)
    s = services.settings
    return HealthOut(status="ok", broker=kind, broker_label=label, paypal_configured=s.paypal_configured,
                     llm_configured=s.llm_configured, channel3_configured=bool(services.channel3 and services.channel3.configured),
                     seeded=ledger.get_setting(session, "SEED_APPLIED") == "true")
