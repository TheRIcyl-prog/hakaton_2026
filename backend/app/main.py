"""FastAPI: один процесс, один порт. Запуск: python -m uvicorn backend.app.main:app --port 8000"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai as ai_layer
from .config import ROOT, Settings, load_rules
from .desk import DeskState
from .itsm import Itsm, ItsmError
from .store import Store

log = logging.getLogger("dashboard")
settings = Settings()
ai_layer.setup()
rules = load_rules(settings.rules_file)
store = Store(settings, rules, desk=DeskState(ROOT / "cache" / "desk_state.json"))
itsm = Itsm(settings.incidents_file, rules, store.clock.now)
BOOT_ID = uuid.uuid4().hex[:8]  # ETag меняется после перезапуска, даже если version совпал


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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "file": store.file,
        "version": store.version,
        "time_mode": store.clock.mode,
        "incidents": len(store.incidents),
        "ai": ai_layer.status(),
    }


@app.get("/api/snapshot")
def snapshot(request: Request):
    etag = f'"{BOOT_ID}-{store.version}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse({**store.snapshot(), "boot_id": BOOT_ID}, headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/api/events")
def events(after: int = 0):
    return {"events": store.events_after(after), "last_event_seq": store.seq}


@app.get("/api/incidents/{inc_id}")
def incident(inc_id: str):
    inc = store.incident(inc_id)
    if inc is None:
        raise HTTPException(404, "нет такого инцидента")
    return inc


# ---------- смена: «Принял», «Взять в работу» (общие для стены и всех АРМ) ----------
class By(BaseModel):
    by: str


class AckBody(By):
    key: str


@app.post("/api/alerts/ack")
def ack(body: AckBody):
    if not body.by.strip():
        raise HTTPException(400, "укажите имя координатора")
    if not store.ack(body.key, body.by.strip()):
        raise HTTPException(404, "тревога уже неактуальна")
    return {"ok": True}


@app.post("/api/incidents/{inc_id}/claim")
def claim(inc_id: str, body: By):
    if inc_id not in store.incidents:
        raise HTTPException(404, "нет такого инцидента")
    if not body.by.strip():
        raise HTTPException(400, "укажите имя координатора")
    store.claim(inc_id, body.by.strip())
    return {"ok": True}


@app.delete("/api/incidents/{inc_id}/claim")
def release(inc_id: str):
    store.release(inc_id)
    return {"ok": True}


# ---------- мини-ITSM: действия пишутся в файл инцидентов ----------
class AssignBody(By):
    team: str
    assignee: str | None = None
    comment: str | None = None


class StateBody(By):
    state_id: int
    reason: str | None = None
    comment: str | None = None


class PriorityBody(By):
    priority_id: int
    recalc_deadline: bool = False
    comment: str | None = None


class DeadlineBody(By):
    deadline: str
    comment: str | None = None


class CommentBody(By):
    text: str


class EscalateBody(By):
    comment: str | None = None


def _run(inc_id: str, fn):
    try:
        fn()
    except ItsmError as e:
        raise HTTPException(e.status, str(e))
    store.poll()  # сразу перечитать файл — экран обновится без ожидания опроса
    return store.incident(inc_id)


@app.post("/api/itsm/{inc_id}/assign")
def itsm_assign(inc_id: str, b: AssignBody):
    return _run(inc_id, lambda: itsm.assign(inc_id, b.team, b.by, b.assignee, b.comment))


@app.post("/api/itsm/{inc_id}/state")
def itsm_state(inc_id: str, b: StateBody):
    return _run(inc_id, lambda: itsm.set_state(inc_id, b.state_id, b.by, b.reason, b.comment))


@app.post("/api/itsm/{inc_id}/priority")
def itsm_priority(inc_id: str, b: PriorityBody):
    return _run(inc_id, lambda: itsm.set_priority(inc_id, b.priority_id, b.by, b.recalc_deadline, b.comment))


@app.post("/api/itsm/{inc_id}/deadline")
def itsm_deadline(inc_id: str, b: DeadlineBody):
    return _run(inc_id, lambda: itsm.set_deadline(inc_id, b.deadline, b.by, b.comment))


@app.post("/api/itsm/{inc_id}/comment")
def itsm_comment(inc_id: str, b: CommentBody):
    return _run(inc_id, lambda: itsm.comment(inc_id, b.text, b.by))


@app.post("/api/itsm/{inc_id}/escalate")
def itsm_escalate(inc_id: str, b: EscalateBody):
    return _run(inc_id, lambda: itsm.escalate(inc_id, b.by, b.comment))


@app.get("/api/meta")
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
@app.get("/api/export")
def export_file():
    return FileResponse(settings.incidents_file, media_type="application/json",
                        filename=f"incidents-{store.clock.now():%Y%m%d-%H%M}.json")


@app.post("/api/import")
async def import_file(request: Request):
    text = (await request.body()).decode("utf-8-sig", errors="replace")
    try:
        n = itsm.import_file(text, ROOT / "cache" / "backups")
    except ItsmError as e:
        raise HTTPException(e.status, str(e))
    store.poll()
    return {"ok": True, "count": n}


# ---------- страницы ----------
@app.get("/itsm/{inc_id}")
def itsm_page(inc_id: str):
    """Мини-ITSM (на хакатоне настоящей нет). В проде ITSM_URL_TEMPLATE ведёт в настоящую систему."""
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
