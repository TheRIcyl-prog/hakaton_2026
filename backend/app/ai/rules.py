"""ИИ-заглушка на правилах: выжимка, куда передать, риск стать критическим."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta

from ..adapter import norm_team, parse_dt
from ..config import Rules

# Команды, которые не являются «куда передать»
NOT_ROUTABLE = {"autoclosedmi"}
MASS_WORDS = re.compile(r"массов|все клиенты|у всех|недоступ|не работает|полност|регион", re.IGNORECASE)


def _summary(inc: dict) -> str | None:
    text = inc.get("short_description") or inc.get("description") or inc.get("title")
    if not text:
        return None
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return first[:200]


def _cases(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = "похожий случай"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = "похожих случая"
    else:
        word = "похожих случаев"
    return f"{n} {word}"


def _route(inc: dict, incidents: dict[str, dict], rules: Rules) -> dict | None:
    coord = norm_team(rules.coordinators_team)
    scores: dict[str, float] = defaultdict(float)
    similar: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for other in incidents.values():
        if other["id"] == inc["id"]:
            continue
        team = other.get("team")
        nt = norm_team(team)
        if not team or nt == coord or nt in NOT_ROUTABLE:
            continue
        s = 0.0
        if inc.get("service") and other.get("service") == inc.get("service"):
            s += 3
        if inc.get("scenario") and other.get("scenario") == inc.get("scenario"):
            s += 2
        if inc.get("product_code") and other.get("product_code") == inc.get("product_code"):
            s += 1.5
        elif inc.get("product") and other.get("product") == inc.get("product"):
            s += 1
        if s == 0:
            continue
        if other["state_kind"] in ("resolved", "closed", "research"):
            s += 0.5  # решённый случай — команда точно справилась
        scores[team] += s
        similar[team].append((s, other["id"]))
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    total = sum(scores.values())
    team, best = ranked[0]
    ids = [i for _, i in sorted(similar[team], reverse=True)[:5]]
    return {
        "team": team,
        "confidence": round(best / total, 2),
        "reason": f"{_cases(len(similar[team]))} по «{inc.get('service') or inc.get('product')}» вела эта команда",
        "alternatives": [{"team": t, "score": round(v / total, 2)} for t, v in ranked[1:4]],
        "similar_ids": ids,
    }


def _risk(inc: dict, incidents: dict[str, dict], now: datetime) -> dict | None:
    if inc["state_kind"] != "active" or inc["priority_level"] == "critical":
        return None
    score = 0
    factors: list[str] = []
    base = {"high": 35, "medium": 15, "low": 5}.get(inc["priority_level"], 10)
    score += base
    if (inc.get("impact_id") or 0) >= 4:
        score += 20
        factors.append("влияние наивысшее")
    elif (inc.get("impact_id") or 0) == 3:
        score += 10
        factors.append("влияние высокое")
    if (inc.get("urgency_id") or 0) >= 4:
        score += 10
        factors.append("срочность наивысшая")
    if inc.get("service_type") == "Клиентский":
        score += 10
        factors.append("затронуты клиенты")
    if inc.get("is_master"):
        score += 10
        factors.append("мастер-инцидент с дублями")

    created, deadline = parse_dt(inc.get("created_at")), parse_dt(inc.get("deadline"))
    if created and deadline and deadline > created:
        frac = (now - created) / (deadline - created)
        if frac >= 1:
            score += 25
            factors.append("срок нарушен")
        elif frac >= 0.75:
            score += 15
            factors.append(f"прошло {int(frac * 100)}% срока")

    if MASS_WORDS.search(" ".join(filter(None, [inc.get("title"), inc.get("description")]))):
        score += 10
        factors.append("признаки массовости в описании")

    window = timedelta(hours=2)
    same_product = [
        o for o in incidents.values()
        if o["id"] != inc["id"] and o["state_kind"] == "active" and o.get("product") == inc.get("product")
        and (c := parse_dt(o.get("created_at"))) and abs(c - (created or now)) <= window
    ]
    if len(same_product) >= 2:
        score += 10
        factors.append(f"всплеск по продукту: ещё {len(same_product)} за 2 ч")

    score = max(0, min(100, score))
    level = "high" if score >= 60 else "medium" if score >= 35 else "low"
    return {"score": score, "level": level, "factors": factors}


def enrich(incidents: dict[str, dict], now: datetime, rules: Rules) -> None:
    for inc in incidents.values():
        inc["ai"] = {
            "source": "rules",
            "summary": _summary(inc),
            "route": _route(inc, incidents, rules) if inc.get("is_coordinators") else None,
            "risk": _risk(inc, incidents, now),
        }
