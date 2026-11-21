"""Карточки сбоев из явных связей, предложений ИИ и подтверждений."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .adapter import iso, parse_dt
from .diff import DONE_KINDS
from .groups import resolved_moment
from .history import History

RESOLVED_FOR = timedelta(minutes=30)


@dataclass(frozen=True)
class OutageCandidate:
    link: dict
    hashes: dict[str, str]
    summary: dict


@dataclass(frozen=True)
class Outage:
    summary: dict
    expires_at: datetime | None

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
        yield {"master_id": root, "ids": [root, *effects[root]], "source": "rules",
               "name": f"Причина и следствия · {incidents[root]['title']}",
               "reason": f"Явные ссылки causeIncidentId ведут к {root}. Связь требует подтверждения координатором."}


def build_candidates(proposals: Iterable[dict], incidents: dict[str, dict]) -> dict[str, OutageCandidate]:
    explicit = _build_candidates(_explicit_proposals(incidents), incidents, min_members=2, reserved=set())
    reserved = {inc_id for candidate in explicit.values() for inc_id in candidate.hashes}
    return {**explicit, **_build_candidates(proposals, incidents, min_members=3, reserved=reserved)}


def _build_candidates(proposals: Iterable[dict], incidents: dict[str, dict], *, min_members: int,
                      reserved: set[str]) -> dict[str, OutageCandidate]:
    candidates = {}
    for proposal in proposals:
        master_id, ids = proposal.get("master_id"), proposal.get("ids")
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids) or master_id not in ids:
            continue
        member_ids = [master_id, *sorted(set(ids) - {master_id})]
        if len(member_ids) < min_members or not reserved.isdisjoint(member_ids):
            continue
        if any(i not in incidents or not _available(incidents[i]) for i in member_ids):
            continue
        members = [incidents[i] for i in member_ids]
        link = {key: proposal[key] for key in ("master_id", "name", "reason", "source")}
        hashes = {inc["id"]: inc["hash"] for inc in members}
        # Фронт отправляет только ID. Изменение предложения или любой его записи меняет этот ID.
        version = json.dumps([link, hashes], sort_keys=True, ensure_ascii=False)
        outage_id = "outage-" + hashlib.sha256(version.encode("utf-8")).hexdigest()[:24]
        link["id"] = outage_id
        candidates[outage_id] = OutageCandidate(link, hashes, _summary(link, members))
    return candidates


def _resolved_at(inc: dict, seen_resolved: dict[str, datetime], history: History) -> datetime | None:
    if inc["state_kind"] not in DONE_KINDS:
        return None
    at = resolved_moment(inc, seen_resolved.get(inc["id"]))
    if at is not None:
        return at
    # Ручная правка могла не заполнить completionDate. После перезапуска остаётся история перехода.
    for event in reversed(history.changes(inc["id"], kind="state")):
        if event.get("before_id") not in (7, 10, 20) and event.get("after_id") in (7, 10, 20):
            return parse_dt(event.get("at"))
    return None


def build_outages(incidents: dict[str, dict], seen_resolved: dict[str, datetime], history: History) -> dict[str, Outage]:
    groups = defaultdict(list)
    for inc in incidents.values():
        if link := inc.get("outage"):
            groups[link["id"]].append(inc)

    outages = {}
    for outage_id, members in groups.items():
        members.sort(key=lambda inc: (inc["id"] != inc["outage"]["master_id"], inc["id"]))
        summary = _summary(members[0]["outage"], members)
        expires_at = None
        if summary["active_count"]:
            summary["status"] = "active"
        else:
            summary["status"] = "resolved"
            moments = (_resolved_at(inc, seen_resolved, history) for inc in members)
            last_resolved = max((at for at in moments if at is not None), default=None)
            if last_resolved is not None:
                expires_at = last_resolved + RESOLVED_FOR
        outages[outage_id] = Outage(summary, expires_at)
    return outages
