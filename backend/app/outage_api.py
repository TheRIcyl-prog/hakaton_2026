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


def outage_action_router(get_itsm: Callable[[], Itsm]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/outages/{outage_id}/actions")
    def outage_action(outage_id: str, body: OutageActionBody):
        try:
            return get_itsm().act_on_outage(outage_id, body.operation, body.by, body.expected_hashes, body.team, body.comment)
        except ItsmError as e:
            raise HTTPException(e.status, str(e)) from e

    return router
