"""ИИ-слой: правила (всегда, мгновенно) + GigaChat поверх (в фоне, если включён).

enrich(incidents, now, rules) кладёт в каждый инцидент поле "ai" (форма — docs/contract.md, раздел «ai»):
выжимка, кому передать, риск стать критическим, тревожная динамика, дубли; у каждой функции —
данные для кнопки «почему?» (факторы, похожие, источник rules|gigachat|cache, время).

Воронка GigaChat: все → активные в группах (+ отмеченные динамикой) → нет в кэше → очередь
(критические → на координаторах → высокие → динамика → исследование) → 2–3 запроса параллельно.
enrich() никогда не ходит в сеть: за один вызов — справочники O(n), правила, чтение кэша, постановка в очередь.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ..adapter import iso
from ..config import ROOT, Rules
from . import dynamics as dyn_mod
from . import duplicates as dup_mod
from . import index as index_mod
from . import rules as rule_ai
from .llm import (LLMWorker, Task, build_dup_prompt, build_incident_prompt, dup_key, dynamics_key, incident_fields,
                  risk_key, route_key, summary_key)

log = logging.getLogger("dashboard.ai")

GROUP_PRIORITY = {"critical": 0, "coordinators": 1, "high": 2, "research": 4}
DYNAMICS_PRIORITY = 3
RISK_EXPLAIN_TOP = 20
COMPACT_DUP = ("group_id", "master_id", "is_master", "size", "name", "confirmed")

_worker: LLMWorker | None = None
_history = dyn_mod.History(None)
_status = {"provider": "rules", "state": "off", "label": "ИИ: правила (GigaChat выключен)", "error": None}
_dup_groups: list[dict] = []


def setup() -> None:
    """История изменений — на диск всегда; GigaChat — если LLM_PROVIDER=gigachat и есть ключ."""
    global _worker, _status, _history
    _history = dyn_mod.History(ROOT / "cache" / "ai_history.json")
    if os.getenv("LLM_PROVIDER", "rules").lower() != "gigachat":
        return
    from .gigachat import GigaChatClient, GigaChatError

    ca = os.getenv("GIGACHAT_CA_BUNDLE") or "certs/russian_trusted_root_ca.cer"
    ca_path = Path(ca) if Path(ca).is_absolute() else ROOT / ca
    try:
        client = GigaChatClient(
            os.getenv("GIGACHAT_CREDENTIALS", ""),
            os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            ca_path,
            os.getenv("GIGACHAT_MODEL", "GigaChat"),
            timeout=float(os.getenv("GIGACHAT_TIMEOUT", "30")),
        )
    except GigaChatError as e:
        _status = {"provider": "gigachat", "state": "offline", "label": "ИИ: GigaChat не настроен, работают правила",
                   "error": str(e)}
        return
    if not ca_path.exists():
        log.warning("Нет %s — TLS к GigaChat может не пройти", ca_path)
    _worker = LLMWorker(client, ROOT / "cache" / "ai_cache.json",
                        concurrency=int(os.getenv("GIGACHAT_CONCURRENCY", "2")),
                        rpm=int(os.getenv("GIGACHAT_RPM", "20")))


def reset(worker: LLMWorker | None = None, history: dyn_mod.History | None = None) -> None:
    """Для тестов: чистое состояние без диска."""
    global _worker, _history, _dup_groups
    _worker = worker
    _history = history or dyn_mod.History(None)
    _dup_groups = []


def status() -> dict:
    return _worker.snapshot_status() if _worker else dict(_status)


def consume_dirty() -> bool:
    return _worker.consume_dirty() if _worker else False


def duplicate_groups() -> list[dict]:
    """Группы дублей последнего пересчёта (для стены и подтверждения координатором)."""
    return list(_dup_groups)


def history(inc_id: str) -> list[dict]:
    """Запасная история изменений, которую копит ИИ (основная — backend/app/history.py)."""
    return _history.get(inc_id)


def _real_now() -> str:
    return iso(datetime.now(timezone.utc))


def _meta(source: str, at: str | None) -> dict:
    return {"source": source, "at": at}


def _events_source(refs):
    """История изменений: бэкенда (refs.history, засеяна из workLog), иначе своя (dynamics.History)."""
    hist = getattr(refs, "history", None) if refs is not None else None
    if hist is not None and callable(getattr(hist, "get", None)):
        return hist.get
    return lambda inc_id: _history.events.get(inc_id)


def enrich(incidents: dict[str, dict], now: datetime, rules: Rules, refs=None) -> None:
    """refs — справочники бэкенда (backend/app/refs.py); из них ИИ берёт только историю изменений."""
    global _dup_groups
    at = _real_now()
    idx = index_mod.build(incidents, rules)
    events_of = _events_source(refs)
    if refs is None or getattr(refs, "history", None) is None:
        _history.observe(incidents, now)      # своя история — запасная, если бэкенд её не дал
    groups = dup_mod.find_groups(incidents, idx)
    dup_of = {i: g for g in groups for i in g["ids"]}

    watched: list[tuple[dict, list[str], dict | None]] = []   # кандидаты в GigaChat
    for inc in incidents.values():
        evs = events_of(inc["id"]) if inc["state_kind"] == "active" else None
        dyn = dyn_mod.detect(inc, evs, now) if evs else None
        g = dup_of.get(inc["id"])
        live = [x for x in inc.get("groups", ()) if x not in inc.get("resolved_in", ())]
        risk = rule_ai.risk(inc, idx, now, dyn, g["size"] if g else 0)
        route = rule_ai.route(inc, idx, incidents) if inc.get("is_coordinators") and inc["state_kind"] in ("active", "research") else None
        ai = {"source": "rules", "summary": rule_ai.summary(inc), "summary_meta": None, "route": route, "risk": risk,
              "dynamics": None, "duplicates": None, "pending": False}
        if dyn:
            ai["dynamics"] = {"flag": True, "label": "нарастает", "text": dyn["text"], "patterns": dyn["patterns"],
                              "since": dyn["since"], "events": dyn["events"], "comment": None, **_meta("rules", at)}
        if g:
            ai["duplicates"] = _dup_card(g, inc["id"], at)
        if live or dyn:
            ai["summary_meta"] = _meta("rules", at)
            if route:
                route.update(_meta("rules", at))
            if risk:
                risk.update(_meta("rules", at))
            watched.append((inc, live, dyn))
        else:
            # Не на экране (нет в группах, нет динамики): компактно, без подробностей «почему?» —
            # на 5 000 инцидентов это мегабайты снимка. Пустые поля не пишем.
            if risk:
                risk.pop("breakdown", None)
            if g:
                ai["duplicates"] = {k: ai["duplicates"][k] for k in COMPACT_DUP}
            ai = {k: v for k, v in ai.items() if v not in (None, False) or k == "source"}
        inc["ai"] = ai

    _dup_groups = groups
    if _worker:
        _overlay_llm(incidents, watched, groups)
    for inc in incidents.values():
        r = inc["ai"].get("route")
        if r:
            r.pop("_cands", None)
            r.pop("_total", None)


def _dup_card(g: dict, inc_id: str, at: str) -> dict:
    return {
        "group_id": g["id"], "master_id": g["master_id"], "is_master": inc_id == g["master_id"],
        "ids": g["ids"][:dup_mod.IDS_IN_CARD], "size": g["size"], "name": g["name"], "reason": g["reason"],
        "confirmed": g["confirmed"], "similarity": g["similarity"], "source": g.get("source", "rules"),
        "at": g.get("at", at),
    }


def _overlay_llm(incidents: dict[str, dict], watched: list, groups: list[dict]) -> None:
    """Накладывает готовые ответы GigaChat из кэша и ставит в очередь то, чего в кэше нет."""
    w = _worker
    risk_top = sorted((inc for inc, _, _ in watched if (inc["ai"]["risk"] or {}).get("level") == "high"),
                      key=lambda i: -i["ai"]["risk"]["score"])[:RISK_EXPLAIN_TOP]
    risk_top_ids = {i["id"] for i in risk_top}
    tasks: list[Task] = []
    task_of: dict[str, str] = {}

    for inc, live, dyn in watched:
        ai = inc["ai"]
        fields = incident_fields(inc)
        missing: dict[str, str] = {}
        prompt_parts: dict = {}
        used_llm: list[str] = []

        def take(name: str, key: str, apply, part_payload) -> None:
            found = w.lookup(key)
            if found is None:
                if not w.known(key):
                    missing[name] = key
                    prompt_parts[name] = part_payload
                return
            value, source, when = found
            if value:
                apply(value, source, when)
                used_llm.append(source)

        def set_summary(v, source, when):
            ai["summary"] = v["text"]
            ai["summary_meta"] = _meta(source, when)

        take("summary", summary_key(fields), set_summary, True)

        route = ai["route"]
        if route:
            cands, total = route["_cands"], route["_total"]

            def set_route(v, source, when, route=route, cands=cands, total=total):
                new = rule_ai.route_from(v["team"], cands, total, route["basis"], incidents)
                new["reason"] = v.get("reason") or new["reason"]
                new["rules_team"] = route["team"]
                new.update(_meta(source, when))
                ai["route"] = new

            take("route", route_key(fields, cands), set_route, {"cands": cands, "total": total})

        risk = ai["risk"]
        if risk and inc["id"] in risk_top_ids:
            def set_risk(v, source, when, risk=risk):
                risk["comment"] = v["comment"]
                risk["comment_meta"] = _meta(source, when)

            take("risk", risk_key(fields, risk), set_risk, {"score": risk["score"], "factors": risk["factors"]})

        if dyn:
            def set_dyn(v, source, when):
                ai["dynamics"]["comment"] = v["comment"]
                ai["dynamics"].update(_meta(source, when))

            take("dynamics", dynamics_key(fields, dyn), set_dyn, {"text": dyn["text"]})

        if used_llm:
            ai["source"] = "gigachat" if "gigachat" in used_llm else "cache"
        if missing:
            prio = min([GROUP_PRIORITY.get(x, 9) for x in live] + ([DYNAMICS_PRIORITY] if dyn else []))
            key = "inc:" + inc["id"] + ":" + ",".join(sorted(missing.values()))
            tasks.append(Task(key=key, priority=prio - (ai["risk"] or {}).get("score", 0) / 1000,
                              prompt=build_incident_prompt(fields, prompt_parts), parts=missing,
                              meta={"parts": prompt_parts}))
            task_of[inc["id"]] = key

    for g in groups:
        g["all_ids"] = list(g["ids"])
        if g["confirmed"]:
            continue
        members = [{"id": i, "title": incidents[i].get("title"), "text": incident_fields(incidents[i])["краткое описание"]}
                   for i in g["ids"][:12]]
        key = dup_key(members)
        found = w.lookup(key)
        if found is None:
            if not w.known(key):
                prio = min(GROUP_PRIORITY.get(x, 9) for i in g["ids"] for x in incidents[i].get("groups", ()) or [9]) \
                    if any(incidents[i].get("groups") for i in g["ids"]) else 5
                tasks.append(Task(key="dup:" + key, priority=prio + 0.5, prompt=build_dup_prompt(members),
                                  parts={"dup": key}, kind="dup", meta={"ids": [m["id"] for m in members]}))
            continue
        value, source, when = found
        if not value:
            continue
        g.update(source=source, at=when)
        if value["same"]:
            keep = [i for i in g["ids"] if i not in value["exclude"]]
            g["llm_confirmed"] = True
            if value.get("name"):
                g["name"] = value["name"]
            if value.get("reason"):
                g["reason"] = value["reason"]
            if len(keep) >= 2:
                g["ids"], g["size"] = keep, len(keep)
        else:
            g["rejected"] = True

    for g in groups:
        for i in g["all_ids"]:
            ai = incidents[i]["ai"]
            compact = "summary_meta" not in ai
            if g.get("rejected") or i not in g["ids"]:
                ai.pop("duplicates", None) if compact else ai.update(duplicates=None)
            else:
                card = _dup_card(g, i, (ai.get("duplicates") or {}).get("at"))
                ai["duplicates"] = {k: card[k] for k in COMPACT_DUP} if compact else card

    queued = w.submit(tasks)
    for inc_id, key in task_of.items():
        if key in queued:
            incidents[inc_id]["ai"]["pending"] = True
    global _dup_groups
    _dup_groups = [g for g in groups if not g.get("rejected")]
