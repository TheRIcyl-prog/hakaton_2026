"""Карточки сбоев из явных связей, предложений ИИ и подтверждений."""
from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .adapter import iso, parse_dt
from .ai.outages import MIN_PRODUCTS, WINDOW
from .groups import finished_until, resolved_moment


@dataclass(frozen=True)
class OutageCandidate:
    link: dict
    hashes: dict[str, str]
    summary: dict
    explicit: bool = False


@dataclass(frozen=True)
class Outage:
    summary: dict
    expires_at: datetime | None
    completed_at: datetime | None = None

    def visible(self, now: datetime) -> bool:
        return self.summary["status"] == "active" or (self.expires_at is not None and now < self.expires_at)


def _summary(link: dict, members: list[dict]) -> dict:
    active = [inc for inc in members if inc["state_kind"] == "active"]
    deadlines = (parse_dt(inc["deadline"]) for inc in active)
    return {
        "id": link["id"], "name": link["name"], "reason": link["reason"], "source": link["source"],
        "incident_ids": [inc["id"] for inc in members],
        "products": sorted({inc["product"] for inc in members if inc["product"]}),
        "active_count": len(active),
        "priority_id": max((inc["priority_id"] for inc in active if inc["priority_id"] is not None), default=None),
        "deadline": iso(min((dt for dt in deadlines if dt is not None), default=None)),
    }


def _available(inc: dict) -> bool:
    return inc["state_kind"] == "active" and not inc.get("outage")


def growth_proposals(members: list[dict], incidents: dict[str, dict]) -> list[dict]:
    active = [inc for inc in members if inc["state_kind"] == "active"]
    if not active:
        return []
    member_ids = {inc["id"] for inc in members}
    excluded = set().union(*(inc.get("_outage_excluded_with", ()) for inc in members))
    dates_by_code = defaultdict(list)
    for member in active:
        if (code := member["product_code"]) and (created := parse_dt(member["created_at"])):
            dates_by_code[code].append(created)
    for dates in dates_by_code.values():
        dates.sort()
    proposals = []
    for inc in incidents.values():
        if not _available(inc) or inc["id"] in excluded or member_ids.intersection(inc.get("_outage_excluded_with", ())):
            continue
        cause = inc.get("cause_incident_id")
        if cause in member_ids:
            reason = f"В выгрузке указана связь с {cause}"
        elif cause:
            continue
        else:
            code = inc.get("product_code")
            dates = dates_by_code.get(code)
            if not dates:
                continue
            created = parse_dt(inc["created_at"])
            if not created:
                continue
            earlier_members = bisect_right(dates, created)
            if not earlier_members or created - dates[earlier_members - 1] > WINDOW:
                continue
            reason = f"Общий код {code}, заявка возникла в течение {int(WINDOW.total_seconds() / 3600)} ч"
        proposals.append({key: inc[key] for key in ("id", "title", "product", "hash")} | {"reason": reason})
    return proposals


def _compatible_effects(root: str, effects: list[str], parents: dict[str, str], incidents: dict[str, dict]) -> list[str]:
    members = {root, *effects}
    blocked = set()
    for inc_id in members:
        for other in (members.intersection(incidents[inc_id].get("_outage_excluded_with", ())) - {inc_id}):
            ancestor = other
            while ancestor is not None and ancestor != inc_id:
                ancestor = parents.get(ancestor)
            # Обычно исключаем инициатора запрета. Если он предок, отсекаем зависимую ветку.
            blocked.add(other if ancestor == inc_id else inc_id)
    if not blocked:
        return effects

    children = defaultdict(list)
    for effect in effects:
        children[parents[effect]].append(effect)
    kept = []
    pending = list(children[root])
    while pending:
        effect = pending.pop()
        if effect not in blocked:
            kept.append(effect)
            pending.extend(children[effect])
    return kept


def _explicit_proposals(incidents: dict[str, dict]) -> Iterable[dict]:
    parents = {inc_id: inc["cause_incident_id"] for inc_id, inc in incidents.items()
               if inc.get("cause_incident_id") and _available(inc)}
    roots: dict[str, str | None] = {}
    for inc_id in parents:
        cursor = inc_id
        path = set()
        while cursor in parents and cursor not in roots and cursor not in path:
            path.add(cursor)
            cursor = parents[cursor]
        if cursor in roots:
            root = roots[cursor]
        elif cursor not in path and cursor in incidents and _available(incidents[cursor]):
            root = cursor
        else:
            root = None
        # Запоминаем в том числе тупики и циклы, чтобы повторно не обходить их ветки.
        roots.update(dict.fromkeys(path, root))

    effects = defaultdict(list)
    for inc_id, root in roots.items():
        if root is not None:
            effects[root].append(inc_id)
    for root in sorted(effects):
        kept = _compatible_effects(root, effects[root], parents, incidents)
        yield {"master_id": root, "ids": [root, *kept], "source": "rules",
               "name": f"Причина и следствия · {incidents[root]['title']}",
               "reason": f"Явные ссылки causeIncidentId ведут к {root}. Связь требует подтверждения координатором."}


