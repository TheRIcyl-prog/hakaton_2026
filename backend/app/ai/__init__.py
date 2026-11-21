"""ИИ-слой: правила (всегда, мгновенно) + GigaChat поверх (в фоне, если включён).

enrich(incidents, now, rules) кладёт в каждый инцидент поле "ai" (форма — docs/contract.md, раздел «ai»):
выжимка, кому передать, риск стать критическим, тревожная динамика, дубли; у каждой функции —
данные для кнопки «почему?» (факторы, похожие, источник rules|gigachat|cache, время).

Воронка GigaChat: все → активные в группах (+ отмеченные динамикой) → нет в кэше → очередь
(критические → на координаторах → высокие → динамика → исследование) → GigaChat (1 поток на бесплатном тарифе).
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
from . import feedback as feedback_mod
from . import index as index_mod
from . import outages as outage_mod
from . import rules as rule_ai
from .llm import (LLMWorker, Task, build_dup_prompt, build_incident_prompt, dup_key, dynamics_key, incident_fields,
                  model_key, risk_key, route_key, summary_key)

log = logging.getLogger("dashboard.ai")

GROUP_PRIORITY = {"critical": 0, "coordinators": 1, "high": 2, "research": 4}
DYNAMICS_PRIORITY = 3
RISK_EXPLAIN_TOP = 20
ROUTE_VIABLE_SHARE = 0.2   # кандидат для GigaChat — не меньше 20% похожих от лидера
COMPACT_DUP = ("group_id", "master_id", "is_master", "size", "name", "confirmed", "kind")
ADVANCED_PARTS = ("route", "risk")

_worker: LLMWorker | None = None
_history = dyn_mod.History(None)
_feedback = feedback_mod.FeedbackStore(None)
_RULES_STATUS = {"provider": "rules", "state": "off", "label": "ИИ: правила (GigaChat выключен)", "error": None}
_status = _RULES_STATUS
_dup_groups: list[dict] = []
_outage_candidates: list[dict] = []


def setup(*, feedback_path: Path | None = None) -> None:
    """MVP starts with AI off. Rules and GigaChat are explicit opt-in modes."""
    global _worker, _status, _history, _feedback
    _worker = None
    mode = os.getenv("LLM_PROVIDER", "off").strip().lower()
    if mode not in ("rules", "gigachat"):
        _status = {"provider": "off", "state": "off", "label": "ИИ выключен", "error": None}
        _history = dyn_mod.History(None)
        _feedback = feedback_mod.FeedbackStore(None)
        return
    _status = _RULES_STATUS
    _history = dyn_mod.History(ROOT / "cache" / "ai_history.json")
    _feedback = feedback_mod.FeedbackStore(feedback_path or ROOT / "cache" / "ai_feedback.json")
    if mode != "gigachat":
        return
    from .gigachat import GigaChatClient, GigaChatError

    ca = os.getenv("GIGACHAT_CA_BUNDLE") or "certs/russian_trusted_root_ca.cer"
    ca_path = Path(ca) if Path(ca).is_absolute() else ROOT / ca
    try:
        client = GigaChatClient(
            os.getenv("GIGACHAT_CREDENTIALS", ""),
            os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            ca_path,
            os.getenv("GIGACHAT_MODEL", "").strip() or "GigaChat",
            timeout=float(os.getenv("GIGACHAT_TIMEOUT", "30")),
        )
    except GigaChatError as e:
        _status = {"provider": "gigachat", "state": "offline", "label": "ИИ: GigaChat не настроен, работают правила",
                   "error": str(e)}
        return
    if not ca_path.exists():
        log.warning("Нет %s — TLS к GigaChat может не пройти", ca_path)
    _worker = LLMWorker(client, ROOT / "cache" / "ai_cache.json",
                        concurrency=int(os.getenv("GIGACHAT_CONCURRENCY", "1")),
                        rpm=int(os.getenv("GIGACHAT_RPM", "20")),
                        advanced_model=os.getenv("GIGACHAT_MODEL_ADVANCED", "").strip() or None)


def reset(worker: LLMWorker | None = None, history: dyn_mod.History | None = None, *,
          feedback_path: Path | None = None) -> None:
    """Для тестов: чистое состояние, с памятью на диске только по явному пути."""
    global _worker, _history, _feedback, _dup_groups, _outage_candidates, _status
    _worker = worker
    _status = _RULES_STATUS
    _history = history or dyn_mod.History(None)
    _feedback = feedback_mod.FeedbackStore(feedback_path)
    _dup_groups = []
    _outage_candidates = []


def status() -> dict:
    return _worker.snapshot_status() if _worker else dict(_status)


def consume_dirty() -> bool:
    feedback_dirty = _feedback.consume_dirty()
    llm_dirty = _worker.consume_dirty() if _worker else False
    return feedback_dirty or llm_dirty


def record_feedback(inc: dict, by: str, kind: str, corrected_team: str | None = None) -> bool:
    """Записывает исправление для показанной подсказки; проверка команды — на стороне вызывающего API.

    True — сохранено, False — повтор. ValueError — некорректный ввод, OSError — ошибка хранения.
    """
    return _feedback.record(inc, by, kind, corrected_team)


def duplicate_groups() -> list[dict]:
    """Группы дублей последнего пересчёта (для стены и подтверждения координатором)."""
    return list(_dup_groups)


def outage_candidates() -> list[dict]:
    """Предложения межпродуктовых сбоев последнего пересчёта."""
    return [{**candidate, "ids": list(candidate["ids"])} for candidate in _outage_candidates]


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
    global _dup_groups, _outage_candidates
    if _status["provider"] == "off":
        # Keep deterministic outage grouping available to the coordinator in MVP mode.
        _dup_groups = []
        _outage_candidates = outage_mod.find_candidates(incidents)
        for inc in incidents.values():
            inc.pop("ai", None)
        return
    at = _real_now()
    idx = index_mod.build(incidents, rules)
    events_of = _events_source(refs)
    if refs is None or getattr(refs, "history", None) is None:
        _history.observe(incidents, now)      # своя история — запасная, если бэкенд её не дал
    groups = dup_mod.find_groups(incidents, idx)
    outages = outage_mod.find_candidates(incidents, idx)
    dup_of = {i: g for g in groups if not g.get("hidden") for i in g["ids"]}
    feedback_index = _feedback.route_index()

    watched: list[tuple[dict, list[str], dict | None]] = []   # кандидаты в GigaChat
    for inc in incidents.values():
        evs = events_of(inc["id"]) if inc["state_kind"] == "active" else None
        dyn = dyn_mod.detect(inc, evs, now) if evs else None
        g = dup_of.get(inc["id"])
        live = [x for x in inc.get("groups", ()) if x not in inc.get("resolved_in", ())]
        risk = rule_ai.risk(inc, idx, now, dyn, g["size"] if g else 0)
        route = None
        if inc.get("is_coordinators") and inc["state_kind"] in ("active", "research"):
            route = rule_ai.route(inc, idx, incidents, feedback_mod.route_evidence(inc, feedback_index))
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

    _dup_groups = [g for g in groups if not g.get("hidden")]
    if _worker:
        outages, tasks = outage_mod.refine(outages, incidents, idx, _worker)
        _overlay_llm(incidents, watched, groups, tasks)
    _outage_candidates = outages
    for inc in incidents.values():
        r = inc["ai"].get("route")
        if r:
            r.pop("_cands", None)
            r.pop("_total", None)
            r.pop("_feedback", None)


def _dup_card(g: dict, inc_id: str, at: str) -> dict:
    return {
        "group_id": g["id"], "master_id": g["master_id"], "is_master": inc_id == g["master_id"],
        "ids": g["ids"][:dup_mod.IDS_IN_CARD], "size": g["size"], "name": g["name"], "reason": g["reason"],
        "confirmed": g["confirmed"], "kind": g["tier"], "similarity": g["similarity"],
        "source": g.get("source", "rules"),
        "at": g.get("at", at),
    }


def _overlay_llm(incidents: dict[str, dict], watched: list, groups: list[dict], tasks: list[Task]) -> None:
    """Накладывает готовые ответы GigaChat из кэша и ставит в очередь то, чего в кэше нет."""
    w = _worker
    base_model = getattr(w, "base_model", None)
    advanced_model = getattr(w, "advanced_model", None)
    risk_top = sorted((inc for inc, _, _ in watched if (inc["ai"]["risk"] or {}).get("level") == "high"),
                      key=lambda i: -i["ai"]["risk"]["score"])[:RISK_EXPLAIN_TOP]
    risk_top_ids = {i["id"] for i in risk_top}
    task_of: dict[str, set[str]] = {}

    for inc, live, dyn in watched:
        ai = inc["ai"]
        fields = incident_fields(inc)
        missing: dict[str, str] = {}
        prompt_parts: dict = {}
        used_llm: list[str] = []

        def take(name: str, key: str, apply, part_payload) -> None:
            key = model_key(key, (advanced_model or base_model) if name in ADVANCED_PARTS else base_model)
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
            feedback = route.get("_feedback")
            # GigaChat выбирает только из весомых: команда с «6 из 378» — шум, а не кандидат
            viable = [c for c in cands if c["count"] >= max(2, ROUTE_VIABLE_SHARE * cands[0]["count"])] or cands[:1]
            if feedback and feedback.preferred_team:
                viable = [c for c in cands if c["team"] == feedback.preferred_team]
            examples = {"total": feedback.total, "by_team": feedback.counts, "examples": feedback.examples} if feedback else None

            def set_route(v, source, when):
                new = rule_ai.route_from(v["team"], cands, total, route["basis"], incidents, feedback, v.get("reason"))
                new["rules_team"] = route["team"]
                new.update(_meta(source, when))
                ai["route"] = new

            take("route", route_key(fields, viable, examples), set_route,
                 {"cands": viable, "total": total, "feedback": examples})

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
            batches = {False: {}, True: {}}
            for name, part_key in missing.items():
                batches[bool(advanced_model) and name in ADVANCED_PARTS][name] = part_key
            for advanced, parts in batches.items():
                if not parts:
                    continue
                key = "inc:" + inc["id"] + ":" + ",".join(sorted(parts.values()))
                payload = {name: prompt_parts[name] for name in parts}
                tasks.append(Task(key=key, priority=prio - (ai["risk"] or {}).get("score", 0) / 1000,
                                  prompt=build_incident_prompt(fields, payload), parts=parts,
                                  meta={"parts": payload}, advanced=advanced))
                task_of.setdefault(inc["id"], set()).add(key)

    for g in groups:
        g["all_ids"] = list(g["ids"])
        if g["confirmed"]:
            continue
        members = [{"id": i, "title": incidents[i].get("title"), "text": incident_fields(incidents[i])["краткое описание"]}
                   for i in g["ids"][:dup_mod.BURST_SHOWN]]
        key = model_key(dup_key(members), advanced_model or base_model)
        found = w.lookup(key)
        if found is None:
            if not w.known(key):
                prio = min((GROUP_PRIORITY.get(x, 9) for i in g["ids"] for x in incidents[i].get("groups", ())), default=5)
                tasks.append(Task(key="dup:" + key, priority=prio + (0.5 if g["tier"] == "text" else 0.7),
                                  prompt=build_dup_prompt(members), parts={"dup": key}, kind="dup",
                                  meta={"ids": [m["id"] for m in members]}, advanced=True))
            continue
        value, source, when = found
        if not value:
            continue
        g.update(source=source, at=when)
        keep = [i for i in g["ids"][:dup_mod.BURST_SHOWN] if i not in value["exclude"]] if g["tier"] == "burst" else             [i for i in g["ids"] if i not in value["exclude"]]
        if value["same"] and len(keep) >= 2:
            g["llm_confirmed"] = True
            g["hidden"] = False
            if value.get("name"):
                g["name"] = value["name"]
            if value.get("reason"):
                g["reason"] = value["reason"]
            g["ids"], g["size"] = keep, len(keep)
            if g["master_id"] not in keep:
                g["master_id"], g["id"] = keep[0], f"burst-{keep[0]}"
        else:
            g["rejected"] = True

    # Подтверждённый всплеск поглощает группы по тексту, целиком вошедшие в него; остальные — без пересечений
    owner: dict[str, dict] = {}
    for g in groups:
        if g["tier"] == "burst" and not g.get("hidden") and not g.get("rejected"):
            for i in g["ids"]:
                owner[i] = g
    for g in groups:
        if g["tier"] == "text" and not g.get("rejected"):
            taken = [i for i in g["ids"] if i in owner]
            if taken and len(taken) == len(g["ids"]):
                g["rejected"] = True                      # целиком внутри всплеска
            elif taken:
                for i in taken:                           # пересечение — оставляем за группой по тексту
                    b = owner.pop(i)
                    b["ids"].remove(i)
                    b["size"] = len(b["ids"])
    visible = [g for g in groups if not g.get("hidden") and not g.get("rejected") and len(g["ids"]) >= 2]

    assign: dict[str, dict] = {}
    for g in visible:                                     # группы по тексту раньше всплесков (порядок find_groups)
        for i in g["ids"]:
            assign.setdefault(i, g)
    for i in {i for g in groups for i in g["all_ids"]}:
        ai = incidents[i]["ai"]
        compact = "summary_meta" not in ai
        g = assign.get(i)
        if g:
            card = _dup_card(g, i, (ai.get("duplicates") or {}).get("at"))
            ai["duplicates"] = {k: card[k] for k in COMPACT_DUP} if compact else card
        elif compact:
            ai.pop("duplicates", None)
        else:
            ai["duplicates"] = None

    queued = w.submit(tasks)
    for inc_id, keys in task_of.items():
        if keys & queued:
            incidents[inc_id]["ai"]["pending"] = True
    global _dup_groups
    _dup_groups = visible
