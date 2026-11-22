from typing import Callable, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import ai
from .adapter import norm_team
from .auth import CurrentProfile


class FeedbackBody(BaseModel):
    kind: Literal["route", "summary", "risk", "dynamics"]
    corrected_team: str | None = Field(default=None, max_length=200)
    expected_team: str | None = Field(default=None, max_length=200)
    expected_hash: str = Field(min_length=1, max_length=100)


def feedback_router(get_incident: Callable, get_teams: Callable, coordinators_team: str) -> APIRouter:
    router = APIRouter()

    @router.post("/api/incidents/{inc_id}/ai-feedback")
    def record(inc_id: str, body: FeedbackBody, profile: CurrentProfile):
        incident = get_incident(inc_id)
        if incident is None:
            raise HTTPException(404, "инцидент не найден")
        if incident["hash"] != body.expected_hash:
            raise HTTPException(409, "инцидент изменился — откройте карточку заново")
        team = body.corrected_team
        if body.kind == "route":
            route = (incident.get("ai") or {}).get("route")
            if not route or route.get("team") != body.expected_team:
                raise HTTPException(409, "подсказка изменилась — откройте карточку заново")
            teams = {norm_team(name): name for name in get_teams()}
            key = norm_team(team)
            if not key or key in ("autoclosedmi", norm_team(coordinators_team)) or key not in teams:
                raise HTTPException(400, "выберите существующую команду-исполнителя")
            team = teams[key]
        try:
            saved = ai.record_feedback(incident, profile.display_name, body.kind, team)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except OSError as e:
            raise HTTPException(503, "память исправлений сейчас недоступна") from e
        return {"ok": True, "saved": saved}

    return router
