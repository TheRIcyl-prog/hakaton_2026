"""Чтение файла, инкрементальный пересчёт и снимок для экранов.

Рассчитано на тысячи инцидентов (decisions.md, п. 0):
- опрос: сначала mtime+size (дёшево), целиком читаем только при изменении; хэш содержимого отсекает
  «тронули, но не поменяли»; чужие правки склеиваем — применяем не чаще раза в settings.coalesce_seconds (0,5 с);
- разбираем только изменённые записи (сравнение с прошлой сырой записью), события и история — только по ним;
- справочники (медианы сроков) — один проход на правку (refs.Refs);
- свои записи (АРМ/ITSM) применяются в памяти сразу, без перечитывания файла (ingest_own);
- снимок лёгкий: только инциденты из групп (+ взятые в работу и из очереди действий), остальное —
  серверный поиск search(); сериализация и gzip — один раз на версию для всех экранов.
Битый/пустой/недоступный файл → остаётся последний целый снимок, а в file.status пишется причина.
"""
from __future__ import annotations

import gzip
import hashlib
import inspect
import json
import os
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from . import adapter
from . import ai as ai_layer
from .actions import build_actions
from .ai import enrich
from .clock import Clock
from .config import Rules, Settings
from .desk import ALERT_GROUPS, DeskState
from .diff import DONE_KINDS, diff
from .forecast import forecast_all
from .groups import assign_groups
from .history import History
from .labels import apply_labels
from .outages import Outage, OutageCandidate, build_candidates, build_outages
from .queue import build_queue
from .queue_preferences import QueuePreferences
from .recurrences import apply_recurrences
from .refs import Refs

AI_REFRESH_SECONDS = 30   # риск и прогноз зависят от времени — пересчитываем и без правок
REGROUP_SECONDS = 1.0     # решённый критический уходит из группы через 30 мин — проверяем раз в секунду
STAT_TRUST_SECONDS = 2.0  # файл старше — верим mtime+size без чтения
OLD_VERSIONS = 3000       # прошлые версии инцидентов по hash — чтобы объяснить конфликт «инцидент изменился»
_ENRICH_TAKES_REFS = "refs" in inspect.signature(enrich).parameters

