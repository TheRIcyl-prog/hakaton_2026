"""ИИ-слой: правила (всегда) + GigaChat поверх (если настроен и доступен).

enrich(incidents, now, rules) кладёт в каждый инцидент поле "ai":
    {
      "source": "rules" | "gigachat",
      "summary": str | None,               # выжимка до 200 символов
      "route": {                           # только для инцидентов на координаторах
          "team": str, "confidence": float 0..1, "reason": str,
          "alternatives": [{"team": str, "score": float}], "similar_ids": [str]
      } | None,
      "risk": {"score": int 0..100, "level": "low|medium|high", "factors": [str], "comment": str|None} | None,
      "pending": bool                      # LLM ещё думает
    }
Числа считает код, слова пишет LLM. LLM-вызовы идут в фоне и не блокируют опрос файла.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from ..config import ROOT, Rules
from . import rules as rule_ai

log = logging.getLogger("dashboard.ai")
_worker = None
_status = {"provider": "rules", "state": "ok", "error": None}

GROUP_PRIORITY = {"critical": 0, "coordinators": 1, "high": 2, "research": 3}


def setup() -> None:
    """Включает GigaChat, если LLM_PROVIDER=gigachat и есть ключ."""
    global _worker, _status
    if os.getenv("LLM_PROVIDER", "rules").lower() != "gigachat":
        return
    from .gigachat import GigaChatClient, GigaChatError
    from .llm import LLMWorker

    ca = os.getenv("GIGACHAT_CA_BUNDLE") or "certs/russian_trusted_root_ca.cer"
    ca_path = Path(ca) if Path(ca).is_absolute() else ROOT / ca
    try:
        client = GigaChatClient(
            os.getenv("GIGACHAT_CREDENTIALS", ""),
            os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            ca_path,
            os.getenv("GIGACHAT_MODEL", "GigaChat"),
        )
    except GigaChatError as e:
        _status = {"provider": "gigachat", "state": "error", "error": str(e)}
        return
    _worker = LLMWorker(client, ROOT / "cache" / "ai_cache.json")
    if not ca_path.exists():
        log.warning("Нет %s — TLS к GigaChat может не пройти", ca_path)


def status() -> dict:
    return dict(_worker.status) if _worker else dict(_status)


def consume_dirty() -> bool:
    return _worker.consume_dirty() if _worker else False


def _candidates(route: dict | None) -> list[dict]:
    if not route:
        return []
    first = {"team": route["team"], "score": route["confidence"]}
    return [first, *route["alternatives"]][:4]


def enrich(incidents: dict[str, dict], now: datetime, rules: Rules) -> None:
    rule_ai.enrich(incidents, now, rules)
    if not _worker:
        return
    for inc in incidents.values():
        groups = [g for g in inc.get("groups", []) if g not in inc.get("resolved_in", [])]
        if not groups:
            continue
        ai = inc["ai"]
        cands = _candidates(ai.get("route"))
        res = _worker.get(inc, cands, min(GROUP_PRIORITY.get(g, 9) for g in groups))
        if res is None:
            ai["pending"] = True
            continue
        ai["source"] = "gigachat"
        if res.get("summary"):
            ai["summary"] = res["summary"]
        if ai.get("route") and res.get("route_team"):
            route = ai["route"]
            if res["route_team"] != route["team"]:
                # LLM выбрал другого кандидата — уверенность берём его долю
                alt = next((a for a in route["alternatives"] if a["team"] == res["route_team"]), None)
                route["alternatives"] = [a for a in route["alternatives"] if a["team"] != res["route_team"]]
                route["alternatives"].insert(0, {"team": route["team"], "score": route["confidence"]})
                route["team"], route["confidence"] = res["route_team"], alt["score"] if alt else route["confidence"]
            if res.get("route_reason"):
                route["reason"] = res["route_reason"]
        if ai.get("risk") and res.get("risk_comment"):
            ai["risk"]["comment"] = res["risk_comment"]
