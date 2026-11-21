"""Исправления координаторов и отдельная от истории передач статистика."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from ..adapter import iso

log = logging.getLogger("dashboard.ai")
KINDS = ("route", "summary", "risk", "dynamics")
SOURCES = ("rules", "gigachat", "cache")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _required(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise ValueError(f"Некорректное поле {field}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"Некорректное поле {field}")
    return value.strip()


def signature(inc: dict) -> str | None:
    parts = [" ".join((inc.get(field) or "").casefold().split()) for field in ("product", "service", "scenario")]
    return _digest(parts) if any(parts) else None


def _old_value(ai: dict, kind: str) -> dict:
    value = ai.get(kind)
    if kind == "summary" and isinstance(value, str) and value.strip():
        old = {"fingerprint": _digest(value)}
        meta = ai.get("summary_meta") or {}
    elif kind != "summary" and isinstance(value, dict) and value:
        meta = value
        if kind == "route":
            old = {"team": _required(value.get("team"), "ai.route.team")}
        else:
            variant_fields = ("score", "level", "factors", "comment") if kind == "risk" else ("text", "patterns", "comment")
            old = {"fingerprint": _digest([value.get(field) for field in variant_fields])}
            if kind == "risk" and type(value.get("score")) is int:
                old["score"] = value["score"]
    else:
        raise ValueError("У инцидента нет такой подсказки ИИ")
    source = meta.get("source")
    return {**old, "source": source if source in SOURCES else "rules"}


@dataclass(frozen=True)
class RouteEvidence:
    counts: dict[str, int]
    total: int
    preferred_team: str | None
    examples: list[dict]


class FeedbackStore:
    def __init__(self, path: Path | None):
        self.path = path
        self.lock = Lock()
        self._dirty = False
        self._read_failed = False
        self._records = self._load()
        self._routes = self._index(self._records)

    def _load(self) -> dict:
        if self.path is None:
            return {}
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("ожидался список исправлений")
            for row in rows:
                if (not isinstance(row, dict) or not all(isinstance(row.get(key), str) for key in
                        ("reviewer", "incident_id", "kind", "at")) or row["kind"] not in KINDS
                        or not isinstance(row.get("old"), dict)):
                    raise ValueError("некорректная запись исправления")
                if row["kind"] == "route":
                    _required(row.get("corrected_team"), "corrected_team")
                    _required(row["old"].get("team"), "old.team")
                    if row.get("signature") is not None and not isinstance(row["signature"], str):
                        raise ValueError("некорректная сигнатура исправления")
            return {(row["reviewer"], row["incident_id"], row["kind"]): row for row in rows}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            self._read_failed = True
            log.warning("Не удалось прочитать память исправлений ИИ")
            return {}

    def _save(self, records: dict) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                    prefix=".ai_feedback-", suffix=".tmp", delete=False) as output:
                tmp = Path(output.name)
                json.dump(list(records.values()), output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            tmp.replace(self.path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    @staticmethod
    def _index(records: dict) -> dict[str, list[dict]]:
        routes = defaultdict(list)
        for row in reversed(list(records.values())):
            if row["kind"] == "route" and row.get("signature"):
                routes[row["signature"]].append(row)
        return dict(routes)

    def record(self, inc: dict, by: str, kind: str, corrected_team: str | None) -> bool:
        if kind not in KINDS or not isinstance(inc, dict) or not isinstance(inc.get("ai"), dict):
            raise ValueError("Некорректный вид исправления или инцидент")
        reviewer = _digest(" ".join(_required(by, "by").casefold().split()))
        inc_id = _required(inc.get("id"), "incident_id")
        if kind == "route":
            corrected_team = _required(corrected_team, "corrected_team")
        elif corrected_team is not None:
            raise ValueError("Команду можно указать только для исправления маршрута")
        old = _old_value(inc["ai"], kind)
        row = {"reviewer": reviewer, "incident_id": inc_id, "kind": kind, "signature": signature(inc),
               "old": old, "corrected_team": corrected_team}
        key = reviewer, inc_id, kind
        with self.lock:
            previous = self._records.get(key)
            if previous and all(previous.get(field) == value for field, value in row.items()):
                return False
            if kind == "route" and old["team"].casefold() == corrected_team.casefold():
                if previous and previous["corrected_team"] == corrected_team:
                    return False
                raise ValueError("Выбранная команда совпадает с подсказкой")
            if self._read_failed:
                raise OSError("Память исправлений ИИ недоступна")
            updated = dict(self._records)
            updated.pop(key, None)
            updated[key] = {**row, "at": iso(datetime.now(timezone.utc))}
            self._save(updated)
            self._records = updated
            if kind == "route":
                self._routes = self._index(updated)
            self._dirty = True
            return True

    def route_index(self) -> dict[str, list[dict]]:
        # Индекс заменяется целиком после успешной записи. Один enrich видит одну ревизию.
        return self._routes

    def consume_dirty(self) -> bool:
        with self.lock:
            dirty, self._dirty = self._dirty, False
            return dirty


def route_evidence(inc: dict, index: dict[str, list[dict]]) -> RouteEvidence | None:
    if not index:
        return None
    rows = [row for row in index.get(signature(inc), ()) if row["incident_id"] != inc["id"]]
    if not rows:
        return None
    counts = Counter(row["corrected_team"] for row in rows)
    team, count = counts.most_common(1)[0]
    preferred = team if count * 2 > len(rows) else None
    examples = [{"suggested_team": row["old"]["team"], "corrected_team": row["corrected_team"]} for row in rows[:3]]
    return RouteEvidence(dict(sorted(counts.items())), len(rows), preferred, examples)
