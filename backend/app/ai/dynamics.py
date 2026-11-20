"""Тревожная динамика (п. 8.5): шаблоны по истории изменений инцидента.

История: между двумя чтениями файла сравниваем у каждого инцидента приоритет, команду, срок и статус
и пишем изменения в журнал (`cache/ai_history.json`, переживает перезапуск). Один проход O(n).
Шаблоны (правила, без LLM):
- приоритет повышали ≥2 раз за 2 ч;
- переназначали ≥3 раз за 4 ч;
- срок переносили ≥2 раз;
- вернули в работу после «Завершен».
Следим за всеми активными, включая средние и низкие. Слова для отмеченных пишет GigaChat (в __init__).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..adapter import iso, parse_dt
from .text import minutes_text, plural

log = logging.getLogger("dashboard.ai")
RETENTION = timedelta(hours=24)
MAX_EVENTS = 30
SAVE_EVERY = 2.0
MSK = timezone(timedelta(hours=3))

PRIO_UP_N, PRIO_UP_WINDOW = 2, timedelta(hours=2)
REASSIGN_N, REASSIGN_WINDOW = 3, timedelta(hours=4)
DEADLINE_N = 2
DONE = ("resolved", "research")   # «вернули в работу» — из решённого; закрытый финальный


def _state(inc: dict) -> list:
    return [inc.get("priority_id"), inc.get("priority"), inc.get("team"), inc.get("deadline"), inc.get("state_kind"),
            inc.get("state")]


class History:
    def __init__(self, path: Path | None):
        self.path = path
        self.last: dict[str, list] = {}
        self.events: dict[str, list[dict]] = {}
        self._dirty = False
        self._saved_at = 0.0
        self._lock = threading.Lock()
        self._load()

    # ----- диск -----
    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.last = data.get("last") or {}
            self.events = data.get("events") or {}
        except (OSError, ValueError, AttributeError):
            pass

    def flush(self, force: bool = False) -> None:
        if not self.path or not self._dirty:
            return
        if not force and time.monotonic() - self._saved_at < SAVE_EVERY:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last": self.last, "events": self.events}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
            self._dirty = False
            self._saved_at = time.monotonic()
        except OSError:
            log.exception("не удалось сохранить историю изменений")

    # ----- наблюдение -----
    def observe(self, incidents: dict[str, dict], now: datetime) -> None:
        with self._lock:
            if self.last and incidents:
                common = len(self.last.keys() & incidents.keys())
                if common / max(len(self.last), len(incidents)) < 0.5:
                    # Файл подменили целиком — новая точка отсчёта, без лавины изменений
                    self.last, self.events, self._dirty = {}, {}, True
            stamp = iso(now)
            for inc_id, inc in incidents.items():
                cur = _state(inc)
                prev = self.last.get(inc_id)
                if prev == cur:
                    continue
                self.last[inc_id] = cur
                self._dirty = True
                if prev is None or len(prev) != len(cur):
                    continue
                evs = self.events.setdefault(inc_id, [])
                p_id, p_name, team, dl, kind, state = prev
                if p_id != cur[0] and p_id is not None and cur[0] is not None:
                    evs.append({"at": stamp, "kind": "priority", "before": p_name, "after": cur[1],
                                "up": cur[0] > p_id})
                if team != cur[2]:
                    evs.append({"at": stamp, "kind": "team", "before": team, "after": cur[2]})
                if dl != cur[3]:
                    b, a = parse_dt(dl), parse_dt(cur[3])
                    evs.append({"at": stamp, "kind": "deadline", "before": dl, "after": cur[3],
                                "later": bool(b and a and a > b),
                                "shift_min": round((a - b).total_seconds() / 60) if a and b else None})
                if kind in DONE and cur[4] == "active":
                    evs.append({"at": stamp, "kind": "reopen", "before": state, "after": cur[5]})
                elif state != cur[5]:
                    evs.append({"at": stamp, "kind": "state", "before": state, "after": cur[5]})
                if len(evs) > MAX_EVENTS:
                    del evs[:-MAX_EVENTS]
            self._trim(now)
        self.flush()

    def _trim(self, now: datetime) -> None:
        border = iso(now - RETENTION)
        for inc_id in [i for i, evs in self.events.items() if not evs or evs[-1]["at"] < border]:
            del self.events[inc_id]

    def get(self, inc_id: str) -> list[dict]:
        with self._lock:
            return list(self.events.get(inc_id, ()))


def _within(evs: list[dict], now: datetime, window: timedelta) -> list[dict]:
    border = now - window
    return [e for e in evs if (at := parse_dt(e.get("at"))) and at >= border]


def _is_up(e: dict) -> bool:
    if "up" in e:
        return bool(e["up"])
    b, a = e.get("before_id"), e.get("after_id")
    return isinstance(b, int) and isinstance(a, int) and a > b


def _is_later(e: dict) -> bool:
    if "later" in e:
        return bool(e["later"])
    b, a = parse_dt(e.get("before")), parse_dt(e.get("after"))
    return bool(a and b and a > b)


def _normalize(evs: list[dict]) -> list[dict]:
    """События из истории бэкенда (refs.history) или своей: по времени, с up/later/shift_min."""
    out = []
    for e in evs:
        if not isinstance(e, dict) or not e.get("at") or not e.get("kind"):
            continue
        e = dict(e)
        if e["kind"] == "priority":
            e["up"] = _is_up(e)
        elif e["kind"] == "deadline":
            e["later"] = _is_later(e)
            if e.get("shift_min") is None:
                b, a = parse_dt(e.get("before")), parse_dt(e.get("after"))
                e["shift_min"] = round((a - b).total_seconds() / 60) if a and b else None
        out.append(e)
    out.sort(key=lambda e: parse_dt(e["at"]) or datetime.min.replace(tzinfo=timezone.utc))
    return out


def _span(evs: list[dict], now: datetime) -> str:
    first = parse_dt(evs[0]["at"])
    last = parse_dt(evs[-1]["at"]) or now
    return minutes_text(((last - first).total_seconds() if first else 0) / 60)


def detect(inc: dict, evs: list[dict], now: datetime) -> dict | None:
    """Шаблоны для одного активного инцидента. → {"patterns": [...], "text": "…", "boost": int} или None."""
    if inc.get("state_kind") != "active" or not evs:
        return None
    evs = _normalize(evs)
    patterns: list[dict] = []

    ups = [e for e in _within(evs, now, PRIO_UP_WINDOW) if e["kind"] == "priority" and e["up"]]
    if len(ups) >= PRIO_UP_N:
        chain = " → ".join([ups[0]["before"] or "?"] + [e["after"] or "?" for e in ups])
        patterns.append({"kind": "priority_up", "count": len(ups),
                         "text": f"{chain} за {_span(ups, now)}"})

    moves = [e for e in _within(evs, now, REASSIGN_WINDOW) if e["kind"] == "team"]
    if len(moves) >= REASSIGN_N:
        patterns.append({"kind": "reassign", "count": len(moves),
                         "text": f"переназначали {len(moves)} {plural(len(moves), 'раз', 'раза', 'раз')} "
                                 f"за {_span(moves, now)}: " + " → ".join(
                                     [moves[0]["before"] or "—"] + [e["after"] or "—" for e in moves])})

    later = [e for e in evs if e["kind"] == "deadline" and e["later"]]
    if len(later) >= DEADLINE_N:
        shifts = ", ".join(f"+{minutes_text(e['shift_min'])}" for e in later if e.get("shift_min"))
        patterns.append({"kind": "deadline_moved", "count": len(later),
                         "text": f"срок переносили {len(later)} {plural(len(later), 'раз', 'раза', 'раз')}"
                                 + (f" ({shifts})" if shifts else "")})

    reopen = [e for e in evs if e["kind"] == "reopen"]
    if reopen:
        at = parse_dt(reopen[-1]["at"])
        patterns.append({"kind": "reopened", "count": len(reopen),
                         "text": f"вернули в работу после «{reopen[-1]['before'] or 'Завершен'}»"
                                 + (f" в {at.astimezone(MSK):%H:%M}" if at else "")})

    if not patterns:
        return None
    return {
        "patterns": patterns,
        "text": "; ".join(p["text"] for p in patterns),
        "boost": min(30, 15 + 5 * (len(patterns) - 1)),
        "since": evs[0]["at"],
        "events": evs[-10:],
    }