def _narrowed_proposals(raw_by_id: dict[str, dict], incidents: dict[str, dict]) -> Iterable[dict]:
    for raw in raw_by_id.values():
        scopes = raw.get("outageExclusions")
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if not isinstance(scope, dict) or scope.get("candidate_remainder") is not True:
                continue
            ids = scope.get("incident_ids")
            if not isinstance(ids, list) or len(ids) < 2 or not all(isinstance(i, str) and i in incidents for i in ids):
                continue
            members = [incidents[i] for i in ids]
            codes = {inc["product_code"] for inc in members}
            products = {inc["product"] for inc in members if inc["product"]}
            if len(codes) != 1 or None in codes or len(products) < MIN_PRODUCTS:
                continue
            dates = [(parse_dt(inc["created_at"]), inc["id"]) for inc in members]
            if any(at is None for at, _ in dates):
                continue
            dates.sort()
            if dates[-1][0] - dates[0][0] > WINDOW:
                continue
            code = members[0]["product_code"]
            yield {"master_id": dates[0][1], "ids": ids, "source": "rules", "name": f"Сбой · {code}",
                   "reason": f"Координатор уточнил состав. У оставшихся заявок общий код {code}."}


def build_candidates(proposals: Iterable[dict], incidents: dict[str, dict], *,
                     raw_by_id: dict[str, dict] | None = None) -> dict[str, OutageCandidate]:
    explicit = _build_candidates(_explicit_proposals(incidents), incidents, min_members=2, reserved=set(), explicit=True)
    reserved = {inc_id for candidate in explicit.values() for inc_id in candidate.hashes}
    narrowed = _build_candidates(_narrowed_proposals(raw_by_id or {}, incidents), incidents,
                                 min_members=2, reserved=reserved)
    reserved.update(inc_id for candidate in narrowed.values() for inc_id in candidate.hashes)
    return {**explicit, **narrowed, **_build_candidates(proposals, incidents, min_members=3, reserved=reserved)}


def _build_candidates(proposals: Iterable[dict], incidents: dict[str, dict], *, min_members: int,
                      reserved: set[str], explicit: bool = False) -> dict[str, OutageCandidate]:
    candidates = {}
    for proposal in proposals:
        master_id, ids = proposal.get("master_id"), proposal.get("ids")
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or master_id not in ids:
            continue
        member_set = set(ids)
        member_ids = [master_id, *sorted(member_set - {master_id})]
        if len(member_ids) < min_members or not reserved.isdisjoint(member_ids):
            continue
        if any(i not in incidents or not _available(incidents[i]) for i in member_ids):
            continue
        members = [incidents[i] for i in member_ids]
        if any(member_set.intersection(inc.get("_outage_excluded_with", ())) for inc in members):
            continue
        link = {key: proposal[key] for key in ("master_id", "name", "reason", "source")}
        hashes = {inc["id"]: inc["hash"] for inc in members}
        # Фронт отправляет только ID. Изменение предложения или любой его записи меняет этот ID.
        version = json.dumps([link, hashes], sort_keys=True, ensure_ascii=False)
        outage_id = "outage-" + hashlib.sha256(version.encode("utf-8")).hexdigest()[:24]
        link["id"] = outage_id
        candidates[outage_id] = OutageCandidate(link, hashes, _summary(link, members), explicit=explicit)
    return candidates


def build_outages(incidents: dict[str, dict], seen_resolved: dict[str, datetime]) -> dict[str, Outage]:
    groups = defaultdict(list)
    for inc in incidents.values():
        if link := inc.get("outage"):
            groups[link["id"]].append(inc)

    outages = {}
    for outage_id, members in groups.items():
        members.sort(key=lambda inc: (inc["id"] != inc["outage"]["master_id"], inc["id"]))
        summary = _summary(members[0]["outage"], members)
        added = [at for inc in members for entry in inc["work_log"] if entry.get("action") == "outage_add"
                 and (entry.get("text") or "").endswith(f"({outage_id})") and (at := parse_dt(entry.get("at")))]
        if added:
            summary["last_growth_at"] = iso(max(added))
        expires_at = None
        completed_at = None
        if summary["active_count"]:
            summary["status"] = "active"
        else:
            summary["status"] = "resolved"
            expiries = (finished_until(inc, seen_resolved.get(inc["id"])) for inc in members)
            expires_at = max((at for at in expiries if at is not None), default=None)
            finishes = (parse_dt(inc.get("resolved_at")) or resolved_moment(inc, seen_resolved.get(inc["id"]))
                        for inc in members)
            completed_at = max((at for at in finishes if at is not None), default=None)
        outages[outage_id] = Outage(summary, expires_at, completed_at)
    return outages
