import json
from datetime import timedelta

import pytest

from backend.app import ai
from backend.app.adapter import parse_dt
from backend.app.history import History
from backend.app.itsm import Itsm, ItsmError
from backend.app.store import Store
from scripts import demo, make_demo

from .conftest import NOW, FakeClock, make, write


@pytest.fixture
def proposal(monkeypatch):
    candidate = {"master_id": "A", "ids": ["A", "B", "C"], "name": "Общий шлюз", "reason": "Общий код",
                 "source": "rules"}
    monkeypatch.setattr(ai, "outage_candidates", lambda: [candidate])
    return candidate


@pytest.fixture
def records():
    return [
        make("A", priority_id=3, productName="Избушка", deadline="2026-11-10T12:45:00Z"),
        make("B", productName="Ковёр", deadline="2026-11-10T12:30:00Z", isDouble=True,
             workLog=[{"at": "2026-11-10T11:55:00Z", "author": "Иван", "action": "comment", "text": "Проверяю"}]),
        make("C", productName="Скатерть", deadline=None),
        make("OTHER", productName="Сапоги", productMasterId="BI_OTHER"),
    ]


def test_outage_confirmation_restart_and_expiry(make_store, proposal, records):
    clock = FakeClock()
    store, path = make_store(records, clock)
    history_path = path.with_name("history.jsonl")
    store.history = History(history_path)
    first = store.snapshot()
    [candidate] = first["outage_candidates"]
    assert candidate == {
        "id": candidate["id"], "name": "Общий шлюз", "reason": "Общий код", "source": "rules",
        "incident_ids": ["A", "B", "C"], "products": ["Избушка", "Ковёр", "Скатерть"],
        "active_count": 3, "priority_id": 3, "deadline": "2026-11-10T12:30:00Z",
    }
    assert "C" not in first["incidents"]

    # Независимая внешняя правка должна сохраниться при подтверждении.
    records[3]["header"] = "Внешнее изменение"
    write(path, records)
    itsm = Itsm(path, store.rules, clock.now, store=store)
    confirmed = itsm.confirm_outage(candidate["id"], "Ольга")
    outage_id = confirmed["id"]
    assert confirmed == {**candidate, "status": "active"}
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[3] == records[3]
    for i, raw in enumerate(saved[:3]):
        assert raw["outage"] == {"id": outage_id, "master_id": "A", "name": "Общий шлюз",
                                  "reason": "Общий код", "source": "rules"}
        assert raw.get("isDouble") == records[i].get("isDouble")
        assert raw.get("isMaster") == records[i].get("isMaster")
        assert raw["workLog"][:-1] == records[i].get("workLog", [])
        assert raw["workLog"][-1]["author"] == "Ольга"
        assert raw["workLog"][-1]["action"] == "outage_confirm"

    def restart():
        restarted = Store(store.settings, store.rules, clock, history=History(history_path))
        assert restarted.poll()
        return restarted

    store = restart()
    assert store.snapshot()["outage_candidates"] == []
    assert store.snapshot()["outages"] == [confirmed]
    itsm = Itsm(path, store.rules, clock.now, store=store)
    itsm.set_state("A", 7, "Ольга")
    assert store.outage(outage_id)["active_count"] == 2
    assert store.outage(outage_id)["priority_id"] == 2
    assert store.outage(outage_id)["incident_ids"] == ["A", "B", "C"]
    clock._now = NOW + timedelta(minutes=5)
    itsm.set_state("B", 7, "Ольга")
    assert store.outage(outage_id)["deadline"] is None

    clock._now = NOW + timedelta(minutes=20)
    edited = json.loads(path.read_text(encoding="utf-8"))
    edited[2].update(stateId=7, state="Завершен", stateEnum="Resolved")
    write(path, edited)  # Внешняя правка без completionDate.
    assert store.poll()
    [resolved] = store.snapshot()["outages"]
    assert resolved == {**confirmed, "status": "resolved", "active_count": 0, "priority_id": None, "deadline": None}

    clock._now = NOW + timedelta(minutes=49)
    store = restart()
    assert store.snapshot()["outages"] == [resolved]
    cached_body, version = store.snapshot_bytes(gzip_ok=False)
    assert json.loads(cached_body)["outages"] == [resolved]
    clock._now = NOW + timedelta(minutes=50)
    assert store.poll()
    body, current_version = store.snapshot_bytes(gzip_ok=False)
    assert current_version > version
    assert json.loads(body)["outages"] == []
    details = store.outage(outage_id, include_incidents=True)
    assert details["status"] == "resolved"
    assert [inc["id"] for inc in details["incidents"]] == ["A", "B", "C"]
    assert store.outage("missing", include_incidents=True) is None


@pytest.mark.parametrize("change", ["record", "linked", "proposal"])
def test_confirm_conflict_leaves_the_file_untouched(make_store, proposal, records, change, monkeypatch):
    store, path = make_store(records)
    [candidate] = store.snapshot()["outage_candidates"]
    if change == "proposal":
        proposal.update(name="Уточнённый сбой", source="cache")
        monkeypatch.setattr(ai, "consume_dirty", lambda: True)
        assert store.poll()
        [current] = store.snapshot()["outage_candidates"]
        assert current["id"] != candidate["id"]
    else:
        if change == "linked":
            records[1]["outage"] = {"id": "other-outage"}
        else:
            records[1]["header"] = "Изменили во время подтверждения"
        write(path, records)  # Снимок ещё не обновился.

    before = path.read_bytes()
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    with pytest.raises(ItsmError) as exc_info:
        itsm.confirm_outage(candidate["id"], "Ольга")
    assert exc_info.value.status == 409
    if change == "linked":
        assert "уже привязан" in str(exc_info.value)
    assert path.read_bytes() == before
    assert store.snapshot()["outages"] == []


def test_demo_outage_data_preserves_mirror_duplicates_and_demo_steps(make_store):
    records = make_demo.build(NOW)
    by_id = {raw["id"]: raw for raw in records}
    linked = [by_id[f"INC0000300000{i}"] for i in (21, 22, 23)]
    unrelated = by_id["INC000030000024"]
    assert len({raw["productMasterId"] for raw in linked}) == 1
    assert unrelated["productMasterId"] != linked[0]["productMasterId"]
    assert linked[0]["submitDate"] < unrelated["submitDate"] < linked[-1]["submitDate"]
    assert parse_dt(linked[-1]["submitDate"]) - parse_dt(linked[0]["submitDate"]) <= timedelta(minutes=15)
    assert all(raw["stateId"] in (0, 2) for raw in [*linked, unrelated])
    for field in ("productName", "receivingChannel", "header"):
        assert len({raw[field] for raw in linked}) == 3
    store, _ = make_store(records)
    assert any(store.incidents[raw["id"]]["groups"] for raw in linked)
    related_ids = {raw["id"] for raw in linked}
    [candidate] = [c for c in store.snapshot()["outage_candidates"] if linked[0]["id"] in c["incident_ids"]]
    assert set(candidate["incident_ids"]) == related_ids
    mirrors = {f"INC0000300000{i}" for i in range(11, 17)}
    assert any(set(group["ids"]) == mirrors for group in ai.duplicate_groups())
    for step in demo.STEPS:
        step(records)
