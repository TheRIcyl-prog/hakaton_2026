"""FastAPI: один процесс, один порт. Запуск: python -m uvicorn backend.app.main:app --port 8000"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_rules
from .store import Store

log = logging.getLogger("dashboard")
settings = Settings()
rules = load_rules(settings.rules_file)
store = Store(settings, rules)


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
    }


@app.get("/api/snapshot")
def snapshot(request: Request):
    etag = f'"{store.version}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(store.snapshot(), headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/api/events")
def events(after: int = 0):
    return {"events": store.events_after(after), "last_event_seq": store.seq}


@app.get("/api/incidents/{inc_id}")
def incident(inc_id: str):
    inc = store.incident(inc_id)
    if inc is None:
        raise HTTPException(404, "нет такого инцидента")
    return inc


if settings.frontend_dist.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
else:

    @app.get("/")
    def root():
        return {"hint": "фронтенд не собран: cd frontend && npm run build; API: /api/snapshot"}
