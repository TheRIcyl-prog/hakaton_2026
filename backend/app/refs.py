"""Справочники на одну правку файла: строятся ОДНИМ проходом по всем инцидентам.

Раньше прогноз срока для каждого инцидента заново перебирал все решённые (n²). Теперь медианы
считаются один раз на правку, а поиск по ним — O(1).

Store передаёт Refs в ai.enrich(..., refs=refs), если у enrich есть такой параметр; иначе их можно
взять через current_refs(). Справочник «кому передать / всплеск» сессия ИИ держит у себя (ai/index.py).
    refs.now, refs.changed (id новых и изменённых по hash), refs.removed, refs.history (history.History)
    refs.median_resolution(team, priority_id) -> (секунды | None, основание, n)
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import TYPE_CHECKING

from .adapter import parse_dt

if TYPE_CHECKING:  # pragma: no cover
    from .history import History

DONE = ("resolved", "closed", "research")
MIN_SAMPLES = 2  # медиана по (команда, приоритет) — если примеров не меньше

_current: "Refs | None" = None


def current_refs() -> "Refs | None":
    """Последние построенные справочники (для кода, который не получает refs параметром)."""
    return _current


class Refs:
    def __init__(self, incidents: dict[str, dict], now: datetime, changed: set[str] | None = None,
                 removed: set[str] | None = None, history: "History | None" = None):
        global _current
        self.now = now
        self.changed: set[str] = set(incidents) if changed is None else changed
        self.removed: set[str] = removed or set()
        self.history = history

        by_tp: dict[tuple, list[float]] = defaultdict(list)
        by_p: dict[int | None, list[float]] = defaultdict(list)
        all_d: list[float] = []
        for inc in incidents.values():
            if inc.get("state_kind") not in DONE:
                continue
            c, r = parse_dt(inc.get("created_at")), parse_dt(inc.get("resolved_at"))
            if c and r and r > c:
                d = (r - c).total_seconds()
                by_tp[(inc.get("team"), inc.get("priority_id"))].append(d)
                by_p[inc.get("priority_id")].append(d)
                all_d.append(d)
        self._med_tp = {k: (median(v), len(v)) for k, v in by_tp.items() if len(v) >= MIN_SAMPLES}
        self._med_p = {k: (median(v), len(v)) for k, v in by_p.items() if len(v) >= MIN_SAMPLES}
        self._med_all = (median(all_d), len(all_d)) if all_d else None
        _current = self

    def median_resolution(self, team: str | None, priority_id: int | None) -> tuple[float | None, str, int]:
        """Медиана времени решения: (команда, приоритет) → приоритет → все решённые."""
        if (team, priority_id) in self._med_tp:
            m, n = self._med_tp[(team, priority_id)]
            return m, "команда и приоритет", n
        if priority_id in self._med_p:
            m, n = self._med_p[priority_id]
            return m, "приоритет", n
        if self._med_all:
            return self._med_all[0], "все решённые", self._med_all[1]
        return None, "нет истории", 0
