"""API layer: POST /api/profile (risk profile)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import ledger, policy
from app.api import get_session
from app.schemas import ProfileRequest, utcnow

router = APIRouter()


@router.post("/profile")
def set_profile(body: ProfileRequest, session: Session = Depends(get_session)) -> dict[str, str]:
    if body.profile not in policy.PROFILES:
        raise ValueError("unknown profile")
    ledger.set_setting(session, "RISK_PROFILE", body.profile)
    ledger.log_event(session, "profile", f"Risk profile set to {body.profile}.", {"profile": body.profile}, utcnow())
    session.commit()
    return {"profile": body.profile}
