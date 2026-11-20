"""История изменений инцидентов — для «тревожной динамики» ИИ (п. 8.5) и панели инцидента.

Сервер сам замечает изменения (правки файла жюри и действия из АРМ/ITSM) и дописывает их
в cache/history.jsonl — история переживает перезапуск. При первом появлении инцидента история
засевается из структурированных записей workLog ({"kind", "before", "after"} — их пишут itsm.py и
генератор демо), поэтому демо-файл сразу показывает «Средний → Высокий → Наивысший за 40 мин».

Формат записи совпадает с ai/dynamics.History.get() — ИИ может брать историю отсюда:
    {"at": ISO Z, "kind": "priority" | "team" | "deadline" | "state" | "reopen" | "assignee",
     "before": текст, "after": текст, "by": автор | None,
     "up": bool (priority), "before_id"/"after_id": int (priority, state),
     "later": bool, "shift_min": int | None (deadline)}
Интерфейс:
    history.get(inc_id) -> list            # старые → новые (как в ai/dynamics)
    history.changes(inc_id, since=None, kind=None) -> list
    history.changed_since(since) -> set[id]
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .adapter import iso, parse_dt

MAX_PER_INCIDENT = 50
DONE = ("resolved", "research")  # «вернули в работу» — из решённого (закрытый финальный)


def compare(prev: dict, inc: dict, stamp: str, by: str | None) -> list[dict]:
    """Отличия двух версий инцидента в формате истории."""
    out: list[dict] = []
    p_id, c_id = prev.get("priority_id"), inc.get("priority_id")
    if p_id != c_id and p_id is not None and c_id is not None:
        out.append({"at": stamp, "kind": "priority", "before": prev.get("priority"), "after": inc.get("priority"),
                    "before_id": p_id, "after_id": c_id, "up": c_id > p_id, "by": by})
    if prev.get("team") != inc.get("team"):
        out.append({"at": stamp, "kind": "team", "before": prev.get("team"), "after": inc.get("team"), "by": by})
    if prev.get("assignee") != inc.get("assignee"):
        out.append({"at": stamp, "kind": "assignee", "before": prev.get("assignee"), "after": inc.get("assignee"), "by": by})
    if prev.get("deadline") != inc.get("deadline"):
        b, a = parse_dt(prev.get("deadline")), parse_dt(inc.get("deadline"))
        out.append({"at": stamp, "kind": "deadline", "before": prev.get("deadline"), "after": inc.get("deadline"),
                    "later": bool(b and a and a > b),
                    "shift_min": round((a - b).total_seconds() / 60) if a and b else None, "by": by})
    if prev.get("state_id") != inc.get("state_id"):
        kind = "reopen" if prev.get("state_kind") in DONE and inc.get("state_kind") == "active" else "state"
        out.append({"at": stamp, "kind": kind, "before": prev.get("state"), "after": inc.get("state"),
                    "before_id": prev.get("state_id"), "after_id": inc.get("state_id"), "by": by})
    return out


class History:
    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.Lock()
        self._by_id: dict[str, list[dict]] = defaultdict(list)
        self._load()

    # ---------- хранение ----------
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # оборванная последняя строка после сбоя — пропускаем
            if isinstance(rec, dict) and rec.get("id") and rec.get("kind"):
                self._by_id[rec.pop("id")].append(rec)
        for v in self._by_id.values():
            v.sort(key=lambda c: c.get("at") or "")
            del v[:-MAX_PER_INCIDENT]

    def _append(self, rows: list[dict]) -> None:
        if not self.path or not rows:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        except OSError:
            pass  # история не должна ронять дашборд

    def clear(self) -> None:
        """Файл подменили целиком — старая история к новым инцидентам не относится."""
        with self.lock:
            self._by_id.clear()
            if self.path and self.path.exists():
                try:
                    self.path.unlink()
                except OSError:
                    pass

    # ---------- запись ----------
    def record(self, old: dict[str, dict], new: dict[str, dict], ids, now: datetime) -> None:
        """Сравнить прошлую и новую версию изменённых инцидентов (ids) и записать отличия."""
        rows: list[dict] = []
        stamp = iso(now)
        with self.lock:
            for inc_id in ids:
                inc, prev = new.get(inc_id), old.get(inc_id)
                if inc is None:
                    continue
                if prev is None:
                    if inc_id not in self._by_id:
                        rows += self._seed(inc)
                    continue
                by = None
                if len(inc.get("work_log") or []) > len(prev.get("work_log") or []):
                    by = inc["work_log"][-1].get("author")
                for ch in compare(prev, inc, stamp, by):
                    self._add(inc_id, ch)
                    rows.append({"id": inc_id, **ch})
        self._append(rows)

    def _seed(self, inc: dict) -> list[dict]:
        rows = []
        for e in inc.get("work_log") or []:
            if not (e.get("kind") and e.get("at")):
                continue
            ch = {k: v for k, v in e.items() if k not in ("author", "action", "text")}
            ch["by"] = e.get("author")
            self._add(inc["id"], ch)
            rows.append({"id": inc["id"], **ch})
        return rows

    def _add(self, inc_id: str, ch: dict) -> None:
        lst = self._by_id[inc_id]
        lst.append(ch)
        if len(lst) > MAX_PER_INCIDENT:
            del lst[:-MAX_PER_INCIDENT]

    # ---------- чтение (ИИ, панель инцидента) ----------
    def get(self, inc_id: str) -> list[dict]:
        """O(1) по числу инцидентов (≤ MAX_PER_INCIDENT записей). Записи только читать — это не копии."""
        lst = self._by_id.get(inc_id)
        return list(lst) if lst else []

    def changes(self, inc_id: str, since: datetime | None = None, kind: str | None = None) -> list[dict]:
        return [dict(c) for c in self.get(inc_id)
                if (not kind or c["kind"] == kind) and (not since or (parse_dt(c.get("at")) or since) >= since)]

    def changed_since(self, since: datetime) -> set[str]:
        with self.lock:
            return {i for i, lst in self._by_id.items() if lst and (parse_dt(lst[-1].get("at")) or since) >= since}
