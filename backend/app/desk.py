"""Общее состояние смены: эпизоды тревог и список координаторов, выходивших на смену.

С 20.11 (decisions.md, п. 4 и 7) кнопки «Принял» нет: тревогу снимает «Взять в работу», а оно
пишется в файл инцидентов полем coordinator (itsm.py). Здесь остаются только эпизоды тревог:
когда инцидент вошёл в «критические»/«на координаторах» (since), — это не свойство инцидента,
поэтому живёт в cache/desk_state.json. Тревога «не взята», пока у инцидента нет координатора;
отпустили, а инцидент всё ещё в группе, — тревога снова не взята.
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
        self.alerts: dict[str, dict] = {}   # "group:id" -> {key, group, incident_id, since}
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
        self.alerts = {k: {"key": k, "group": a.get("group"), "incident_id": a.get("incident_id"), "since": a.get("since")}
                       for k, a in (data.get("alerts") or {}).items() if isinstance(a, dict)}
        self.coordinators = data.get("coordinators", [])

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"alerts": self.alerts, "coordinators": self.coordinators},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # состояние смены не должно ронять дашборд

    # ---------- тревоги ----------
    def sync_alerts(self, incidents: dict[str, dict]) -> bool:
        """Эпизод тревоги начинается, когда инцидент входит в группу, и кончается, когда выходит.
        Повторный вход = новый эпизод. True — если что-то изменилось."""
        active: set[str] = set()
        for inc in incidents.values():
            groups = inc.get("groups")
            if not groups:
                continue
            for g in ALERT_GROUPS:
                if g in groups and g not in inc.get("resolved_in", ()):
                    active.add(f"{g}:{inc['id']}")
        changed = False
        with self.lock:
            for key in list(self.alerts):
                if key not in active:
                    del self.alerts[key]
                    changed = True
            for key in active - set(self.alerts):
                group, inc_id = key.split(":", 1)
                self.alerts[key] = {"key": key, "group": group, "incident_id": inc_id, "since": self._now_iso()}
                changed = True
            if changed:
                self._save()
        return changed

    def remember(self, name: str) -> None:
        """Координатор что-то сделал — запомним имя для подсказки на других АРМ."""
        with self.lock:
            if name and name not in self.coordinators:
                self.coordinators.append(name)
                self.coordinators = self.coordinators[-20:]
                self._save()

    def _now_iso(self) -> str:
        return self.now_fn().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def export(self, incidents: dict[str, dict]) -> dict:
        """alerts — критические первыми; taken_by/acked_by — координатор из файла (acked_by оставлен для старого фронта).
        claims — {id: {by, at}} по полю coordinator активных инцидентов (совместимость с фронтом)."""
        with self.lock:
            alerts = []
            for a in sorted(self.alerts.values(), key=lambda a: (a["group"] != "critical", a["since"] or "")):
                coord = (incidents.get(a["incident_id"]) or {}).get("coordinator") or {}
                alerts.append({**a, "taken_by": coord.get("name"), "taken_at": coord.get("at"),
                               "acked_by": coord.get("name"), "acked_at": coord.get("at")})
            coordinators = list(self.coordinators)
        claims = {i: {"by": inc["coordinator"]["name"], "at": inc["coordinator"]["at"]}
                  for i, inc in incidents.items() if inc.get("coordinator") and inc.get("state_kind") == "active"}
        return {"alerts": alerts, "claims": claims, "coordinators": coordinators}
