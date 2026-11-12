"""API layer: human approval queue and the undo window for proposals."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import explain, ledger
from app.api import Services, get_services, get_session
from app.models import ProposalStatus, TradeProposal
from app.schemas import ProposalActionOut, ProposalOut, utcnow

router = APIRouter()


def _get(session: Session, proposal_id: int) -> TradeProposal:
    prop = session.get(TradeProposal, proposal_id)
    if prop is None:
        raise HTTPException(404, "proposal not found")
    return prop


def _out(prop: TradeProposal, message: str) -> ProposalActionOut:
    return ProposalActionOut(proposal=ProposalOut.model_validate(prop), message=message)


@router.post("/proposals/{proposal_id}/approve", response_model=ProposalActionOut)
def approve(proposal_id: int, services: Services = Depends(get_services), session: Session = Depends(get_session)):
    prop = _get(session, proposal_id)
    if prop.status != ProposalStatus.PENDING_APPROVAL:
        raise HTTPException(409, f"proposal is {prop.status}, not PENDING_APPROVAL")
    now = utcnow()
    window = services.settings.undo_window_seconds
    prop.status, prop.policy_reason = ProposalStatus.APPROVED, "USER_APPROVED"
    prop.undo_deadline = now + timedelta(seconds=window)
    ledger.log_event(session, "proposal", explain.approved_waiting(prop.ticker, prop.dollar_amount, window),
                     {"proposal_id": prop.id}, now)
    if window <= 0:
        ledger.execute_proposal(session, prop, services.broker, ledger.get_profile(session, services.settings), now)
    session.commit()
    return _out(prop, "approved; executes after the undo window" if window > 0 else "approved and executed")


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalActionOut)
def reject(proposal_id: int, session: Session = Depends(get_session)):
    prop = _get(session, proposal_id)
    if prop.status not in (ProposalStatus.PENDING_APPROVAL, ProposalStatus.APPROVED):
        raise HTTPException(409, f"proposal is {prop.status}; it can no longer be rejected")
    prop.status, prop.policy_reason = ProposalStatus.REJECTED, "USER_REJECTED"
    ledger.log_event(session, "proposal", f"You rejected the ${prop.dollar_amount} {prop.ticker} order.", {"proposal_id": prop.id})
    session.commit()
    return _out(prop, "rejected")


@router.post("/proposals/{proposal_id}/undo", response_model=ProposalActionOut)
def undo(proposal_id: int, session: Session = Depends(get_session)):
    prop = _get(session, proposal_id)
    now = utcnow()
    if prop.status != ProposalStatus.APPROVED or prop.undo_deadline is None or now > prop.undo_deadline:
        raise HTTPException(409, "the undo window has passed or the proposal is not undoable")
    prop.status, prop.policy_reason = ProposalStatus.UNDONE, "USER_UNDO"
    ledger.log_event(session, "proposal", explain.undone(prop.ticker, prop.dollar_amount), {"proposal_id": prop.id}, now)
    session.commit()
    return _out(prop, "undone; nothing was executed and your cash is still in the pot")
