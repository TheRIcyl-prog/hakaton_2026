"""«Сейчас» для дашборда.

export: сейчас = момент выгрузки + сколько реально прошло с запуска сервера.
        Так сроки из старой выгрузки не становятся разом просроченными и при этом тикают.
real:   обычное системное время.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self, mode: str = "export", anchor: datetime | None = None):
        self.mode = mode if mode in ("export", "real") else "export"
        self._anchor: datetime | None = anchor
        self._started = time.monotonic()

    def set_anchor_if_empty(self, anchor: datetime | None) -> None:
        if self._anchor is None and anchor is not None:
            self._anchor = anchor
            self._started = time.monotonic()

    @property
    def anchor(self) -> datetime | None:
        return self._anchor

    def now(self) -> datetime:
        if self.mode == "real" or self._anchor is None:
            return datetime.now(timezone.utc)
        return self._anchor + timedelta(seconds=time.monotonic() - self._started)

    def offset_seconds(self) -> float:
        """Насколько «сейчас» дашборда отличается от реального (для тикающих таймеров на фронте)."""
        return (self.now() - datetime.now(timezone.utc)).total_seconds()
