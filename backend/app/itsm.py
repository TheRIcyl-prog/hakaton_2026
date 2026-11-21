"""Мини-ITSM: действия координатора записываются прямо в JSON-файл инцидентов.

Менторы: файл — это «база инцидентов», поля можно менять и добавлять. Поэтому передача
в команду, смена статуса/приоритета/срока, комментарии, эскалации и «Взять в работу» идут в файл,
а дашборд (видеостена и все АРМ) видит их так же, как ручную правку файла жюри.

Это второе (после adapter.py) место, которое знает поля МТС, — сторона записи.
Наши поля: workLog (журнал: кто, когда, что; у правок полей ещё kind/before/after — для истории изменений)
и coordinator ({name, at} — кто из координаторов взял инцидент в работу).

Каждая запись: перечитать файл (если не менялся с прошлого чтения — берём уже разобранный из Store),
проверить конфликт (expected_hash — версия, которую видел координатор), сделать копию файла
(храним backups_keep последних), записать атомарно и сразу применить в Store без перечитывания.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .adapter import (adapt_all, content_hash, iso, norm_team, outage_excluded_with, outage_link, parse_dt, resolve_priority_id,
                      resolve_state_id, to_str, unwrap)
from .config import Rules

if TYPE_CHECKING:  # pragma: no cover
    from .store import Store

STATE_ENUM = {0: "Assigned", 2: "WorkInProgress", 7: "Resolved", 10: "Closed", 20: "Research"}
STATE_NAMES = {0: "Назначен", 2: "Выполняется", 7: "Завершен", 10: "Закрыт", 20: "Исследование ситуации"}
DONE = (7, 10, 20)
_write_lock = threading.Lock()  # без Store (тесты, скрипты)


class ItsmError(Exception):
    def __init__(self, message: str, status: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail  # для 409 «инцидент изменился»: {"changes": [...], "hash": текущий}


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:  # без перевода строк: хэш файла = хэш текста
        f.write(text)
    for _ in range(40):  # на Windows файл может быть на миг занят (опрос, редактор)
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.unlink(tmp)
    raise ItsmError("файл занят другой программой — повторите", 409)


def backup(path: Path, backup_dir: Path | None, keep: int = 20) -> None:
    """Копия файла перед записью; храним keep последних."""
    if not backup_dir or not path.exists():
        return
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_dir / f"incidents-{datetime.now():%Y%m%d-%H%M%S-%f}.json")
        old = sorted(backup_dir.glob("incidents-*.json"))
        for p in old[:-keep] if keep > 0 else []:
            p.unlink(missing_ok=True)
    except OSError:
        pass  # копия не должна мешать правке


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


class Itsm:
    def __init__(self, path: Path, rules: Rules, now_fn, store: "Store | None" = None,
                 backup_dir: Path | None = None, backups_keep: int = 20):
        self.path = path
        self.rules = rules
        self.now_fn = now_fn  # «сейчас» дашборда (в режиме выгрузки — сдвинутое время)
        self.store = store
        self.backup_dir = backup_dir
        self.backups_keep = backups_keep

    # ---------- общий цикл: прочитать → проверить → изменить запись → копия → записать → применить ----------
    def _load(self) -> list:
        data = self.store.data_for_write() if self.store else None
        if data is not None:
            return data
        try:
            data = unwrap(json.loads(self.path.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as e:
            raise ItsmError(f"файл инцидентов сейчас не читается (его редактируют?) — правка не сохранена: {e}", 409)
        return data

    def _commit(self, data: list, by: str) -> None:
        text = dumps(data)
        backup(self.path, self.backup_dir, self.backups_keep)
        _atomic_write(self.path, text)
        if self.store:
            self.store.ingest_own(data, text)
            self.store.desk.remember(by)

    def _update(self, inc_id: str, by: str, mutate, expected_hash: str | None = None) -> dict:
        if not by or not by.strip():
            raise ItsmError("укажите имя координатора")
        by = by.strip()
        lock = self.store.write_lock if self.store else _write_lock
        with lock:
            data = self._load()
            idx = next((i for i, r in enumerate(data) if isinstance(r, dict) and str(r.get("id", "")).strip() == inc_id), None)
            if idx is None:
                raise ItsmError("инцидент не найден в файле", 404)
            if expected_hash and content_hash(data[idx]) != expected_hash:
                changes = self.store.conflict_changes(inc_id, expected_hash) if self.store else []
                raise ItsmError("инцидент только что изменили — обновите карточку, правка не сохранена", 409,
                                {"conflict": True, "hash": content_hash(data[idx]), "changes": changes})
            rec = copy.deepcopy(data[idx])  # прошлую версию не трогаем: Store сравнивает старую и новую запись
            now = self.now_fn()
            action, text, extra = _unpack(mutate(rec, now))
            log = rec.get("workLog") if isinstance(rec.get("workLog"), list) else []
            log.append({"at": _fmt(now), "author": by, "action": action, "text": text, **extra})
            rec["workLog"] = log
            new_data = list(data)
            new_data[idx] = rec
            self._commit(new_data, by)
            return rec

    # ---------- «Да, это один сбой»: подтверждение группы дублей от ИИ (п. 8.4) ----------
    def confirm_duplicates(self, master_id: str, ids: list[str], by: str) -> list[dict]:
        """Одной записью: isMaster у мастера, isDouble у остальных + журнал у каждого."""
        if not by or not by.strip():
            raise ItsmError("укажите имя координатора")
        by = by.strip()
        doubles = [i for i in dict.fromkeys(ids) if i and i != master_id]
        if not master_id or not doubles:
            raise ItsmError("нужны мастер и хотя бы один дубль")
        lock = self.store.write_lock if self.store else _write_lock
        with lock:
            data = self._load()
            pos = {str(r.get("id", "")).strip(): i for i, r in enumerate(data) if isinstance(r, dict)}
            members = [master_id, *doubles]
            missing = [i for i in members if i not in pos]
            if missing:
                raise ItsmError(f"нет в файле: {', '.join(missing)}", 404)
            member_ids = set(members)
            if any((outage_excluded_with(data[pos[i]].get("outageExclusions")) & member_ids) - {i} for i in members):
                raise ItsmError("в группе есть ранее исключённая связь — обновите снимок", 409)
            now = self.now_fn()
            new_data = list(data)
            changed = []
            for inc_id in members:
                rec = copy.deepcopy(data[pos[inc_id]])
                is_master = inc_id == master_id
                rec["isMaster"], rec["isDouble"] = is_master, not is_master
                rec["isRootIncident"] = is_master
                text = (f"мастер-инцидент сбоя, дубли: {', '.join(doubles)}" if is_master
                        else f"дубль мастер-инцидента {master_id}")
                log = rec.get("workLog") if isinstance(rec.get("workLog"), list) else []
                log.append({"at": _fmt(now), "author": by, "action": "duplicates", "text": text})
                rec["workLog"] = log
                new_data[pos[inc_id]] = rec
                changed.append(rec)
            self._commit(new_data, by)
            return changed

    def confirm_outage(self, candidate_id: str, by: str) -> dict:
        by = by.strip()
        if not by:
            raise ItsmError("укажите имя координатора")
        conflict = "предложение о сбое изменилось — обновите снимок"
        if self.store is None:
            raise ItsmError(conflict, 409)
        with self.store.write_lock:
            candidate = self.store.outage_candidate(candidate_id)
            if candidate is None:
                raise ItsmError(conflict, 409)
            data = self._load()
            # Как в adapt_all: при повторе id используется последняя запись из файла.
            positions = {to_str(rec.get("id")): i for i, rec in enumerate(data) if isinstance(rec, dict)}
            for inc_id, expected_hash in candidate.hashes.items():
                idx = positions.get(inc_id)
                if idx is None:
                    raise ItsmError(conflict, 409)
                rec = data[idx]
                if outage_link(rec.get("outage")) is not None:
                    raise ItsmError("инцидент уже привязан к сбою — обновите снимок", 409)
                if content_hash(rec) != expected_hash:
                    raise ItsmError(conflict, 409)

            new_data = list(data)
            entry = {"at": _fmt(self.now_fn()), "author": by, "action": "outage_confirm",
                     "text": f"Подтверждён сбой «{candidate.link['name']}» ({candidate_id})"}
            for inc_id in candidate.hashes:
                idx = positions[inc_id]
                rec = data[idx]
                log = rec["workLog"] if isinstance(rec.get("workLog"), list) else []
                new_data[idx] = {**rec, "outage": candidate.link, "workLog": [*log, entry]}
            self._commit(new_data, by)
            outage = self.store.outage(candidate_id)
            assert outage is not None
            return outage

    def remove_outage_incident(self, outage_id: str, inc_id: str, by: str, expected_hash: str,
                               expected_incident_ids: list[str]) -> dict | None:
        by = by.strip()
        if not by:
            raise ItsmError("укажите имя координатора")
        conflict = "заявка или состав сбоя изменились — откройте сбой заново"
        if self.store is None:
            raise ItsmError(conflict, 409)
        with self.store.write_lock:
            data = self._load()
            positions = {to_str(row.get("id")): i for i, row in enumerate(data) if isinstance(row, dict)}
            members = {key: i for key, i in positions.items() if key and
                       (outage_link(data[i].get("outage")) or {}).get("id") == outage_id}
            if inc_id not in members or set(expected_incident_ids) != set(members):
                raise ItsmError(conflict, 409)
            if content_hash(data[members[inc_id]]) != expected_hash:
                raise ItsmError(conflict, 409)

            remaining = sorted(members.keys() - {inc_id})
            updated = list(data)
            now = _fmt(self.now_fn())
            dissolved = len(remaining) < 2
            removed = dict(data[members[inc_id]])
            exclusions = removed.get("outageExclusions")
            removed["outageExclusions"] = [*(exclusions if isinstance(exclusions, list) else []),
                                            {"outage_id": outage_id, "incident_ids": remaining}]
            removed.pop("outage", None)
            _log_outage_change(removed, now, by, "outage_remove", f"Исключён из сбоя {outage_id}")
            updated[members[inc_id]] = removed

            if dissolved:
                for key in remaining:
                    survivor = dict(data[members[key]])
                    survivor.pop("outage", None)
                    _log_outage_change(survivor, now, by, "outage_dissolve",
                                       f"Сбой {outage_id} распущен после исключения {inc_id}")
                    updated[members[key]] = survivor
            else:
                master = data[members[inc_id]]["outage"].get("master_id")
                if master not in remaining:
                    master = min(remaining, key=lambda key: (
                        self.rules.state_kind(resolve_state_id(data[members[key]], self.rules)) != "active", key))
                for key in remaining:
                    rec = data[members[key]]
                    updated[members[key]] = {**rec, "outage": {**rec["outage"], "master_id": master}}
            self._commit(updated, by)
            return self.store.outage(outage_id, include_incidents=True)

    def act_on_outage(self, outage_id: str, operation: str, by: str, expected_hashes: dict[str, str],
                      team: str | None = None, comment: str | None = None) -> dict:
        """Проверяет все активные записи до единственной записи в файл."""
        by, team = by.strip(), (team or "").strip()
        if not by or operation not in ("claim", "assign", "escalate"):
            raise ItsmError("укажите координатора и действие")
        if operation == "assign" and not team:
            raise ItsmError("не выбрана команда")
        if self.store is None:
            raise ItsmError("сбой недоступен", 404)
        with self.store.write_lock:
            data = self._load()
            positions = {to_str(rec.get("id")): i for i, rec in enumerate(data) if isinstance(rec, dict)}
            members = [(inc_id, i) for inc_id, i in positions.items() if inc_id and
                       (outage_link(data[i].get("outage")) or {}).get("id") == outage_id]
            if not members:
                raise ItsmError("сбой не найден", 404)
            active = [(inc_id, i) for inc_id, i in members
                      if self.rules.state_kind(resolve_state_id(data[i], self.rules)) == "active"]
            if not active or {inc_id for inc_id, _ in active} != set(expected_hashes):
                raise ItsmError("состав активных заявок изменился — откройте сбой заново", 409)
            if operation == "assign" and team not in {row.get("assignedGroupName") for row in data if isinstance(row, dict)}:
                raise ItsmError("неизвестная команда")
            for inc_id, i in active:
                current = data[i]
                if content_hash(current) != expected_hashes[inc_id]:
                    raise ItsmError("заявки сбоя изменились — откройте сбой заново", 409)
                owner = current.get("coordinator")
                if isinstance(owner, dict) and owner.get("name") not in (None, "", by):
                    raise ItsmError(f"{inc_id} уже в работе у другого координатора", 409)

            now = self.now_fn()
            updated = list(data)
            suffix = f" (сбой {outage_id})"
            changed = False
            for _, i in active:
                rec = copy.deepcopy(data[i])
                if operation == "claim":
                    owner = rec.get("coordinator")
                    if isinstance(owner, dict) and owner.get("name") == by:
                        continue
                    rec["coordinator"] = {"name": by, "at": _fmt(now)}
                    action, message, details = "take", "взял в работу", {}
                elif operation == "assign":
                    action, message, details = _assign_record(rec, now, team, None, comment)
                else:
                    action, message, details = self._escalate_record(rec, now, comment)
                log = rec.get("workLog") if isinstance(rec.get("workLog"), list) else []
                rec["workLog"] = [*log, {"at": _fmt(now), "author": by, "action": action,
                                         "text": message + suffix, **details}]
                updated[i] = rec
                changed = True
            if changed:
                self._commit(updated, by)
            return self.store.outage(outage_id, include_incidents=True) or {}

    # ---------- «Взять в работу» / «Отпустить» (наше поле coordinator) ----------
    def take(self, inc_id: str, by: str, expected_hash: str | None = None, force: bool = False) -> dict:
        def mutate(rec: dict, now: datetime):
            if _int(rec.get("stateId")) in (7, 10):
                raise ItsmError("инцидент уже решён — брать в работу нечего")
            cur = rec.get("coordinator") if isinstance(rec.get("coordinator"), dict) else None
            cur_name = (cur or {}).get("name")
            if cur_name and cur_name == by.strip():
                raise ItsmError(f"инцидент уже в работе у вас", 409)
            if cur_name and not force:
                raise ItsmError(f"инцидент уже в работе у {cur_name}", 409, {"coordinator": cur_name})
            rec["coordinator"] = {"name": by.strip(), "at": _fmt(now)}
            return "take", "взял в работу" + (f" (был у {cur_name})" if cur_name else "")

        return self._update(inc_id, by, mutate, expected_hash)

    def release(self, inc_id: str, by: str, expected_hash: str | None = None) -> dict:
        def mutate(rec: dict, now: datetime):
            cur = rec.get("coordinator") if isinstance(rec.get("coordinator"), dict) else None
            if not cur:
                raise ItsmError("инцидент никто не брал в работу", 409)
            rec.pop("coordinator", None)
            who = cur.get("name") or "—"
            return "release", "отпустил" if who == by.strip() else f"снял с {who}"

        return self._update(inc_id, by, mutate, expected_hash)

    # ---------- действия ----------
    def assign(self, inc_id: str, team: str, by: str, assignee: str | None = None, comment: str | None = None,
               expected_hash: str | None = None) -> dict:
        team = (team or "").strip()
        if not team:
            raise ItsmError("не выбрана команда")

        def mutate(rec: dict, now: datetime):
            sid = _int(rec.get("stateId"))
            if sid == 10:
                raise ItsmError("инцидент закрыт — переназначить нельзя, заведите новый")
            if sid == 7:
                raise ItsmError("инцидент завершён — сначала верните его в работу")
            return _assign_record(rec, now, team, assignee, comment)

        return self._update(inc_id, by, mutate, expected_hash)

    def set_state(self, inc_id: str, state_id: int, by: str, reason: str | None = None, comment: str | None = None,
                  expected_hash: str | None = None) -> dict:
        if state_id not in self.rules.states:
            raise ItsmError("неизвестный статус")

        def mutate(rec: dict, now: datetime):
            cur = _int(rec.get("stateId"))
            allowed = self.rules.transitions.get(cur, [])
            if state_id not in allowed:
                names = ", ".join(self.rules.states[s]["name"] for s in allowed) or "никуда (финальный статус)"
                raise ItsmError(f"из «{self.rules.states.get(cur, {}).get('name', cur)}» можно перевести только в: {names}")
            before = rec.get("state")
            _set_state(rec, state_id)
            if state_id == 7:
                rec["completionDate"] = _fmt(now)  # время перевода в «Завершен» (ответ менторов)
            elif state_id in (0, 2):
                rec["completionDate"] = None  # вернули в работу
            if state_id in DONE:
                rec["stateReason"] = (reason or "").strip() or rec.get("stateReason") or "Постоянное решение"
            else:
                rec["stateReason"] = "Нет"
            kind = "reopen" if cur in (7, 20) and state_id in (0, 2) else "state"
            return ("state", f"статус → {self.rules.states[state_id]['name']}" + (f". {comment.strip()}" if comment and comment.strip() else ""),
                    {"kind": kind, "before": before, "after": rec["state"], "before_id": cur, "after_id": state_id})

        return self._update(inc_id, by, mutate, expected_hash)

    def set_priority(self, inc_id: str, priority_id: int, by: str, recalc_deadline: bool = False, comment: str | None = None,
                     expected_hash: str | None = None) -> dict:
        if priority_id not in self.rules.priorities:
            raise ItsmError("неизвестный приоритет")

        def mutate(rec: dict, now: datetime):
            old, old_id = rec.get("priority"), _int(rec.get("priorityId"))
            rec["priorityId"] = priority_id
            rec["priority"] = self.rules.priorities[priority_id]["name"]
            extra = ""
            if recalc_deadline and priority_id in self.rules.sla_hours:
                start = parse_dt(rec.get("submitDate")) or now
                new_dl = start + timedelta(hours=self.rules.sla_hours[priority_id])
                rec["deadline"] = _fmt(new_dl)
                extra = f", срок пересчитан по SLA ({self.rules.sla_hours[priority_id]:g} ч)"
            return ("priority", f"приоритет: {old or '—'} → {rec['priority']}{extra}" + (f". {comment.strip()}" if comment and comment.strip() else ""),
                    {"kind": "priority", "before": old, "after": rec["priority"], "before_id": old_id, "after_id": priority_id,
                     "up": bool(old_id is not None and priority_id > old_id)})

        return self._update(inc_id, by, mutate, expected_hash)

    def set_deadline(self, inc_id: str, deadline: str, by: str, comment: str | None = None,
                     expected_hash: str | None = None) -> dict:
        dl = parse_dt(deadline)
        if not dl:
            raise ItsmError("не понимаю дату срока")

        def mutate(rec: dict, now: datetime):
            before = parse_dt(rec.get("deadline"))
            rec["deadline"] = _fmt(dl)
            return ("deadline", f"срок → {iso(dl)}" + (f". {comment.strip()}" if comment and comment.strip() else ""),
                    {"kind": "deadline", "before": iso(before), "after": iso(dl), "later": bool(before and dl > before),
                     "shift_min": round((dl - before).total_seconds() / 60) if before else None})

        return self._update(inc_id, by, mutate, expected_hash)

    def comment(self, inc_id: str, text: str, by: str) -> dict:
        if not text or not text.strip():
            raise ItsmError("пустой комментарий")
        return self._update(inc_id, by, lambda rec, now: ("comment", text.strip()[:1000]))

    def escalate(self, inc_id: str, by: str, comment: str | None = None, expected_hash: str | None = None) -> dict:
        def mutate(rec: dict, now: datetime):
            if _int(rec.get("stateId")) in DONE:
                raise ItsmError("инцидент уже решён — эскалация не нужна")
            return self._escalate_record(rec, now, comment)

        return self._update(inc_id, by, mutate, expected_hash)

    def _escalate_record(self, rec: dict, now: datetime, comment: str | None) -> tuple:
        team = to_str(rec.get("assignedGroupName"))
        scope = {"team": norm_team(team), "deadline": iso(parse_dt(rec.get("deadline"))),
                 "priority_id": resolve_priority_id(rec, self.rules)}
        text = (comment or "").strip()[:1000] or f"эскалирован руководителю группы «{team or '—'}»"
        return "escalate", text, {"at": iso(now), "escalation_scope": scope}

    # ---------- импорт / экспорт ----------
    def import_file(self, text: str, backup_dir: Path) -> int:
        try:
            data = json.loads(text)
            incs, _ = adapt_all(data, self.rules, "{id}")
        except (ValueError, TypeError, AttributeError) as e:
            raise ItsmError(f"это не файл инцидентов: {e}")
        if not incs:
            raise ItsmError("в файле нет ни одного инцидента с id")
        lock = self.store.write_lock if self.store else _write_lock
        with lock:
            backup(self.path, backup_dir, self.backups_keep)
            _atomic_write(self.path, dumps(data))
        return len(incs)


def _log_outage_change(rec: dict, at: str, by: str, action: str, text: str) -> None:
    log = rec.get("workLog")
    rec["workLog"] = [*(log if isinstance(log, list) else []),
                      {"at": at, "author": by, "action": action, "text": text}]


def _unpack(res) -> tuple[str, str, dict]:
    if len(res) == 3:
        return res[0], res[1], res[2]
    return res[0], res[1], {}


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _set_state(rec: dict, state_id: int) -> None:
    rec["stateId"] = state_id
    rec["state"] = STATE_NAMES[state_id]
    rec["stateEnum"] = STATE_ENUM[state_id]


def _assign_record(rec: dict, now: datetime, team: str, assignee: str | None, comment: str | None) -> tuple:
    old = rec.get("assignedGroupName")
    name = (assignee or "").strip()
    rec.update(assignedGroupName=team, assignedEmployee=name or None, assignedEmployeeLogin=None,
               lastAssignmentToTeamDateTime=_fmt(now))
    if _int(rec.get("stateId")) == 2:
        _set_state(rec, 0)
    coord = rec.pop("coordinator", None)
    text = f"передан: {old or '—'} → {team}" + (f" ({name})" if name else "")
    if isinstance(coord, dict) and coord.get("name"):
        text += f"; снят с координатора {coord['name']}"
    if comment and comment.strip():
        text += f". {comment.strip()}"
    return "assign", text, {"kind": "team", "before": old, "after": team}
