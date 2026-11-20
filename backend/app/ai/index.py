"""Справочники ИИ за один проход по всем инцидентам (вместо сравнения «каждый со всеми»).

- route: (уровень, значение) → {команда: сколько похожих вела, сколько решила, примеры id}
  Уровни от точного к грубому: сервис → сценарий → продукт (код) → продукт.
- spike: сервис (или продукт) → отсортированные моменты регистрации активных (всплеск за ±2 ч — бинпоиском).
Строится за O(n log n) на каждую правку файла; на 5 000 инцидентов — десятки миллисекунд.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..adapter import norm_team, parse_dt
from ..config import Rules

NOT_ROUTABLE = {"autoclosedmi"}
DONE = ("resolved", "closed", "research")
ROUTE_LEVELS = (("service", "сервису"), ("scenario", "сценарию"), ("product_code", "продукту"), ("product", "продукту"))
MIN_SIMILAR = 3      # меньше — спускаемся на уровень грубее
EXAMPLES = 5
SPIKE_WINDOW = timedelta(hours=2)


@dataclass
class TeamStat:
    count: int = 0
    solved: int = 0
    solved_ids: list[str] = field(default_factory=list)
    open_ids: list[str] = field(default_factory=list)

    @property
    def examples(self) -> list[str]:
        """Примеры похожих: сначала решённые."""
        return (self.solved_ids + self.open_ids)[:EXAMPLES]


@dataclass
class Index:
    route: dict[tuple[str, str], dict[str, TeamStat]]
    spike: dict[str, list[float]]
    created: dict[str, datetime | None]

    def route_stats(self, inc: dict) -> tuple[str, str, dict[str, TeamStat]] | None:
        """Похожие по самому точному уровню, где набралось ≥ MIN_SIMILAR. → (поле, «по сервису «…»», статистика)."""
        fallback = None
        for fld, word in ROUTE_LEVELS:
            value = inc.get(fld)
            if not value:
                continue
            stats = self.route.get((fld, value))
            if not stats:
                continue
            total = sum(s.count for s in stats.values())
            label = f"по {word} «{value}»" if fld != "product_code" else f"по {word} «{inc.get('product') or value}»"
            if total >= MIN_SIMILAR:
                return fld, label, stats
            if fallback is None:
                fallback = (fld, label, stats)
        return fallback

    def spike_count(self, inc: dict) -> int:
        """Сколько ещё активных по тому же сервису (нет сервиса — продукту) создано в пределах ±2 ч."""
        times = self.spike.get(inc.get("service") or inc.get("product") or "")
        created = self.created.get(inc["id"])
        if not times or created is None:
            return 0
        t, w = created.timestamp(), SPIKE_WINDOW.total_seconds()
        n = bisect_right(times, t + w) - bisect_left(times, t - w)
        return max(0, n - (1 if inc["state_kind"] == "active" else 0))


def build(incidents: dict[str, dict], rules: Rules) -> Index:
    coord = norm_team(rules.coordinators_team)
    route: dict[tuple[str, str], dict[str, TeamStat]] = defaultdict(lambda: defaultdict(TeamStat))
    spike: dict[str, list[float]] = defaultdict(list)
    created_map: dict[str, datetime | None] = {}
    for inc in incidents.values():
        created = parse_dt(inc.get("created_at"))
        created_map[inc["id"]] = created
        if inc["state_kind"] == "active" and created and (inc.get("service") or inc.get("product")):
            spike[inc.get("service") or inc["product"]].append(created.timestamp())
        team = inc.get("team")
        nt = norm_team(team)
        if not team or nt == coord or nt in NOT_ROUTABLE:
            continue
        solved = inc["state_kind"] in DONE
        for fld, _ in ROUTE_LEVELS:
            value = inc.get(fld)
            if not value:
                continue
            st = route[(fld, value)][team]
            st.count += 1
            if solved:
                st.solved += 1
                if len(st.solved_ids) < EXAMPLES:
                    st.solved_ids.append(inc["id"])
            elif len(st.open_ids) < EXAMPLES:
                st.open_ids.append(inc["id"])
    for times in spike.values():
        times.sort()
    return Index(route={k: dict(v) for k, v in route.items()}, spike=dict(spike), created=created_map)
