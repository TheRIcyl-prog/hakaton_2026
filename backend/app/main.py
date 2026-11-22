"""FastAPI: один процесс, один порт. Запуск: python -m uvicorn backend.app.main:app --port 8000"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai as ai_layer
from .auth import NO_STORE, CurrentProfile, DemoAuth, require_profile, router as auth_router
from .config import Settings, load_rules
from .desk import DeskState
from .feedback_api import feedback_router
from .handoffs import handoff_router
from .history import History
from .itsm import Itsm, ItsmError
from .outage_api import outage_action_router
from .queue_preferences import Operation, QueuePreferences
from .store import Store

log = logging.getLogger("dashboard")
settings = Settings()
ai_layer.setup(cache_dir=settings.cache_dir)
rules = load_rules(settings.rules_file)
store = Store(settings, rules, desk=DeskState(settings.cache_dir / "desk_state.json"),
              history=History(settings.cache_dir / "history.jsonl"),
              queue_preferences=QueuePreferences(settings.cache_dir / "queue_preferences.json"))
itsm = Itsm(settings.incidents_file, rules, store.clock.now, store=store,
            backup_dir=settings.backups_dir, backups_keep=settings.backups_keep)
BOOT_ID = uuid.uuid4().hex[:8]  # ETag меняется после перезапуска, даже если version совпал
store.snapshot_extra["boot_id"] = BOOT_ID


async def _poll_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(store.poll)
        except Exception:  # опрос не должен умирать никогда
            log.exception("poll failed")
        await asyncio.sleep(settings.poll_interval)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.poll()
    task = asyncio.create_task(_poll_loop())
    yield
    task.cancel()


app = FastAPI(title="МТС · дашборд координатора", lifespan=lifespan)
app.state.auth = DemoAuth(settings)
app.include_router(auth_router)
app.include_router(outage_action_router(lambda: itsm))
app.include_router(handoff_router(lambda: store))
app.include_router(feedback_router(lambda inc_id: store.incident(inc_id), lambda: store._teams(), rules.coordinators_team))


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "file": store.file,
        "version": store.version,
        "time_mode": store.clock.mode,
        "incidents": len(store.incidents),
        "last_apply_seconds": round(store.last_apply_seconds, 3),
        "ai": ai_layer.status(),
    }


@app.get("/api/snapshot")
def snapshot(request: Request):
    etag = f'"{BOOT_ID}-{store.version}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    # Снимок сериализуется и сжимается один раз на версию — для стены и всех АРМ
    gz = "gzip" in (request.headers.get("accept-encoding") or "")
    body, version = store.snapshot_bytes(gz)
    headers = {"ETag": f'"{BOOT_ID}-{version}"', "Cache-Control": "no-cache", "X-Boot-Id": BOOT_ID, "Vary": "Accept-Encoding"}
    headers["X-Dashboard-Now"] = store.clock.now().isoformat()
    if gz:
        headers["Content-Encoding"] = "gzip"
    return Response(body, media_type="application/json", headers=headers)


@app.get("/api/workstation/snapshot", dependencies=[Depends(require_profile)])
def workstation_snapshot(request: Request):
    response = snapshot(request)
    response.headers.update(NO_STORE)
    return response


@app.get("/api/search", dependencies=[Depends(require_profile)])
def search(q: str = "", status: str = "all", product: str | None = None, team: str | None = None,
           priority: str | None = None, coordinator: str | None = None, group: str | None = None,
           overdue: bool = False, burning: bool = False, untaken: bool = False, unassigned: bool = False,
           risk_high: bool = False, duplicates: bool = False, service_type: str | None = None,
           new_hour: bool = False, limit: int = 100, offset: int = 0):
    """Поиск и фильтры по ВСЕМ инцидентам (в снимке — только видимые). Списки — через запятую."""
    return store.search(q, limit=limit, offset=max(0, offset), status=status, product=product, team=team,
                        priority=priority, coordinator=coordinator, group=group, overdue=overdue, burning=burning,
                        untaken=untaken, unassigned=unassigned, risk_high=risk_high, duplicates=duplicates,
                        service_type=service_type, new_hour=new_hour)


@app.get("/api/events", dependencies=[Depends(require_profile)])
def events(after: int = 0):
    return {"events": store.events_after(after), "last_event_seq": store.seq}


class QueuePreferenceBody(BaseModel):
    id: str
    operation: Operation


@app.get("/api/queue")
def queue(profile: CurrentProfile):
    return {"items": store.queue(profile.display_name)}


@app.post("/api/queue/preferences")
def queue_preference(body: QueuePreferenceBody, profile: CurrentProfile):
    if not body.id.strip():
        raise HTTPException(400, "укажите задачу")
    try:
        store.queue_preferences.set(profile.display_name, body.id.strip(), body.operation)
    except OSError:
        log.exception("queue preference write failed")
        raise HTTPException(503, "не удалось сохранить предпочтение очереди")
    return {"ok": True}


@app.get("/api/incidents/{inc_id}", dependencies=[Depends(require_profile)])
def incident(inc_id: str):
    inc = store.incident(inc_id)
    if inc is None:
        raise HTTPException(404, "нет такого инцидента")
    return inc


# ---------- «Взять в работу» / «Отпустить» — пишутся в файл (поле coordinator), снимают тревогу у всех ----------
class VersionedBody(BaseModel):
    expected_hash: str | None = None   # hash инцидента, который видел координатор; изменился → 409 с тем, что поменялось


class TakeBody(VersionedBody):
    force: bool = False                # Requires expected_hash when true.


@app.post("/api/incidents/{inc_id}/claim")
def claim(inc_id: str, body: TakeBody, profile: CurrentProfile):
    return _run(inc_id, lambda: itsm.take(inc_id, profile.display_name, body.expected_hash, body.force))


@app.delete("/api/incidents/{inc_id}/claim")
def release(inc_id: str, profile: CurrentProfile, expected_hash: str | None = None):
    return _run(inc_id, lambda: itsm.release(inc_id, profile.display_name, expected_hash))


# ---------- мини-ITSM: действия пишутся в файл инцидентов ----------
class AssignBody(VersionedBody):
    team: str
    assignee: str | None = None
    comment: str | None = None


class StateBody(VersionedBody):
    state_id: int
    reason: str | None = None
    comment: str | None = None


class PriorityBody(VersionedBody):
    priority_id: int
    recalc_deadline: bool = False
    comment: str | None = None


class DeadlineBody(VersionedBody):
    deadline: str
    comment: str | None = None


class CommentBody(VersionedBody):
    text: str


class EscalateBody(VersionedBody):
    comment: str | None = None


class AcknowledgeEscalationBody(BaseModel):
    request_id: str
    expected_hash: str


def _run(inc_id: str, fn):
    try:
        fn()  # запись применяется в Store сразу, без перечитывания файла
    except ItsmError as e:
        if e.detail:
            return JSONResponse({"detail": str(e), **e.detail}, status_code=e.status)
        raise HTTPException(e.status, str(e))
    return store.incident(inc_id)


@app.post("/api/itsm/{inc_id}/assign")
def itsm_assign(inc_id: str, b: AssignBody, profile: CurrentProfile):
    return _run(inc_id, lambda: itsm.assign(inc_id, b.team, profile.display_name, b.assignee, b.comment, b.expected_hash))


@app.post("/api/itsm/{inc_id}/state")
def itsm_state(inc_id: str, b: StateBody, profile: CurrentProfile):
    return _run(inc_id, lambda: itsm.set_state(inc_id, b.state_id, profile.display_name, b.reason, b.comment, b.expected_hash))


@app.post("/api/itsm/{inc_id}/priority")
def itsm_priority(inc_id: str, b: PriorityBody, profile: CurrentProfile):
    return _run(inc_id, lambda: itsm.set_priority(inc_id, b.priority_id, profile.display_name, b.recalc_deadline, b.comment, b.expected_hash))


@app.post("/api/itsm/{inc_id}/deadline")
def itsm_deadline(inc_id: str, b: DeadlineBody, profile: CurrentProfile):
    return _run(inc_id, lambda: itsm.set_deadline(inc_id, b.deadline, profile.display_name, b.comment, b.expected_hash))


@app.post("/api/itsm/{inc_id}/comment")
def itsm_comment(inc_id: str, b: CommentBody, profile: CurrentProfile):
    return _run(inc_id, lambda: itsm.comment(inc_id, b.text, profile.display_name, b.expected_hash))


@app.post("/api/itsm/{inc_id}/escalate")
def itsm_escalate(inc_id: str, b: EscalateBody, profile: CurrentProfile):
    return _run(inc_id, lambda: itsm.escalate(inc_id, profile.display_name, b.comment, b.expected_hash))


@app.post("/api/itsm/{inc_id}/escalation/acknowledge")
def acknowledge_escalation(inc_id: str, b: AcknowledgeEscalationBody, profile: CurrentProfile):
    return _run(inc_id, lambda: itsm.acknowledge_escalation(inc_id, b.request_id, profile.display_name, b.expected_hash))


class DuplicatesBody(BaseModel):
    master_id: str
    ids: list[str]


@app.post("/api/duplicates/confirm")
def confirm_duplicates(b: DuplicatesBody, profile: CurrentProfile):
    """«Да, это один сбой»: isMaster у мастера, isDouble у остальных + журнал (п. 8.4)."""
    try:
        itsm.confirm_duplicates(b.master_id, b.ids, profile.display_name)
    except ItsmError as e:
        raise HTTPException(e.status, str(e))
    return {"ok": True, "incidents": [store.incident(i) for i in dict.fromkeys([b.master_id, *b.ids])]}


class ConfirmOutageBody(BaseModel):
    candidate_id: str


@app.post("/api/outages/confirm")
def confirm_outage(body: ConfirmOutageBody, profile: CurrentProfile):
    try:
        return itsm.confirm_outage(body.candidate_id, profile.display_name)
    except ItsmError as e:
        raise HTTPException(e.status, str(e))


@app.get("/api/outages/{outage_id}", dependencies=[Depends(require_profile)])
def outage(outage_id: str):
    result = store.outage(outage_id, include_incidents=True)
    if result is None:
        raise HTTPException(404, "нет такого сбоя")
    return result


@app.get("/api/recently-closed", dependencies=[Depends(require_profile)])
def recently_closed(kind: Literal["incidents", "outages"] = "incidents", offset: int = Query(0, ge=0)):
    return store.recently_closed(kind, offset)


@app.get("/api/meta", dependencies=[Depends(require_profile)])
def meta():
    """Справочники для форм: статусы, переходы, приоритеты, SLA, команды."""
    return {
        "states": {k: v["name"] for k, v in rules.states.items()},
        "transitions": rules.transitions,
        "priorities": {k: v["name"] for k, v in rules.priorities.items()},
        "sla_hours": rules.sla_hours,
        "teams": store._teams(),
        "coordinators_team": rules.coordinators_team,
    }


# ---------- импорт / экспорт файла инцидентов ----------
@app.get("/api/export", dependencies=[Depends(require_profile)])
def export_file():
    return FileResponse(settings.incidents_file, media_type="application/json",
                        headers=NO_STORE, filename=f"incidents-{store.clock.now():%Y%m%d-%H%M}.json")


@app.post("/api/import", dependencies=[Depends(require_profile)])
async def import_file(request: Request):
    text = (await request.body()).decode("utf-8-sig", errors="replace")
    try:
        n = itsm.import_file(text, settings.backups_dir)
    except ItsmError as e:
        raise HTTPException(e.status, str(e))
    store.poll(force=True)
    return {"ok": True, "count": n}


# ---------- страницы ----------
@app.get("/itsm/{inc_id}")
def itsm_page(inc_id: str, request: Request):
    """Мини-ITSM (на хакатоне настоящей нет). В проде ITSM_URL_TEMPLATE ведёт в настоящую систему."""
    if request.app.state.auth.profile(request) is None:
        target = f"/itsm/{quote(inc_id, safe='')}" if inc_id not in {".", ".."} else "/"
        return RedirectResponse("/?" + urlencode({"return": target}), headers=NO_STORE)
    return FileResponse(settings.frontend_dir / "itsm.html", headers={"Cache-Control": "no-cache"})


@app.get("/wall")
def wall_page():
    """Видеостена: общий экран над рабочими местами координаторов."""
    return FileResponse(settings.frontend_dir / "wall.html", headers={"Cache-Control": "no-cache"})


if (settings.frontend_dir / "index.html").exists():
    # Фронт без сборки: статические ES-модули. no-cache — чтобы правки сразу были видны
    class NoCacheStatic(StaticFiles):
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    app.mount("/", NoCacheStatic(directory=settings.frontend_dir, html=True), name="frontend")
