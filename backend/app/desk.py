"""Общее состояние смены: тревоги с подтверждением и «кто взял в работу».

Это не свойства инцидента, а состояние работы координаторов, поэтому живёт не в файле
инцидентов, а в cache/desk_state.json. Общее для видеостены и всех АРМ: координатор нажал
«Принял» у себя — баннер пропадает и на стене.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

ALERT_GROUPS = ("critical", "coordinators")


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


class DeskState:
    def __init__(self, path: Path | None):
        self.path = path
        self.now_fn = _real_now  # Store подставляет часы дашборда, чтобы время совпадало с экраном
        self.lock = threading.Lock()
        self.alerts: dict[str, dict] = {}   # "group:id" -> {group, incident_id, since, acked_by, acked_at}
        self.claims: dict[str, dict] = {}   # id -> {by, at}
        self.coordinators: list[str] = []   # кто выходил на смену (для подсказки имени)
        self._load()

    # ---------- хранение ----------
    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.alerts = data.get("alerts", {})
        self.claims = data.get("claims", {})
        self.coordinators = data.get("coordinators", [])

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"alerts": self.alerts, "claims": self.claims, "coordinators": self.coordinators},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # состояние смены не должно ронять дашборд

    # ---------- тревоги ----------
    def sync_alerts(self, incidents: dict[str, dict]) -> bool:
        """Эпизод тревоги начинается, когда инцидент входит в группу, и кончается, когда выходит.
        Повторный вход = новый эпизод, «Принял» сбрасывается. True — если что-то изменилось."""
        active: set[str] = set()
        for inc in incidents.values():
            for g in ALERT_GROUPS:
                if g in inc.get("groups", []) and g not in inc.get("resolved_in", []):
                    active.add(f"{g}:{inc['id']}")
        changed = False
        with self.lock:
            for key in list(self.alerts):
                if key not in active:
                    del self.alerts[key]
                    changed = True
            for key in active - set(self.alerts):
                group, inc_id = key.split(":", 1)
                self.alerts[key] = {"key": key, "group": group, "incident_id": inc_id, "since": self._now_iso(),
                                    "acked_by": None, "acked_at": None}
                changed = True
            # «В работе у …» снимаем, когда инцидент ушёл из всех групп (решён/передан и т.п.)
            for inc_id in list(self.claims):
                inc = incidents.get(inc_id)
                if not inc or not [g for g in inc.get("groups", []) if g not in inc.get("resolved_in", [])]:
                    del self.claims[inc_id]
                    changed = True
            if changed:
                self._save()
        return changed

    def ack(self, key: str, by: str) -> bool:
        with self.lock:
            a = self.alerts.get(key)
            if not a:
                return False
            a["acked_by"], a["acked_at"] = by, self._now_iso()
            self._remember(by)
            self._save()
            return True

    # ---------- в работе ----------
    def claim(self, inc_id: str, by: str) -> None:
        with self.lock:
            self.claims[inc_id] = {"by": by, "at": self._now_iso()}
            self._remember(by)
            self._save()

    def release(self, inc_id: str) -> bool:
        with self.lock:
            if self.claims.pop(inc_id, None) is None:
                return False
            self._save()
            return True

    def _now_iso(self) -> str:
        return self.now_fn().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _remember(self, name: str) -> None:
        if name and name not in self.coordinators:
            self.coordinators.append(name)
            self.coordinators = self.coordinators[-20:]

    def export(self) -> dict:
        with self.lock:
            return {
                "alerts": sorted(self.alerts.values(), key=lambda a: (a["group"] != "critical", a["since"])),
                "claims": dict(self.claims),
                "coordinators": list(self.coordinators),
            }
