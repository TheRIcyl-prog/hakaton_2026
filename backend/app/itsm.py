"""Мини-ITSM: действия координатора записываются прямо в JSON-файл инцидентов.

Менторы: файл — это «база инцидентов», поля можно менять и добавлять. Поэтому передача
в команду, смена статуса/приоритета/срока, комментарии и эскалации идут в файл, а дашборд
(видеостена и все АРМ) видит их так же, как ручную правку файла жюри.

Это второе (после adapter.py) место, которое знает поля МТС, — сторона записи.
Каждое действие добавляет запись в наше поле workLog: кто, когда, что сделал.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .adapter import adapt_all, iso, parse_dt
from .config import Rules

STATE_ENUM = {0: "Assigned", 2: "WorkInProgress", 7: "Resolved", 10: "Closed", 20: "Research"}
DONE = (7, 10, 20)
_write_lock = threading.Lock()


class ItsmError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    for _ in range(40):  # на Windows файл может быть на миг занят (опрос, редактор)
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.unlink(tmp)
    raise ItsmError("файл занят другой программой — повторите", 409)


class Itsm:
    def __init__(self, path: Path, rules: Rules, now_fn):
        self.path = path
        self.rules = rules
        self.now_fn = now_fn  # «сейчас» дашборда (в режиме выгрузки — сдвинутое время)

    # ---------- общий цикл: прочитать → изменить запись → записать ----------
    def _update(self, inc_id: str, by: str, mutate) -> dict:
        if not by or not by.strip():
            raise ItsmError("укажите имя координатора")
        with _write_lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as e:
                raise ItsmError(f"файл инцидентов сейчас не читается (его редактируют?) — правка не сохранена: {e}", 409)
            if not isinstance(data, list):
                raise ItsmError("файл инцидентов не является массивом", 409)
            rec = next((r for r in data if isinstance(r, dict) and str(r.get("id", "")).strip() == inc_id), None)
            if rec is None:
                raise ItsmError("инцидент не найден в файле", 404)
            now = self.now_fn()
            text = mutate(rec, now)
            log = rec.get("workLog") if isinstance(rec.get("workLog"), list) else []
            log.append({"at": _fmt(now), "author": by.strip(), "action": text[0], "text": text[1]})
            rec["workLog"] = log
            _atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2))
            return rec

    # ---------- действия ----------
    def assign(self, inc_id: str, team: str, by: str, assignee: str | None = None, comment: str | None = None) -> dict:
        team = (team or "").strip()
        if not team:
            raise ItsmError("не выбрана команда")

        def mutate(rec: dict, now: datetime):
            sid = _int(rec.get("stateId"))
            if sid == 10:
                raise ItsmError("инцидент закрыт — переназначить нельзя, заведите новый")
            if sid == 7:
                raise ItsmError("инцидент завершён — сначала верните его в работу")
            old = rec.get("assignedGroupName")
            rec["assignedGroupName"] = team
            name = (assignee or "").strip()
            rec["assignedEmployee"] = name or None
            rec["assignedEmployeeLogin"] = None
            rec["lastAssignmentToTeamDateTime"] = _fmt(now)
            if sid == 2:  # новая команда ещё не взяла в работу
                _set_state(rec, 0)
            return "assign", f"передан: {old or '—'} → {team}" + (f" ({name})" if name else "") + (f". {comment.strip()}" if comment and comment.strip() else "")

        return self._update(inc_id, by, mutate)

    def set_state(self, inc_id: str, state_id: int, by: str, reason: str | None = None, comment: str | None = None) -> dict:
        if state_id not in self.rules.states:
            raise ItsmError("неизвестный статус")

        def mutate(rec: dict, now: datetime):
            cur = _int(rec.get("stateId"))
            allowed = self.rules.transitions.get(cur, [])
            if state_id not in allowed:
                names = ", ".join(self.rules.states[s]["name"] for s in allowed) or "никуда (финальный статус)"
                raise ItsmError(f"из «{self.rules.states.get(cur, {}).get('name', cur)}» можно перевести только в: {names}")
            _set_state(rec, state_id)
            if state_id == 7:
                rec["completionDate"] = _fmt(now)  # время перевода в «Завершен» (ответ менторов)
            elif state_id in (0, 2):
                rec["completionDate"] = None  # вернули в работу
            if state_id in DONE:
                rec["stateReason"] = (reason or "").strip() or rec.get("stateReason") or "Постоянное решение"
            else:
                rec["stateReason"] = "Нет"
            return "state", f"статус → {self.rules.states[state_id]['name']}" + (f". {comment.strip()}" if comment and comment.strip() else "")

        return self._update(inc_id, by, mutate)

    def set_priority(self, inc_id: str, priority_id: int, by: str, recalc_deadline: bool = False, comment: str | None = None) -> dict:
        if priority_id not in self.rules.priorities:
            raise ItsmError("неизвестный приоритет")

        def mutate(rec: dict, now: datetime):
            old = rec.get("priority")
            rec["priorityId"] = priority_id
            rec["priority"] = self.rules.priorities[priority_id]["name"]
            extra = ""
            if recalc_deadline and priority_id in self.rules.sla_hours:
                start = parse_dt(rec.get("submitDate")) or now
                new_dl = start + timedelta(hours=self.rules.sla_hours[priority_id])
                rec["deadline"] = _fmt(new_dl)
                extra = f", срок пересчитан по SLA ({self.rules.sla_hours[priority_id]:g} ч)"
            return "priority", f"приоритет: {old or '—'} → {rec['priority']}{extra}" + (f". {comment.strip()}" if comment and comment.strip() else "")

        return self._update(inc_id, by, mutate)

    def set_deadline(self, inc_id: str, deadline: str, by: str, comment: str | None = None) -> dict:
        dl = parse_dt(deadline)
        if not dl:
            raise ItsmError("не понимаю дату срока")

        def mutate(rec: dict, now: datetime):
            rec["deadline"] = _fmt(dl)
            return "deadline", f"срок → {iso(dl)}" + (f". {comment.strip()}" if comment and comment.strip() else "")

        return self._update(inc_id, by, mutate)

    def comment(self, inc_id: str, text: str, by: str) -> dict:
        if not text or not text.strip():
            raise ItsmError("пустой комментарий")
        return self._update(inc_id, by, lambda rec, now: ("comment", text.strip()[:1000]))

    def escalate(self, inc_id: str, by: str, comment: str | None = None) -> dict:
        def mutate(rec: dict, now: datetime):
            if _int(rec.get("stateId")) in DONE:
                raise ItsmError("инцидент уже решён — эскалация не нужна")
            return "escalate", (comment or "").strip()[:1000] or f"эскалирован руководителю группы «{rec.get('assignedGroupName') or '—'}»"

        return self._update(inc_id, by, mutate)

    # ---------- импорт / экспорт ----------
    def import_file(self, text: str, backup_dir: Path) -> int:
        try:
            data = json.loads(text)
            incs, _ = adapt_all(data, self.rules, "{id}")
        except (ValueError, TypeError) as e:
            raise ItsmError(f"это не файл инцидентов: {e}")
        if not incs:
            raise ItsmError("в файле нет ни одного инцидента с id")
        with _write_lock:
            backup_dir.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                shutil.copy2(self.path, backup_dir / f"incidents-{datetime.now():%Y%m%d-%H%M%S}.json")
            _atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2))
        return len(incs)


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _set_state(rec: dict, state_id: int) -> None:
    from_names = {0: "Назначен", 2: "Выполняется", 7: "Завершен", 10: "Закрыт", 20: "Исследование ситуации"}
    rec["stateId"] = state_id
    rec["state"] = from_names[state_id]
    rec["stateEnum"] = STATE_ENUM[state_id]
