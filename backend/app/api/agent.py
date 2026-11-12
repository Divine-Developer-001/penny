"""API layer: manual agent trigger (the scheduler runs the same function on an interval)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.agent.loop import run_agent
from app.api import Services, get_services, get_session
from app.schemas import AgentRunOut

router = APIRouter()


@router.post("/agent/run", response_model=AgentRunOut)
def agent_run(services: Services = Depends(get_services), session: Session = Depends(get_session)) -> AgentRunOut:
    return AgentRunOut(**run_agent(session, services.settings, services.broker, services.llm))
