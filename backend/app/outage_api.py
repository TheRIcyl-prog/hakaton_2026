from typing import Callable, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .itsm import Itsm, ItsmError


class OutageActionBody(BaseModel):
    by: str = Field(max_length=100)
    operation: Literal["claim", "assign", "escalate"]
    expected_hashes: dict[str, str]
    team: str | None = None
    comment: str | None = Field(default=None, max_length=1000)


class RemoveOutageIncidentBody(BaseModel):
    by: str = Field(max_length=100)
    expected_hash: str = Field(min_length=1)
    expected_incident_ids: list[str] = Field(min_length=1)


class RemoveOutageCandidateIncidentBody(BaseModel):
    by: str = Field(max_length=100)


def outage_action_router(get_itsm: Callable[[], Itsm]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/outages/{outage_id}/actions")
    def outage_action(outage_id: str, body: OutageActionBody):
        try:
            return get_itsm().act_on_outage(outage_id, body.operation, body.by, body.expected_hashes, body.team, body.comment)
        except ItsmError as e:
            raise HTTPException(e.status, str(e)) from e

    @router.delete("/api/outages/{outage_id}/incidents/{inc_id}")
    def remove_outage_incident(outage_id: str, inc_id: str, body: RemoveOutageIncidentBody):
        try:
            return {"outage": get_itsm().remove_outage_incident(
                outage_id, inc_id, body.by, body.expected_hash, body.expected_incident_ids)}
        except ItsmError as e:
            raise HTTPException(e.status, str(e)) from e

    @router.get("/api/outage-candidates/{candidate_id}")
    def outage_candidate_detail(candidate_id: str):
        candidate = get_itsm().outage_candidate_detail(candidate_id)
        if candidate is None:
            raise HTTPException(404, "предложение о сбое не найдено или устарело")
        return candidate

    @router.delete("/api/outage-candidates/{candidate_id}/incidents/{inc_id}")
    def remove_outage_candidate_incident(candidate_id: str, inc_id: str, body: RemoveOutageCandidateIncidentBody):
        try:
            return {"candidate": get_itsm().remove_outage_candidate_incident(candidate_id, inc_id, body.by)}
        except ItsmError as e:
            raise HTTPException(e.status, str(e)) from e

    return router
