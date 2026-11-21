import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from backend.app.adapter import iso
from backend.app.itsm import Itsm, ItsmError
from backend.app.store import Store

from .conftest import NOW, make, write


def _heuristic_group(products=("Ковёр", "Избушка", "Зеркальце")):
    return [make(inc_id, productName=product, submitDate=iso(NOW - timedelta(minutes=10 - i)))
            for i, (inc_id, product) in enumerate(zip("ABC", products))]


def test_delete_candidate_member_returns_pair_without_confirming_and_survives_reload(make_store, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "rules")
    from backend.app import main

    records = _heuristic_group()
    store, path = make_store(records)
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    monkeypatch.setattr(main, "itsm", itsm)
    [candidate] = store.snapshot()["outage_candidates"]
    url = f"/api/outage-candidates/{candidate['id']}/incidents/A"
    client = TestClient(main.app)
    response = client.request("DELETE", url, json={"by": "Ольга"})
    assert response.status_code == 200, response.text
    pair = response.json()["candidate"]
    assert pair["id"] != candidate["id"] and pair["incident_ids"] == ["B", "C"]
    assert store.snapshot()["outage_candidates"] == [pair]
    assert store.snapshot()["outages"] == []
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[1:] == records[1:]
    assert set(saved[0]) - set(records[0]) == {"outageExclusions", "workLog"}
    assert saved[0]["outageExclusions"][0]["incident_ids"] == ["B", "C"]
    assert saved[0]["workLog"][-1]["author"] == "Ольга"
    assert saved[0]["workLog"][-1]["action"] == "outage_candidate_remove"
    assert client.request("DELETE", url, json={"by": "Ольга"}).status_code == 409

    restarted = Store(store.settings, store.rules, store.clock)
    assert restarted.poll()
    assert restarted.snapshot()["outage_candidates"] == [pair]
    after_restart = Itsm(path, store.rules, store.clock.now, store=restarted)
    assert after_restart.confirm_outage(pair["id"], "Ольга")["incident_ids"] == ["B", "C"]


def test_stale_hash_of_another_member_prevents_exclusion(make_store):
    records = _heuristic_group()
    store, path = make_store(records)
    [candidate] = store.snapshot()["outage_candidates"]
    records[2]["header"] = "Изменили после показа кандидата"
    write(path, records)
    before = path.read_bytes()
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    with pytest.raises(ItsmError) as error:
        itsm.remove_outage_candidate_incident(candidate["id"], "A", "Ольга")
    assert error.value.status == 409
    assert path.read_bytes() == before


@pytest.mark.parametrize("removed,expected", [("A", ["R", "B"]), ("R", None)])
def test_explicit_candidate_keeps_only_effects_with_valid_ancestry(make_store, removed, expected):
    records = [make("R"), make("A", causeIncidentId="R"), make("A-child", causeIncidentId="A"),
               make("B", causeIncidentId="R")]
    store, path = make_store(records)
    [candidate] = store.snapshot()["outage_candidates"]
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    refreshed = itsm.remove_outage_candidate_incident(candidate["id"], removed, "Ольга")
    assert (refreshed["incident_ids"] if refreshed else None) == expected
    assert store.snapshot()["outage_candidates"] == ([refreshed] if expected else [])


def test_narrowed_heuristic_group_requires_two_products_and_current_time_window(make_store):
    records = _heuristic_group(("Ковёр", "Избушка", "Избушка"))
    store, path = make_store(records)
    [candidate] = store.snapshot()["outage_candidates"]
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    assert itsm.remove_outage_candidate_incident(candidate["id"], "A", "Ольга") is None
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved[2].update(productName="Зеркальце", submitDate=iso(NOW - timedelta(minutes=60)))
    write(path, saved)
    assert store.poll()
    assert store.snapshot()["outage_candidates"] == []
