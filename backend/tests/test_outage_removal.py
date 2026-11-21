import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app.adapter import iso
from backend.app.itsm import Itsm, ItsmError
from backend.app.store import Store

from .conftest import NOW, make, write


def _body(outage, inc_id):
    return {"by": "Ольга", "expected_incident_ids": outage["incident_ids"],
            "expected_hash": next(inc["hash"] for inc in outage["incidents"] if inc["id"] == inc_id)}


def test_delete_outage_member_checks_both_versions_and_dissolves_atomically(make_store, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "rules")
    from backend.app import main

    records = [make("ROOT", priority_id=3), make("A", state_id=2, causeIncidentId="ROOT",
               coordinator={"name": "Ольга", "at": iso(NOW)},
               workLog=[{"at": iso(NOW), "author": "Иван", "action": "comment", "text": "Проверяем"}]),
               make("B", causeIncidentId="ROOT")]
    store, path = make_store(records)
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "itsm", itsm)
    [candidate] = store.snapshot()["outage_candidates"]
    outage_id = itsm.confirm_outage(candidate["id"], "Иван")["id"]
    client = TestClient(main.app)
    url = f"/api/outages/{outage_id}"
    initial = client.get(url).json()
    initial_a = _body(initial, "A")
    stale_b = _body(initial, "B")

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[1]["header"] = "Изменено после снимка"
    write(path, raw)
    before = path.read_bytes()
    assert client.request("DELETE", f"{url}/incidents/A", json=initial_a).status_code == 409
    assert path.read_bytes() == before
    assert store.poll()

    fresh = client.get(url).json()
    response = client.request("DELETE", f"{url}/incidents/A", json=_body(fresh, "A"))
    assert response.status_code == 200, response.text
    remaining = response.json()["outage"]
    assert remaining["id"] == outage_id and remaining["incident_ids"] == ["ROOT", "B"]
    assert _body(remaining, "B")["expected_hash"] == stale_b["expected_hash"]
    before = path.read_bytes()
    assert client.request("DELETE", f"{url}/incidents/B", json=stale_b).status_code == 409
    assert path.read_bytes() == before

    response = client.request("DELETE", f"{url}/incidents/B", json=_body(remaining, "B"))
    assert response.status_code == 200 and response.json() == {"outage": None}
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert all("outage" not in rec for rec in saved)
    assert saved[0]["workLog"][-1]["action"] == "outage_dissolve"
    assert saved[2]["workLog"][-1]["action"] == "outage_remove"
    assert saved[0]["workLog"][-1]["author"] == saved[2]["workLog"][-1]["author"] == "Ольга"
    assert saved[1]["workLog"][0] == records[1]["workLog"][0]
    assert saved[1]["workLog"][-1]["action"] == "outage_remove"
    for field in ("coordinator", "stateId", "assignedGroupName", "assignedEmployee", "causeIncidentId"):
        assert saved[1][field] == records[1][field]
    restarted = Store(store.settings, store.rules, store.clock)
    assert restarted.poll()
    assert restarted.snapshot()["outage_candidates"] == []


def test_removing_master_preserves_outage_and_scoped_ai_rejection(make_store):
    records = [make(key, productName=key, productMasterId="BI_SHARED", submitDate=iso(NOW + timedelta(minutes=i - 10)))
               for i, key in enumerate(("ROOT", "B", "C"))]
    store, path = make_store(records)
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    [candidate] = store.snapshot()["outage_candidates"]
    outage_id = itsm.confirm_outage(candidate["id"], "Иван")["id"]
    outage = store.outage(outage_id, include_incidents=True)
    updated = itsm.remove_outage_incident(outage_id, "ROOT", "Ольга", store.incident("ROOT")["hash"],
                                          outage["incident_ids"])
    assert updated["id"] == outage_id and updated["incident_ids"] == ["B", "C"]
    assert all(inc["outage"]["master_id"] == "B" for inc in updated["incidents"])
    assert itsm.remove_outage_incident(outage_id, "C", "Ольга", store.incident("C")["hash"], ["C", "B"]) is None
    restarted = Store(store.settings, store.rules, store.clock)
    assert restarted.poll()
    assert restarted.snapshot()["outage_candidates"] == []

    saved = json.loads(path.read_text(encoding="utf-8"))
    saved[0]["productMasterId"] = "BI_NEW"
    saved.extend(make(key, productName=key, productMasterId="BI_NEW", submitDate=iso(NOW - timedelta(minutes=5)))
                 for key in ("D", "E"))
    write(path, saved)
    assert restarted.poll(force=True)
    [new] = restarted.snapshot()["outage_candidates"]
    assert set(new["incident_ids"]) == {"ROOT", "D", "E"}


def test_explicit_outage_preserves_independent_branches_after_rejection(make_store):
    records = [make("R"), make("A", causeIncidentId="R",
               outageExclusions=[{"outage_id": "old", "incident_ids": ["B", "C"]}]),
               make("B", causeIncidentId="R"), make("C", causeIncidentId="R"),
               make("A-child", causeIncidentId="A"), make("B-child", causeIncidentId="B")]
    store, _ = make_store(records)
    [proposal] = store.snapshot()["outage_candidates"]
    assert proposal["incident_ids"] == ["R", "B", "B-child", "C"]
    assert store.incidents["A"]["_outage_excluded_with"] == {"B", "C"}
    assert "_outage_excluded_with" not in store.incident("A")


def test_confirm_duplicates_rejects_stale_request_with_excluded_non_master_pair(make_store):
    records = [make("MASTER"), make("A"), make("B")]
    store, path = make_store(records)
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    records[1]["outageExclusions"] = [{"outage_id": "old", "incident_ids": ["B"]}]
    write(path, records)
    assert "_outage_excluded_with" not in store.incidents["A"]  # Снимок ещё содержит старое состояние.
    before = path.read_bytes()
    with pytest.raises(ItsmError) as exc:
        itsm.confirm_duplicates("MASTER", ["A", "B"], "Ольга")
    assert exc.value.status == 409
    assert path.read_bytes() == before

    updated = itsm.confirm_duplicates("MASTER", ["B"], "Ольга")
    assert updated[0]["isMaster"] and updated[1]["isDouble"]
