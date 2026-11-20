"""Чтение файла и текущий снимок.

Опрос раз в POLL_INTERVAL по хэшу содержимого. Битый/пустой/недоступный файл → остаётся
последний целый снимок, а в file.status пишется причина.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from . import adapter
from .actions import build_actions
from . import ai as ai_layer
from .ai import enrich
from .clock import Clock
from .config import Rules, Settings
from .desk import DeskState
from .diff import DONE_KINDS, diff
from .forecast import forecast_all
from .groups import assign_groups

AI_REFRESH_SECONDS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, settings: Settings, rules: Rules, clock: Clock | None = None, desk: DeskState | None = None):
        self.settings = settings
        self.rules = rules
        self.clock = clock or Clock(settings.time_mode, adapter.parse_dt(settings.export_now))
        self.lock = threading.Lock()
        self.desk = desk or DeskState(None)
        self.desk.now_fn = self.clock.now

        self.incidents: dict[str, dict] = {}
        self.raw_by_id: dict[str, dict] = {}
        self.groups: dict[str, list[str]] = {g.key: [] for g in rules.groups}
        self.seen_resolved: dict[str, datetime] = {}
        self.events: deque[dict] = deque(maxlen=300)
        self.actions: list[dict] = []
        self.seq = 0
        self.version = 0
        self.loaded = False

        self._bytes_hash: str | None = None
        self._data: Any = None
        self._last_ai = 0.0
        self.file: dict[str, Any] = {
            "path": str(settings.incidents_file),
            "status": "missing",  # ok | corrupt | missing
            "error": None,
            "last_ok_at": None,  # реальное время последнего удачного чтения
            "last_change_at": None,
            "warnings": [],
            "count": 0,
        }

    # ---------- чтение ----------
    def poll(self) -> bool:
        """Один шаг опроса. True — если снимок изменился (version вырос)."""
        path = self.settings.incidents_file
        try:
            data_bytes = path.read_bytes()
        except FileNotFoundError:
            self._set_file_problem("missing", "файл не найден")
            return self._regroup_if_needed(force=False)
        except OSError:
            return False  # файл занят редактором — попробуем на следующем шаге

        h = hashlib.sha256(data_bytes).hexdigest()
        if h == self._bytes_hash:
            return self._regroup_if_needed(force=False)

        try:
            text = data_bytes.decode("utf-8-sig")
            if not text.strip():
                raise ValueError("файл пустой")
            data = json.loads(text)
            new_incidents, warnings = adapter.adapt_all(data, self.rules, self.settings.itsm_url_template)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
            # Не запоминаем хэш: как только файл дозапишут, прочитаем снова
            self._set_file_problem("corrupt", str(e))
            return False

        self._bytes_hash = h
        self._data = data
        if self.loaded and self._is_replaced(new_incidents):
            # Файл подменили целиком (другая выгрузка/демо-файл): новая точка отсчёта, без лавины событий
            self.clock.reanchor(adapter.export_moment(data))
            self.seen_resolved.clear()
            self.loaded = False
        self.clock.set_anchor_if_empty(adapter.export_moment(data))
        with self.lock:
            self.raw_by_id = {
                str(r.get("id")).strip(): r for r in data if isinstance(r, dict) and r.get("id")
            } if isinstance(data, list) else {}
            was_problem = self.file["status"] != "ok"
            self.file.update(
                status="ok",
                error=None,
                last_ok_at=adapter.iso(_utcnow()),
                last_change_at=adapter.iso(_utcnow()),
                warnings=warnings[:20],
                count=len(new_incidents),
            )
            self._apply(new_incidents)
            if was_problem:
                self.version += 1
        return True

    def _is_replaced(self, new_incidents: dict[str, dict]) -> bool:
        old_ids, new_ids = set(self.incidents), set(new_incidents)
        if not old_ids or not new_ids:
            return False
        return len(old_ids & new_ids) / max(len(old_ids), len(new_ids)) < 0.5

    def _set_file_problem(self, status: str, error: str) -> None:
        with self.lock:
            if self.file["status"] != status or self.file["error"] != error:
                self.file.update(status=status, error=error)
                self.version += 1

    # ---------- применение ----------
    def _apply(self, new_incidents: dict[str, dict]) -> None:
        now = self.clock.now()
        old = self.incidents
        # Запоминаем, когда увидели переход в «решён» — если в файле нет completionDate
        for inc_id, inc in new_incidents.items():
            prev = old.get(inc_id)
            if inc["state_kind"] in DONE_KINDS and (prev is None or prev["state_kind"] not in DONE_KINDS):
                if prev is not None:
                    self.seen_resolved[inc_id] = now
            elif inc["state_kind"] == "active":
                self.seen_resolved.pop(inc_id, None)
        self.groups = assign_groups(new_incidents, self.rules, now, self.seen_resolved)
        self._derive(new_incidents, now)

        if self.loaded:
            stamp = adapter.iso(_utcnow())
            clock_stamp = adapter.iso(now)
            for ev in diff(old, new_incidents):
                self.seq += 1
                ev.update(seq=self.seq, at=stamp, clock_at=clock_stamp)
                self.events.append(ev)
        self.incidents = new_incidents
        self.loaded = True
        self.desk.sync_alerts(new_incidents)
        self.version += 1

    def _derive(self, incidents: dict[str, dict], now: datetime) -> None:
        """Производные поля: прогноз срока, ИИ, очередь действий."""
        forecast_all(incidents, now)
        enrich(incidents, now, self.rules)
        self.actions = build_actions(incidents, now)
        self._last_ai = time.monotonic()

    def _regroup_if_needed(self, force: bool) -> bool:
        """Время идёт: решённые критические уходят из группы, риск растёт. Пересчитываем без перечитывания."""
        if not self.loaded:
            return False
        with self.lock:
            now = self.clock.now()
            before = {i: (tuple(x["groups"]), tuple(x["resolved_in"])) for i, x in self.incidents.items()}
            groups = assign_groups(self.incidents, self.rules, now, self.seen_resolved)
            after = {i: (tuple(x["groups"]), tuple(x["resolved_in"])) for i, x in self.incidents.items()}
            changed = before != after or groups != self.groups
            self.groups = groups
            if changed:
                self.desk.sync_alerts(self.incidents)
            if force or ai_layer.consume_dirty() or time.monotonic() - self._last_ai > AI_REFRESH_SECONDS:
                self._derive(self.incidents, now)
                changed = True
            if changed:
                self.version += 1
        return changed

    # ---------- выдача ----------
    def snapshot(self, events_limit: int = 50) -> dict:
        with self.lock:
            now = self.clock.now()
            groups = [
                {"key": g.key, "title": g.title, "ids": list(self.groups.get(g.key, [])), "count": len(self.groups.get(g.key, []))}
                for g in self.rules.groups
            ]
            return {
                "version": self.version,
                "server_time": adapter.iso(_utcnow()),
                "now": adapter.iso(now),
                "clock_offset_ms": int(self.clock.offset_seconds() * 1000),
                "time_mode": self.clock.mode,
                "export_anchor": adapter.iso(self.clock.anchor),
                "file": dict(self.file),
                "groups": groups,
                "counts": {g["key"]: sum(1 for i in g["ids"] if g["key"] not in self.incidents[i]["resolved_in"]) for g in groups},
                "incidents": self.incidents,
                "actions": self.actions,
                "ai_status": ai_layer.status(),
                **self.desk.export(),          # alerts, claims, coordinators — общее состояние смены
                "teams": self._teams(),
                "events": list(self.events)[-events_limit:],
                "last_event_seq": self.seq,
            }

    def _teams(self) -> list[str]:
        teams = {i["team"] for i in self.incidents.values() if i.get("team") and i["team"] != "AutoClosedMi"}
        teams.add(self.rules.coordinators_team)
        return sorted(teams, key=str.casefold)

    # ---------- действия смены (общие для стены и всех АРМ) ----------
    def ack(self, key: str, by: str) -> bool:
        ok = self.desk.ack(key, by)
        if ok:
            with self.lock:
                self.version += 1
        return ok

    def claim(self, inc_id: str, by: str) -> None:
        self.desk.claim(inc_id, by)
        with self.lock:
            self.version += 1

    def release(self, inc_id: str) -> None:
        if self.desk.release(inc_id):
            with self.lock:
                self.version += 1

    def events_after(self, seq: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["seq"] > seq]

    def incident(self, inc_id: str) -> dict | None:
        with self.lock:
            inc = self.incidents.get(inc_id)
            if inc is None:
                return None
            return {**inc, "raw": self.raw_by_id.get(inc_id)}
