"""ИИ на правилах — всегда включён, мгновенный, без сети. Числа считает только он (GigaChat пишет слова).

- выжимка: краткое описание из файла, ≤ 90 знаков (п. 8.1);
- кому передать: кандидаты по истории похожих из справочника, уверенность «8 из 10 похожих» (п. 8.2);
- риск стать критическим: балл 0–100 с разбивкой по факторам, «высокий» от 70 (п. 8.3).
Всё через справочники `index.build` за один проход — никаких сравнений «каждый со всеми».
"""
from __future__ import annotations

import re
from datetime import datetime

from ..adapter import parse_dt
from .feedback import RouteEvidence
from .index import Index
from .text import SUMMARY_MAX, clip, first_sentence

MASS_WORDS = re.compile(r"массов|все клиенты|у всех|недоступ|не работает|полност|регион", re.IGNORECASE)
HIGH_RISK = 70
MEDIUM_RISK = 40
CANDIDATES = 5
BACKUPS = 2
SIMILAR_SHOWN = 5


SHORT_ENOUGH = 30   # «Массово.», «Клиенты пишут.» — это не выжимка, берём первое предложение описания


def summary(inc: dict) -> str | None:
    short = inc.get("short_description")
    if short and len(short) >= SHORT_ENOUGH:
        return clip(short, SUMMARY_MAX)
    desc = first_sentence(inc.get("description"))
    best = max(filter(None, [short, desc]), key=len, default=None)
    if not best or len(best) < SHORT_ENOUGH:
        title = inc.get("title")
        best = f"{title}: {best}" if title and best and title.casefold() not in best.casefold() else best or title
    return clip(best, SUMMARY_MAX)


def similar_brief(ids: list[str], incidents: dict[str, dict]) -> list[dict]:
    out = []
    for i in ids[:SIMILAR_SHOWN]:
        o = incidents.get(i)
        if o:
            out.append({"id": i, "title": o.get("title"), "team": o.get("team"), "state": o.get("state"),
                        "url": o.get("itsm_url")})
    return out


def route(inc: dict, index: Index, incidents: dict[str, dict], feedback: RouteEvidence | None = None) -> dict | None:
    found = index.route_stats(inc)
    preferred = feedback.preferred_team if feedback else None
    if not found and not preferred:
        return None
    _, basis, stats = found or (None, "по продукту, сервису и сценарию", {})
    total = sum(s.count for s in stats.values())
    teams = sorted(stats, key=lambda team: (-stats[team].count, -stats[team].solved, team))[:CANDIDATES]
    if preferred and preferred not in teams:
        teams = [*teams[:CANDIDATES - 1], preferred]
    cands = []
    for team in teams:
        s = stats.get(team)
        cands.append({"team": team, "count": s.count if s else 0, "solved": s.solved if s else 0,
                      "score": round(s.count / total, 2) if s else 0.0, "similar_ids": s.examples if s else []})
    return route_from(preferred or teams[0], cands, total, basis, incidents, feedback)


def route_from(team: str, cands: list[dict], total: int, basis: str, incidents: dict[str, dict],
               feedback: RouteEvidence | None = None, model_reason: str | None = None) -> dict:
    """Маршрут на выбранную команду; уверенность — всегда по истории этой команды."""
    best = next(c for c in cands if c["team"] == team)
    others = [c for c in cands if c["team"] != team]
    n, k = best["count"], best["solved"]
    history = f"{n} из {total} похожих" if total else "Нет похожих в истории"
    reason = (f"{history} {basis} вела эта команда" + (f", решила {k}" if k else "")) if total else history
    # При наличии исправлений оставляем знаменатели в пояснении даже после ответа модели.
    if model_reason:
        reason = f"{model_reason.rstrip('. ')}. {reason}" if feedback else model_reason
    result = {
        "team": team,
        "confidence": best["score"],
        "confidence_text": history,
        "basis": basis,
        "reason": reason,
        "alternatives": [{"team": c["team"], "score": c["score"], "confidence_text": f"{c['count']} из {total} похожих"}
                         for c in others[:BACKUPS]],
        "candidates": [{k2: v for k2, v in c.items() if k2 != "similar_ids"} for c in cands],
        "similar_ids": best["similar_ids"],
        "similar": similar_brief(best["similar_ids"], incidents),
        "_cands": cands,          # служебное: для GigaChat, из снимка убирается
        "_total": total,
    }
    if feedback:
        count = feedback.counts.get(team, 0)
        result["_feedback"] = feedback
        result["feedback"] = {"count": count, "total": feedback.total, "by_team": feedback.counts}
        result["reason"] += f". В похожих заявках эту команду выбрали в {count} из {feedback.total} исправлений."
    return result


def risk(inc: dict, index: Index, now: datetime, dynamics: dict | None, dup_size: int = 0) -> dict | None:
    """Для активных высоких; средние и низкие — только если ИИ заметил тревожную динамику (п. 8.3, 8.5)."""
    if inc["state_kind"] != "active" or inc["priority_level"] == "critical":
        return None
    if inc["priority_level"] != "high" and not dynamics:
        return None
    parts: list[tuple[str, int]] = []
    base = {"high": 30, "medium": 10, "low": 0}.get(inc["priority_level"], 5)
    parts.append((f"приоритет «{inc.get('priority') or '—'}»", base))

    impact, urgency = inc.get("impact_id") or 0, inc.get("urgency_id") or 0
    if impact >= 4:
        parts.append(("влияние наивысшее", 15))
    elif impact == 3:
        parts.append(("влияние высокое", 8))
    if urgency >= 4:
        parts.append(("срочность наивысшая", 10))
    elif urgency == 3:
        parts.append(("срочность высокая", 5))
    if inc.get("service_type") == "Клиентский":
        parts.append(("затронуты клиенты", 10))

    created, deadline = index.created.get(inc["id"]), parse_dt(inc.get("deadline"))
    if created and deadline and deadline > created:
        frac = (now - created) / (deadline - created)
        if frac >= 1:
            parts.append(("срок нарушен", 20))
        elif frac >= 0.75:
            parts.append((f"прошло {int(frac * 100)}% срока", 12))
        elif frac >= 0.5:
            parts.append((f"прошло {int(frac * 100)}% срока", 5))
    fc = inc.get("forecast") or {}
    if fc.get("level") == "breach":
        parts.append(("прогноз: не успеют к сроку" + (f" (обычно {fc['median_minutes']} мин)" if fc.get("median_minutes") else ""), 10))

    if MASS_WORDS.search(" ".join(filter(None, [inc.get("title"), inc.get("description")]))):
        parts.append(("признаки массовости в описании", 8))
    spike = index.spike_count(inc)
    if spike >= 3 and dup_size < 2:
        parts.append((f"ещё {spike} открытых по «{inc.get('service') or inc.get('product')}» за ±2 ч", min(12, 3 + spike)))
    if dup_size >= 2 or inc.get("is_master"):
        parts.append((f"группа похожих: {dup_size}" if dup_size >= 2 else "мастер-инцидент с дублями", 10))
    if dynamics:
        parts.append((f"нарастает: {dynamics['text']}", dynamics["boost"]))

    score = max(0, min(100, sum(p for _, p in parts)))
    level = "high" if score >= HIGH_RISK else "medium" if score >= MEDIUM_RISK else "low"
    return {
        "score": score,
        "level": level,
        "factors": [label for label, pts in parts[1:] if pts > 0],
        "breakdown": [{"label": label, "points": pts} for label, pts in parts],
        "comment": None,
    }
