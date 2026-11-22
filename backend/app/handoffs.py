"""Снимки передачи смены для участников, независимо от дальнейших правок инцидентов."""
from __future__ import annotations

import json
import threading
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .adapter import iso, parse_dt
from .auth import CurrentProfile, Profile
from .itsm import ItsmError, _atomic_write
from .queue import escalation_is_current

if TYPE_CHECKING:
    from .store import Store


class Handoffs:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("expected a list")
            return data
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise HTTPException(503, "Не удалось прочитать журнал передачи смены") from exc

    def _write(self, items: list[dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.path, json.dumps(items, ensure_ascii=False, indent=2))
        except (OSError, ItsmError) as exc:
            raise HTTPException(503, "Не удалось сохранить передачу смены") from exc

    def list_for(self, profile: Profile) -> list[dict]:
        with self.lock:
            return [item for item in reversed(self._load()) if profile.id in (item["from_id"], item["to_id"])]

    def send(self, sender: Profile, receiver: Profile, note: str, summary: dict) -> dict:
        with self.lock:
            items = self._load()
            for previous in reversed(items):
                if (previous["from_id"] == sender.id and previous["to_id"] == receiver.id
                        and not previous["accepted_at"] and previous["note"] == note
                        and previous["incidents"] == summary["incidents"]):
                    return previous
            entry = {**summary, "id": uuid4().hex, "from_id": sender.id, "from_name": sender.display_name,
                     "to_id": receiver.id, "to_name": receiver.display_name, "note": note, "accepted_at": None}
            self._write([*items, entry])
            return entry

    def acknowledge(self, handoff_id: str, receiver: Profile, now: str) -> dict:
        with self.lock:
            items = self._load()
            for item in items:
                if item["id"] == handoff_id and item["to_id"] == receiver.id:
                    if item["accepted_at"] is None:
                        item["accepted_at"] = now
                        self._write(items)
                    return item
            raise HTTPException(404, "Входящая передача смены не найдена")


def shift_summary(store: Store, profile: Profile) -> dict:
    with store.lock:
        now = store.clock.now()
        near_deadline = now + timedelta(minutes=15)
        rows = []
        for inc in store.incidents.values():
            owner = (inc.get("coordinator") or {}).get("name")
            mine = (owner or "").casefold() == profile.display_name.casefold()
            if inc["state_kind"] != "active" and not (inc["state_kind"] == "research" and mine):
                continue
            escalation = inc.get("_escalation") or {}
            signals = []
            if inc["priority_level"] == "critical":
                signals.append("Критический")
            deadline = parse_dt(inc["deadline"])
            if ((deadline and deadline <= near_deadline)
                    or (inc.get("forecast") or {}).get("level") in ("risk", "breach", "overdue")):
                signals.append("Риск SLA")
            if ((inc.get("ai") or {}).get("risk") or {}).get("level") == "high":
                signals.append("Риск стать критическим")
            if escalation.get("id") and not escalation.get("accepted_at") and escalation_is_current(inc, store.history):
                signals.append("Эскалация без ответа" if escalation["due"] and escalation["due"] <= now
                               else "Эскалация ждёт ответа")
            if mine:
                signals.append("В моей работе")
            if signals:
                log = inc["work_log"][-1] if inc["work_log"] else None
                rows.append({"id": inc["id"], "title": inc["title"], "product": inc["product"], "team": inc["team"],
                             "coordinator": owner, "deadline": inc["deadline"], "signals": signals,
                             "outage": (inc.get("outage") or {}).get("name"), "last_action": log})
        rows.sort(key=lambda row: ("Критический" not in row["signals"], row["deadline"] or "~", row["id"]))
        return {"at": iso(now), "incidents": rows}


class SendHandoffBody(BaseModel):
    recipient_id: str
    note: str = Field(min_length=1, max_length=1000)


def handoff_router(get_store) -> APIRouter:
    router = APIRouter(prefix="/api/handoffs")

    @router.get("")
    def handoffs(request: Request, profile: CurrentProfile):
        store = get_store()
        accounts = request.app.state.auth.accounts.values()
        return {"me": profile.id, "recipients": [{"id": account.profile.id, "name": account.profile.display_name}
                                                 for account in accounts if account.profile.id != profile.id],
                "preview": shift_summary(store, profile), "items": store.handoffs.list_for(profile)}

    @router.post("")
    def send_handoff(body: SendHandoffBody, request: Request, profile: CurrentProfile):
        receiver = next((account.profile for account in request.app.state.auth.accounts.values()
                         if account.profile.id == body.recipient_id and body.recipient_id != profile.id), None)
        note = body.note.strip()
        if receiver is None or not note:
            raise HTTPException(400, "Выберите другого координатора и укажите следующий шаг")
        store = get_store()
        return store.handoffs.send(profile, receiver, note, shift_summary(store, profile))

    @router.post("/{handoff_id}/acknowledge")
    def acknowledge_handoff(handoff_id: str, profile: CurrentProfile):
        store = get_store()
        return store.handoffs.acknowledge(handoff_id, profile, iso(store.clock.now()))

    return router
