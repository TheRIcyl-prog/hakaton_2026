import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

from backend.app import store as store_module
from backend.app.adapter import content_hash, iso
from backend.app.itsm import Itsm

from .conftest import COORD, NOW, make, write


@pytest.fixture(autouse=True)
def _disable_ai(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "off")
    monkeypatch.setattr(store_module, "enrich", lambda *args, **kwargs: None)
    monkeypatch.setattr(store_module.ai_layer, "outage_candidates", lambda: [])


def _connect(store, path, monkeypatch, coordinator_clients):
    from backend.app import main

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "itsm", Itsm(path, store.rules, store.clock.now, store=store))
    return coordinator_clients(main.app, "Alice", "Bob", "Casey")


def _queue(client):
    return {item["id"] for item in client.get("/api/queue").json()["items"]}


def test_racing_claims_and_version_bound_takeover_update_every_coordinator(make_store, monkeypatch, coordinator_clients):
    store, path = make_store([make("K", priority_id=4, team=COORD)])
    clients = _connect(store, path, monkeypatch, coordinator_clients)
    client = clients["Alice"]
    claim = "/api/incidents/K/claim"
    seen = client.get("/api/incidents/K").json()["hash"]
    start = Barrier(2)

    def take(by):
        start.wait(timeout=5)
        return by, clients[by].post(claim, json={"expected_hash": seen}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(take, ["Alice", "Bob"]))
    assert sorted(status for _, status in results) == [200, 409]
    owner = next(by for by, status in results if status == 200)
    other = next(by for by, status in results if status == 409)
    snapshot = client.get("/api/snapshot").json()
    assert snapshot["claims"]["K"]["by"] == owner
    assert next(alert for alert in snapshot["alerts"] if alert["key"] == "critical:K")["taken_by"] == owner
    assert "route:K" in _queue(clients[owner]) and "route:K" not in _queue(clients[other])

    before = path.read_bytes()
    for version in ({}, {"expected_hash": None}, {"expected_hash": " "}):
        assert clients[other].post(claim, json={"force": True, **version}).status_code == 400
    for response in (clients[other].delete(claim, params={"by": owner}),
                     clients[other].post("/api/itsm/K/assign", json={"team": "Support"}),
                     clients[other].post("/api/itsm/K/escalate", json={})):
        assert response.status_code == 409 and response.json()["coordinator"] == owner
    assert path.read_bytes() == before

    prompt = clients[other].post(claim, json={})
    assert prompt.status_code == 409 and prompt.json()["coordinator"] == owner
    third = clients["Casey"].post(claim, json={"force": True, "expected_hash": prompt.json()["hash"]})
    assert third.status_code == 200
    before = path.read_bytes()
    delayed = clients[other].post(claim, json={"force": True, "expected_hash": prompt.json()["hash"]})
    assert delayed.status_code == 409 and delayed.json()["conflict"]
    assert path.read_bytes() == before
    fresh = clients[other].post(claim, json={}).json()
    assert fresh["coordinator"] == "Casey"
    taken = clients[other].post(claim, json={"force": True, "expected_hash": fresh["hash"]})
    assert taken.status_code == 200 and taken.json()["coordinator"]["name"] == other
    assert clients["Casey"].delete(claim).status_code == 409
    assert clients[other].delete(claim).status_code == 200
    snapshot = client.get("/api/snapshot").json()
    assert "K" not in snapshot["claims"]
    assert next(alert for alert in snapshot["alerts"] if alert["key"] == "critical:K")["taken_by"] is None
    assert all("claim:K" in _queue(clients[by]) for by in (owner, other, "Casey"))


def test_stale_raw_hash_guards_actions_and_escalation_deduplicates_legacy_retry(make_store, monkeypatch, coordinator_clients):
    records = [make("K", state_id=2, priority_id=4, team=COORD, deadline=iso(NOW - timedelta(minutes=5)))]
    store, path = make_store(records)
    clients = _connect(store, path, monkeypatch, coordinator_clients)
    client = clients["Alice"]
    seen = client.get("/api/incidents/K").json()["hash"]
    records[0]["header"] = "Edited outside the dashboard"
    write(path, records)  # Keep the old snapshot in memory until after the requests.
    before = path.read_bytes()
    for action, fields in [("assign", {"team": "Support"}), ("escalate", {}), ("comment", {"text": "Old view"})]:
        stale = client.post(f"/api/itsm/K/{action}", json={"expected_hash": seen, **fields})
        assert stale.status_code == 409 and stale.json()["hash"] == content_hash(records[0])
    assert path.read_bytes() == before

    assert client.post("/api/itsm/K/escalate", json={}).status_code == 200
    before = path.read_bytes()
    assert clients["Bob"].post("/api/itsm/K/escalate", json={}).status_code == 409
    assert path.read_bytes() == before
    assert all("escalate:K" not in _queue(clients[by]) for by in ("Alice", "Bob"))

    claimed = client.post("/api/incidents/K/claim", json={}).json()
    assigned = client.post("/api/itsm/K/assign", json={"team": "Support", "expected_hash": claimed["hash"]})
    assert assigned.status_code == 200
    assert assigned.json()["team"] == "Support" and assigned.json()["state_id"] == 0
    assert assigned.json()["coordinator"] is None and "K" not in client.get("/api/snapshot").json()["claims"]
    assert all("route:K" not in _queue(clients[by]) and "escalate:K" in _queue(clients[by]) for by in ("Alice", "Bob"))


def test_actions_use_the_same_imported_record_and_status_as_the_snapshot(make_store, monkeypatch, coordinator_clients):
    records = [make("DUP", header="Earlier duplicate"), make("DUP", state_id=2, stateId=None),
               make("DONE", state_id=7, stateId=None)]
    store, path = make_store(records)
    clients = _connect(store, path, monkeypatch, coordinator_clients)
    client = clients["Alice"]
    assert client.post("/api/incidents/DONE/claim", json={}).status_code == 400
    assert client.post("/api/itsm/DONE/assign", json={"team": "Support"}).status_code == 400
    assert client.post("/api/itsm/DONE/escalate", json={}).status_code == 400
    seen = client.get("/api/incidents/DUP").json()["hash"]
    edited = client.post("/api/itsm/DUP/assign", json={"team": "Support", "expected_hash": seen})
    assert edited.status_code == 200 and edited.json()["state_id"] == 0
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[0] == records[0]
    assert saved[1]["assignedGroupName"] == "Support"
