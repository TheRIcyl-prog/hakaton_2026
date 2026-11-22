"""Настройки из .env и правила групп из config/rules.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]

try:  # .env необязателен
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


@dataclass
class Settings:
    incidents_file: Path = field(default_factory=lambda: _path(os.getenv("INCIDENTS_FILE", "data/incidents.json")))
    rules_file: Path = field(default_factory=lambda: _path(os.getenv("RULES_FILE", "config/rules.yaml")))
    itsm_url_template: str = field(
        default_factory=lambda: os.getenv("ITSM_URL_TEMPLATE", "/itsm/{id}")
    )
    time_mode: str = field(default_factory=lambda: os.getenv("TIME_MODE", "export"))
    export_now: str | None = field(default_factory=lambda: os.getenv("EXPORT_NOW") or None)
    poll_interval: float = field(default_factory=lambda: float(os.getenv("POLL_INTERVAL", "0.25")))
    # Чужие правки файла применяем не чаще раза в столько секунд (частые сохранения редактора склеиваются)
    coalesce_seconds: float = field(default_factory=lambda: float(os.getenv("COALESCE_SECONDS", "0.5")))
    # Копии файла перед каждой записью из АРМ/ITSM (храним backups_keep последних)
    backups_dir: Path = field(default_factory=lambda: _path(os.getenv("BACKUPS_DIR", "cache/backups")))
    backups_keep: int = field(default_factory=lambda: int(os.getenv("BACKUPS_KEEP", "20")))
    frontend_dir: Path = field(default_factory=lambda: ROOT / "frontend")
    demo_accounts: str = field(default_factory=lambda: os.getenv("DEMO_ACCOUNTS", ""), repr=False)
    demo_session_seconds: str = field(default_factory=lambda: os.getenv("DEMO_SESSION_SECONDS", "7200"))
    demo_origins: str = field(default_factory=lambda: os.getenv(
        "DEMO_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000"))
    cache_dir: Path = field(default_factory=lambda: _path(os.getenv("CACHE_DIR", "cache")))


@dataclass
class GroupRule:
    key: str
    title: str
    priority_ids: list[int] | None = None
    state_ids: list[int] | None = None
    teams: list[str] | None = None
    show_resolved_minutes: int = 0


@dataclass
class Rules:
    states: dict[int, dict[str, str]]
    state_enum: dict[str, int]
    priorities: dict[int, dict[str, str]]
    priority_text: dict[str, int]
    coordinators_team: str
    groups: list[GroupRule]
    transitions: dict[int, list[int]] = field(default_factory=dict)
    sla_hours: dict[int, float] = field(default_factory=dict)

    def state_kind(self, state_id: int | None) -> str:
        if state_id is None or state_id not in self.states:
            return "unknown"
        return self.states[state_id]["kind"]

    def priority_level(self, priority_id: int | None) -> str:
        if priority_id is None or priority_id not in self.priorities:
            return "unknown"
        return self.priorities[priority_id]["level"]


def load_rules(path: Path) -> Rules:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    groups = [
        GroupRule(
            key=key,
            title=g.get("title", key),
            priority_ids=g.get("priority_ids"),
            state_ids=g.get("state_ids"),
            teams=g.get("teams"),
            show_resolved_minutes=int(g.get("show_resolved_minutes", 0)),
        )
        for key, g in raw["groups"].items()
    ]
    return Rules(
        states={int(k): v for k, v in raw["states"].items()},
        state_enum=dict(raw.get("state_enum", {})),
        priorities={int(k): v for k, v in raw["priorities"].items()},
        priority_text=dict(raw.get("priority_text", {})),
        coordinators_team=raw["coordinators_team"],
        groups=groups,
        transitions={int(k): [int(x) for x in (v or [])] for k, v in (raw.get("transitions") or {}).items()},
        sla_hours={int(k): float(v) for k, v in (raw.get("sla_hours") or {}).items()},
    )
