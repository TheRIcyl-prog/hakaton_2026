import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.auth import DemoAuth, hash_password
from backend.app.clock import Clock
from backend.app.config import ROOT, Settings, load_rules
from backend.app.store import Store

COORD = "КЦ ИТ Координаторы Массовых Инцидентов"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
ORIGIN = "http://testserver"
TEST_PASSWORD = "test-only-password"


@pytest.fixture(scope="session")
def account_hash():
    return hash_password(TEST_PASSWORD)


@pytest.fixture
def configure_auth(monkeypatch, account_hash):
    def configure(app, names, *, ttl=7200, origin=ORIGIN):
        accounts = [dict(id=str(i), login=name.casefold(), display_name=name, password_hash=account_hash)
                    for i, name in enumerate(names)]
        settings = Settings(demo_accounts=json.dumps(accounts), demo_origins=origin, demo_session_seconds=str(ttl))
        monkeypatch.setitem(app.state._state, "auth", DemoAuth(settings))
        return settings
    return configure


@pytest.fixture
def coordinator_clients(configure_auth):
    def connect(app, *names):
        configure_auth(app, names)
        clients = {}
        for name in names:
            client = TestClient(app, headers={"Origin": ORIGIN})
            response = client.post("/api/auth/login", json={"login": name.casefold(), "password": TEST_PASSWORD})
            assert response.status_code == 200 and response.json()["display_name"] == name
            clients[name] = client
        return clients
    return connect


@pytest.fixture
def rules():
    return load_rules(ROOT / "config" / "rules.yaml")


def make(id_="INC1", state_id=0, priority_id=2, team="КЦ ИТ Мой МТС", **extra):
    states = {0: ("Назначен", "Assigned"), 2: ("Выполняется", "WorkInProgress"), 7: ("Завершен", "Resolved"),
              10: ("Закрыт", "Closed"), 20: ("Исследование ситуации", "Research")}
    prios = {1: "Низкий", 2: "Средний", 3: "Высокий", 4: "Наивысший"}
    raw = {
        "sysId": "1" + id_[-3:],
        "id": id_,
        "state": states[int(state_id)][0],
        "stateEnum": states[int(state_id)][1],
        "stateId": state_id,
        "submitDate": "2026-11-10T10:00:00Z",
        "deadline": "2026-11-10T14:00:00Z",
        "completionDate": None,
        "header": f"Сбой {id_}",
        "description": "Описание. Второе предложение.",
        "shortDescription": "Коротко.",
        "assignedGroupName": team,
        "assignedEmployee": "Винни (winnie)",
        "priorityId": priority_id,
        "priority": prios[int(priority_id)],
        "productName": "Летающий ковер",
        "productMasterId": "BI_1",
        "service": "[Летающий ковер] Ворс",
        "scenarioName": "Полёт",
    }
    raw.update(extra)
    return raw


class FakeClock(Clock):
    def __init__(self, now=NOW):
        super().__init__("export", now)
        self._now = now

    def now(self):
        return self._now


@pytest.fixture
def make_store(tmp_path, rules):
    def _make(records, clock=None):
        f = tmp_path / "incidents.json"
        write(f, records)
        s = Settings()
        s.incidents_file = f
        s.coalesce_seconds = 0  # тесты пишут файл и сразу опрашивают — склейку правок отключаем
        store = Store(s, rules, clock or FakeClock())
        store.poll()
        return store, f

    return _make


def write(path: Path, records) -> None:
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_ai():
    """ИИ хранит историю изменений между вызовами — каждому тесту чистое состояние без диска."""
    from backend.app import ai

    ai.reset()
    yield
    ai.reset()