# Поля, которые показываем в конфликте «инцидент только что изменили»
CONFLICT_FIELDS = [
    ("priority", "приоритет"), ("state", "статус"), ("team", "команда"), ("assignee", "сотрудник"),
    ("deadline", "срок"), ("impact", "влияние"), ("urgency", "срочность"), ("title", "заголовок"),
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, settings: Settings, rules: Rules, clock: Clock | None = None, desk: DeskState | None = None,
                 history: History | None = None, queue_preferences: QueuePreferences | None = None):
        self.settings = settings
        self.rules = rules
        self.clock = clock or Clock(settings.time_mode, adapter.parse_dt(settings.export_now))
        self.lock = threading.RLock()          # состояние снимка
        self.write_lock = threading.Lock()     # чтение/запись файла: опрос и действия АРМ не пересекаются
        self.desk = desk or DeskState(None)
        self.desk.now_fn = self.clock.now
        self.history = history or History(None)
        self.queue_preferences = queue_preferences or QueuePreferences()

        self.incidents: dict[str, dict] = {}
        self.raw_by_id: dict[str, dict] = {}
        self.groups: dict[str, list[str]] = {g.key: [] for g in rules.groups}
        self.seen_resolved: dict[str, datetime] = {}
        self.events: deque[dict] = deque(maxlen=300)
        self.actions: list[dict] = []
        self._outage_candidates: dict[str, OutageCandidate] = {}
        self._outages: dict[str, Outage] = {}
        self._visible_outages: list[dict] = []
        self.refs: Refs | None = None
        self.seq = 0
        self.version = 0
        self.loaded = False
        self.last_apply_seconds = 0.0          # сколько занял последний пересчёт (для замеров)

        self._data: list | None = None         # разобранный файл (список сырых записей)
        self._bytes_hash: str | None = None
        self._stat: tuple[int, int] | None = None
        self._last_foreign = 0.0
        self._last_regroup: datetime | None = None   # по часам дашборда
        self._last_ai = 0.0
        self._old_versions: OrderedDict[str, dict] = OrderedDict()
        self._search: dict[str, tuple[str, str]] = {}   # id -> (hash, строка для поиска)
        self._snap_cache: dict[str, Any] = {}
        self.snapshot_extra: dict[str, Any] = {}   # boot_id и т.п. — дописывается в каждый снимок
        self.file: dict[str, Any] = {
            "path": str(settings.incidents_file),
            "status": "missing",  # ok | corrupt | missing
            "error": None,
            "last_ok_at": None,  # реальное время последнего удачного чтения
            "last_change_at": None,
            "warnings": [],
            "count": 0,
        }

    # ---------- чтение ----------
    def poll(self, force: bool = False) -> bool:
        """Один шаг опроса. True — если снимок изменился (version вырос)."""
        if not self.write_lock.acquire(blocking=False):
            return False  # идёт запись из АРМ/ITSM — она сама применит результат
        try:
            return self._poll(force)
        finally:
            self.write_lock.release()

    def _poll(self, force: bool) -> bool:
        path = self.settings.incidents_file
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self._set_file_problem("missing", "файл не найден")
            return self._regroup_if_needed(force=False)
        except OSError:
            return False
        stat = (st.st_mtime_ns, st.st_size)
        # mtime на Windows грубый: две быстрые правки одного размера дают тот же stat. Поэтому stat доверяем,
        # только если файл не трогали STAT_TRUST_SECONDS; свежий файл сверяем по хэшу (10 мс на 5 000).
        fresh = time.time_ns() - st.st_mtime_ns < STAT_TRUST_SECONDS * 1e9
        if stat == self._stat and not force and not fresh:
            return self._regroup_if_needed(force=False)
        if not force and self.loaded and time.monotonic() - self._last_foreign < self.settings.coalesce_seconds:
            return self._regroup_if_needed(force=False)  # частые правки склеиваем: прочитаем на следующем шаге
        try:
            data_bytes = path.read_bytes()
        except FileNotFoundError:
            self._set_file_problem("missing", "файл не найден")
            return False
        except OSError:
            return False  # файл занят редактором — попробуем на следующем шаге

        h = hashlib.sha256(data_bytes).hexdigest()
        if h == self._bytes_hash:
            self._stat = stat
            if self.file["status"] != "ok":
                with self.lock:
                    self.file.update(status="ok", error=None)
                    self.version += 1
                return True
            return self._regroup_if_needed(force=False)
        try:
            text = data_bytes.decode("utf-8-sig")
            if not text.strip():
                raise ValueError("файл пустой")
            data = adapter.unwrap(json.loads(text))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
            # Не запоминаем stat и хэш: как только файл дозапишут, прочитаем снова
            self._set_file_problem("corrupt", str(e))
            return False
        self._last_foreign = time.monotonic()
        return self._ingest(data, h, stat)

    def _ingest(self, data: list, h: str, stat: tuple[int, int] | None) -> bool:
        t0 = time.perf_counter()
        prev = {i: (self.raw_by_id[i], inc) for i, inc in self.incidents.items() if i in self.raw_by_id}
        try:
            new_incidents, warnings = adapter.adapt_all(data, self.rules, self.settings.itsm_url_template, prev)
        except ValueError as e:
            self._set_file_problem("corrupt", str(e))
            return False
        with self.lock:
            self._bytes_hash, self._stat, self._data = h, stat, data
            if self.loaded and self._is_replaced(new_incidents):
                # Файл подменили целиком (другая выгрузка/демо-файл): новая точка отсчёта, без лавины событий
                self.clock.reanchor(adapter.export_moment(data))
                self.seen_resolved.clear()
                self.history.clear()
                self.loaded = False
            self.clock.set_anchor_if_empty(adapter.export_moment(data))
            self.raw_by_id = {str(r.get("id")).strip(): r for r in data if isinstance(r, dict) and r.get("id")}
            self.file.update(
                status="ok",
                error=None,
                last_ok_at=adapter.iso(_utcnow()),
                last_change_at=adapter.iso(_utcnow()),
                warnings=warnings[:20],
                count=len(new_incidents),
            )
            self._apply(new_incidents)
        self.last_apply_seconds = time.perf_counter() - t0
        return True

    def _is_replaced(self, new_incidents: dict[str, dict]) -> bool:
        old_ids, new_ids = set(self.incidents), set(new_incidents)
        if not old_ids or not new_ids:
            return False
        return len(old_ids & new_ids) / max(len(old_ids), len(new_ids)) < 0.5

    def _set_file_problem(self, status: str, error: str) -> None:
        with self.lock:
            if self.file["status"] != status or self.file["error"] != error:
                self.file.update(status=status, error=error)
                self.version += 1

    # ---------- применение ----------
    def _apply(self, new_incidents: dict[str, dict]) -> None:
        now = self.clock.now()
        old = self.incidents
        changed = {i for i, inc in new_incidents.items() if old.get(i) is not inc}
        removed = set(old) - set(new_incidents)
        # Запоминаем, когда увидели переход в «решён» — если в файле нет completionDate
        for inc_id in changed:
            inc, prev = new_incidents[inc_id], old.get(inc_id)
            if inc["state_kind"] in DONE_KINDS and prev is not None and prev["state_kind"] not in DONE_KINDS:
                self.seen_resolved[inc_id] = now
            elif inc["state_kind"] == "active":
                self.seen_resolved.pop(inc_id, None)
            if prev is not None:
                self._old_versions[prev["hash"]] = prev
        for inc_id in removed:
            self.seen_resolved.pop(inc_id, None)
        while len(self._old_versions) > OLD_VERSIONS:
            self._old_versions.popitem(last=False)

        # История — до пересчёта ИИ: динамика (п. 8.5) должна видеть эту же правку, а не следующую
        self.history.record(old if self.loaded else {}, new_incidents, changed, now)  # при первой загрузке — засев из workLog
        self.groups = assign_groups(new_incidents, self.rules, now, self.seen_resolved)
        self.refs = Refs(new_incidents, now, changed, removed, self.history)
        apply_recurrences(new_incidents, self.rules.coordinators_team)
        self._derive(new_incidents, now)
        self._outages = build_outages(new_incidents, self.seen_resolved, self.history)
        self._visible_outages = self._outages_in_snapshot(now)

        if self.loaded:
            stamp = adapter.iso(_utcnow())
            clock_stamp = adapter.iso(now)
            for ev in diff(old, new_incidents, changed | removed):
                self.seq += 1
                ev.update(seq=self.seq, at=stamp, clock_at=clock_stamp)
                self.events.append(ev)
        self.incidents = new_incidents
        self.loaded = True
        self.desk.sync_alerts(new_incidents)
        self._last_regroup = now
        self.version += 1

    def _derive(self, incidents: dict[str, dict], now: datetime) -> None:
        """Производные поля: прогноз срока, ИИ, очередь действий."""
        refs = self.refs or Refs(incidents, now, history=self.history)
        refs.now = now
        forecast_all(incidents, now, refs)
        self._enrich(incidents, now)
        self.actions = build_actions(incidents, now)
        apply_labels(incidents, self.history, now)
        self._last_ai = time.monotonic()

    def _enrich(self, incidents: dict[str, dict], now: datetime) -> None:
        refs = self.refs or Refs(incidents, now, history=self.history)
        refs.now = now
        if _ENRICH_TAKES_REFS:
            enrich(incidents, now, self.rules, refs=refs)
        else:
            enrich(incidents, now, self.rules)
        self._outage_candidates = build_candidates(ai_layer.outage_candidates(), incidents)

    def _outages_in_snapshot(self, now: datetime) -> list[dict]:
        return [outage.summary for outage in self._outages.values() if outage.visible(now)]

    def _regroup_if_needed(self, force: bool) -> bool:
        """Время идёт: решённые критические уходят из группы, риск растёт. Пересчитываем без перечитывания."""
        if not self.loaded:
            return False
        now = self.clock.now()
        ai_due = force or time.monotonic() - self._last_ai > AI_REFRESH_SECONDS
        ai_dirty = not ai_due and ai_layer.consume_dirty()   # GigaChat дописал ответы — нужен только ИИ
        if not (ai_due or ai_dirty) and self._last_regroup and abs((now - self._last_regroup).total_seconds()) < REGROUP_SECONDS:
            return False
        with self.lock:
            self._last_regroup = now
            groups = assign_groups(self.incidents, self.rules, now, self.seen_resolved)
            changed = groups != self.groups
            self.groups = groups
            if changed:
                self.desk.sync_alerts(self.incidents)
            if ai_due:
                self._derive(self.incidents, now)
                changed = True
            elif ai_dirty:
                self._enrich(self.incidents, now)
                self.actions = build_actions(self.incidents, now)  # подсказки «кому передать» в очереди
                changed = True
            visible_outages = self._outages_in_snapshot(now)
            if visible_outages != self._visible_outages:
                self._visible_outages = visible_outages
                changed = True
            if changed:
                self.version += 1
        return changed

    # ---------- свои записи (АРМ / ITSM) ----------
    def data_for_write(self) -> list | None:
        """Текущее содержимое файла для правки без повторного разбора: если файл не менялся с прошлого чтения.
        Вызывать под write_lock. None — файл изменился/не читался, его надо разобрать заново."""
        path = self.settings.incidents_file
        try:
            data_bytes = path.read_bytes()
        except OSError:
            return None
        if self._data is not None and hashlib.sha256(data_bytes).hexdigest() == self._bytes_hash:
            return self._data
        return None

    def ingest_own(self, data: list, text: str) -> None:
        """Файл только что записан нами (под write_lock): применить в памяти, не перечитывая."""
        path = self.settings.incidents_file
        try:
            st = os.stat(path)
            stat = (st.st_mtime_ns, st.st_size)
        except OSError:
            stat = None
        self._ingest(data, hashlib.sha256(text.encode("utf-8")).hexdigest(), stat)

    def conflict_changes(self, inc_id: str, expected_hash: str) -> list[dict]:
        """Что изменилось в инциденте с версии expected_hash (её видел координатор) до текущей."""
        with self.lock:
            cur = self.incidents.get(inc_id)
            old = self._old_versions.get(expected_hash)
        if not cur:
            return []
        if not old:
            return [{"field": "unknown", "label": "запись", "before": None, "after": None,
                     "text": "инцидент изменили (подробности недоступны)"}]
        out = []
        for key, label in CONFLICT_FIELDS:
            if old.get(key) != cur.get(key):
                out.append({"field": key, "label": label, "before": old.get(key), "after": cur.get(key),
                            "text": f"{label}: {old.get(key) or '—'} → {cur.get(key) or '—'}"})
        oc, cc = (old.get("coordinator") or {}).get("name"), (cur.get("coordinator") or {}).get("name")
        if oc != cc:
            out.append({"field": "coordinator", "label": "в работе у", "before": oc, "after": cc,
                        "text": f"в работе у: {oc or '—'} → {cc or '—'}"})
        new_log = (cur.get("work_log") or [])[len(old.get("work_log") or []):]
        for e in new_log:
            if e.get("action") in ("comment", "escalate"):
                out.append({"field": "work_log", "label": "журнал", "before": None, "after": e.get("text"),
                            "text": f"{e.get('author') or '—'}: {e.get('text') or ''}"})
        if not out:
            out.append({"field": "other", "label": "запись", "before": None, "after": None,
                        "text": "изменены другие поля записи"})
        return out

    # ---------- выдача ----------
    def _visible_ids(self) -> list[str]:
        """Что уходит в снимок: инциденты из групп + взятые в работу (вкладка «Мои») + из очереди действий."""
        seen: dict[str, None] = {}
        for g in self.rules.groups:
            for i in self.groups.get(g.key, []):
                seen[i] = None
        for i, inc in self.incidents.items():
            if inc.get("coordinator") and inc["state_kind"] == "active":
                seen[i] = None
        for a in self.actions:
            seen[a["incident_id"]] = None
        return [i for i in seen if i in self.incidents]

    def _build_snapshot(self, events_limit: int) -> dict:
        now = self.clock.now()
        groups = [
            {"key": g.key, "title": g.title, "ids": list(self.groups.get(g.key, [])), "count": len(self.groups.get(g.key, []))}
            for g in self.rules.groups
        ]
        visible = self._visible_ids()
        active_total = sum(1 for inc in self.incidents.values() if inc["state_kind"] == "active")
        active_in_groups = sum(1 for inc in self.incidents.values()
                               if inc["state_kind"] == "active" and set(inc.get("groups", ())) - set(inc.get("resolved_in", ())))
        return {
            "version": self.version,
            "server_time": adapter.iso(_utcnow()),
            "now": adapter.iso(now),
            "clock_offset_ms": int(self.clock.offset_seconds() * 1000),
            "time_mode": self.clock.mode,
            "export_anchor": adapter.iso(self.clock.anchor),
            "file": dict(self.file),
            "groups": groups,
            "counts": {g["key"]: sum(1 for i in g["ids"] if g["key"] not in self.incidents[i]["resolved_in"]) for g in groups},
            "incidents_scope": "visible",          # в incidents только видимые; остальные — через /api/search
            "incidents": {i: _public(self.incidents[i]) for i in visible},
            "total_incidents": len(self.incidents),
            "active_total": active_total,
            "outside_active": active_total - active_in_groups,   # «ещё N активных вне групп»
            "outage_candidates": [candidate.summary for candidate in self._outage_candidates.values()],
            "outages": self._visible_outages,
            "actions": self.actions,
            "ai_status": ai_layer.status(),
            **self.desk.export(self.incidents),    # alerts, claims, coordinators — общее состояние смены
            "teams": self._teams(),
            "products": self._products(),
            "events": list(self.events)[-events_limit:],
            "last_event_seq": self.seq,
        }

    def snapshot(self, events_limit: int = 50) -> dict:
        with self.lock:
            return {**self._build_snapshot(events_limit), **self.snapshot_extra}

    def snapshot_bytes(self, gzip_ok: bool) -> tuple[bytes, int]:
        """Снимок, сериализованный один раз на версию (для всех экранов). → (тело, version)."""
        with self.lock:
            c = self._snap_cache
            if c.get("version") != self.version:
                body = json.dumps({**self._build_snapshot(50), **self.snapshot_extra}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                c.clear()
                c.update(version=self.version, raw=body)
            if gzip_ok:
                if "gz" not in c:
                    c["gz"] = gzip.compress(c["raw"], compresslevel=5)
                return c["gz"], c["version"]
            return c["raw"], c["version"]

    def _teams(self) -> list[str]:
        teams = {i["team"] for i in self.incidents.values() if i.get("team") and i["team"] != "AutoClosedMi"}
        teams.add(self.rules.coordinators_team)
        return sorted(teams, key=str.casefold)

    def _products(self) -> list[str]:
        return sorted({i["product"] for i in self.incidents.values() if i.get("product")}, key=str.casefold)

    # ---------- поиск и фильтры по ВСЕМ инцидентам ----------
    def _blob(self, inc: dict) -> str:
        cached = self._search.get(inc["id"])
        if cached and cached[0] == inc["hash"]:
            return cached[1]
        coord = (inc.get("coordinator") or {}).get("name")
        blob = " ".join(filter(None, [inc["id"], inc.get("product"), inc.get("team"), inc.get("assignee"), coord,
                                      inc.get("title"), inc.get("short_description"), inc.get("description"),
                                      inc.get("service"), inc.get("scenario")])).casefold()
        self._search[inc["id"]] = (inc["hash"], blob)
        return blob

    def search(self, q: str = "", limit: int = 100, offset: int = 0, **f: Any) -> dict:
        """Поиск по номеру/продукту/команде/сотруднику/тексту + фильтры decisions.md, п. 6.
        Фильтры: status (active|done|all), product, team, priority (список id), coordinator (в работе у),
        overdue, burning (прогноз «не успеют» или просрочен), untaken (тревога не взята), unassigned,
        risk_high, duplicates, service_type (client|internal), new_hour, group."""
        words = [w for w in (q or "").casefold().split() if w]
        with self.lock:
            now = self.clock.now()
            hour_ago = now - timedelta(hours=1)
            products = _as_set(f.get("product"))
            teams = _as_set(f.get("team"))
            prios = {int(p) for p in _as_set(f.get("priority")) if str(p).lstrip("-").isdigit()}
            coordinator = (f.get("coordinator") or "").strip().casefold()
            status = f.get("status") or "all"
            group = f.get("group")
            matched: list[dict] = []
            for inc in self.incidents.values():
                kind = inc["state_kind"]
                if status == "active" and kind != "active":
                    continue
                if status == "done" and kind == "active":
                    continue
                if products and inc.get("product") not in products:
                    continue
                if teams and inc.get("team") not in teams:
                    continue
                if prios and inc.get("priority_id") not in prios:
                    continue
                if group and group not in inc.get("groups", ()):
                    continue
                if coordinator and ((inc.get("coordinator") or {}).get("name") or "").casefold() != coordinator:
                    continue
                fc = inc.get("forecast") or {}
                if f.get("overdue") and fc.get("level") != "overdue":
                    continue
                if f.get("burning") and fc.get("level") not in ("breach", "overdue"):
                    continue
                if f.get("untaken") and (inc.get("coordinator") or not any(
                        g in inc.get("groups", ()) and g not in inc.get("resolved_in", ()) for g in ALERT_GROUPS)):
                    continue
                if f.get("unassigned") and (inc.get("assignee") or kind != "active"):
                    continue
                ai = inc.get("ai") or {}
                if f.get("risk_high") and (ai.get("risk") or {}).get("level") != "high":
                    continue
                if f.get("duplicates") and not (inc.get("is_master") or inc.get("is_double") or ai.get("duplicates")):
                    continue
                st = f.get("service_type")
                if st == "client" and inc.get("service_type") != "Клиентский":
                    continue
                if st == "internal" and inc.get("service_type") == "Клиентский":
                    continue
                if f.get("new_hour"):
                    c = adapter.parse_dt(inc.get("created_at"))
                    if not c or c < hour_ago:
                        continue
                if words:
                    blob = self._blob(inc)
                    if not all(w in blob for w in words):
                        continue
                matched.append(inc)
            exact = (q or "").strip().casefold()
            matched.sort(key=lambda inc: _search_key(inc, now, exact))
            page = [_public(i) for i in matched[offset:offset + max(1, min(limit, 500))]]
            return {"total": len(matched), "items": page, "offset": offset, "limit": limit, "now": adapter.iso(now)}

    # ---------- одиночные запросы ----------
    def queue(self, by: str = "") -> list[dict]:
        by = by.strip().casefold()
        hidden = self.queue_preferences.hidden(by)
        with self.lock:
            items = build_queue(self.incidents, self.actions, self._outage_candidates.values(), self.history,
                                by, self.clock.now())
        return [item for item in items if item["id"] not in hidden]

    def events_after(self, seq: int) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["seq"] > seq]

    def incident(self, inc_id: str) -> dict | None:
        with self.lock:
            inc = self.incidents.get(inc_id)
            if inc is None:
                return None
            return {**_public(inc), "raw": self.raw_by_id.get(inc_id), "history": self.history.get(inc_id)}

    def outage_candidate(self, candidate_id: str) -> OutageCandidate | None:
        with self.lock:
            return self._outage_candidates.get(candidate_id)

    def outage(self, outage_id: str, *, include_incidents: bool = False) -> dict | None:
        with self.lock:
            outage = self._outages.get(outage_id)
            if outage is None:
                return None
            summary = outage.summary
            if include_incidents:
                return {**summary, "incidents": [_public(self.incidents[i]) for i in summary["incident_ids"]]}
            return summary


def _public(inc: dict) -> dict:
    """Убрать служебные поля перед выдачей."""
    if "_escalation" in inc or "_outage_excluded_with" in inc:
        inc = {key: value for key, value in inc.items() if key not in ("_escalation", "_outage_excluded_with")}
    ai = inc.get("ai")
    route = ai.get("route") if isinstance(ai, dict) else None
    if isinstance(route, dict) and any(k.startswith("_") for k in route):
        return {**inc, "ai": {**ai, "route": {k: v for k, v in route.items() if not k.startswith("_")}}}
    return inc


def _as_set(value: Any) -> set:
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = str(value).split(",")
    return {str(v).strip() for v in items if str(v).strip()}


def _search_key(inc: dict, now: datetime, exact: str) -> tuple:
    """Точное совпадение номера — первым; активные выше решённых; выше приоритет; меньше осталось времени."""
    dl = adapter.parse_dt(inc.get("deadline"))
    rem = (dl - now).total_seconds() if dl else float("inf")
    return (
        0 if exact and inc["id"].casefold() == exact else 1,
        0 if inc["state_kind"] == "active" else 1,
        -(inc.get("priority_id") or 0),
        rem,
        -(adapter.parse_dt(inc.get("created_at")) or now).timestamp(),
    )
