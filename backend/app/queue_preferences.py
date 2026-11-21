"""Личные скрытия задач. Кэш очереди хранится отдельно от выгрузки инцидентов."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from .adapter import iso, parse_dt

Operation = Literal["snooze", "dismiss"]
SNOOZE_FOR = timedelta(minutes=15)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QueuePreferences:
    def __init__(self, path: Path | None = None, now_fn: Callable[[], datetime] = _utcnow):
        self.path = path
        self.now_fn = now_fn
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict[str, dict[str, datetime | None]]:
        if self.path is None:
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        preferences: dict[str, dict[str, datetime | None]] = {}
        for by, items in data.items():
            if not isinstance(items, dict):
                continue
            preferences[by] = {item_id: expires for item_id, value in items.items()
                               if (expires := parse_dt(value)) is not None or value is None}
        return preferences

    def hidden(self, by: str) -> set[str]:
        with self._lock:
            now = self.now_fn()
            return {item_id for item_id, expires in self._data.get(by.strip().casefold(), {}).items()
                    if expires is None or expires > now}

    def set(self, by: str, item_id: str, operation: Operation) -> None:
        with self._lock:
            now = self.now_fn()
            updated = {name: {key: expires for key, expires in items.items() if expires is None or expires > now}
                       for name, items in self._data.items()}
            updated.setdefault(by.strip().casefold(), {})[item_id] = now + SNOOZE_FOR if operation == "snooze" else None
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                data = {name: {key: iso(expires) for key, expires in items.items()}
                        for name, items in updated.items() if items}
                try:
                    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(self.path)
                finally:
                    tmp.unlink(missing_ok=True)
            self._data = updated
