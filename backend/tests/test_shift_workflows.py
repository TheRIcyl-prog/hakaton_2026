import json
from datetime import timedelta

from fastapi.testclient import TestClient

from backend.app.handoffs import Handoffs
from backend.app.itsm import Itsm

from .conftest import FakeClock, make, write


def connect(store, path, rules, monkeypatch, coordinator_clients):
    from backend.app import main

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "itsm", Itsm(path, rules, store.clock.now, store=store))
    return coordinator_clients(main.app, "Ольга", "Иван", "Анна")


def test_outage_growth_checks_current_data_and_updates_wall(make_store, rules, monkeypatch, coordinator_clients):
    link = {"id": "group", "master_id": "A", "name": "Оплата", "reason": "Общий узел", "source": "rules"}
    store, path = make_store([make("A", outage=link), make("B", outage=link)])
    clients = connect(store, path, rules, monkeypatch, coordinator_clients)
    author, colleague = clients["Ольга"], clients["Иван"]
    url = "/api/outages/group"
    assert author.get(url).json()["growth_proposals"] == []
    records = json.loads(path.read_text(encoding="utf-8"))
    records.extend([
        make("C", productName="Кабинет", productMasterId="BI_OTHER", causeIncidentId="A",
             deadline="2026-11-10T12:20:00Z"),
        make("D", productName="Оплата", submitDate="2026-11-10T11:00:00Z"),
        make("E", productMasterId="BI_UNRELATED"),
    ])
    write(path, records)
    store.poll()
    detail = author.get(url).json()
    proposals = {row["id"]: row for row in detail["growth_proposals"]}
    assert set(proposals) == {"C", "D"}
    assert "A" in proposals["C"]["reason"]
    body = {"expected_hash": proposals["C"]["hash"], "expected_incident_ids": detail["incident_ids"]}
    assert author.post(f"{url}/incidents/C", json=body).status_code == 200
    assert colleague.post(f"{url}/incidents/C", json=body).status_code == 409
    from backend.app import main

    wall = TestClient(main.app).get("/api/snapshot").json()
    [outage] = wall["outages"]
    assert set(outage["incident_ids"]) == {"A", "B", "C"}
    assert "Кабинет" in outage["products"]
    assert outage["deadline"] == "2026-11-10T12:20:00Z"
    assert outage["last_growth_at"]
    assert "growth_proposals" not in outage
    member = colleague.get("/api/incidents/C").json()
    assert member["work_log"][-1]["author"] == "Ольга"
    remove = {"expected_hash": member["hash"], "expected_incident_ids": outage["incident_ids"]}
    assert colleague.request("DELETE", f"{url}/incidents/C", json=remove).status_code == 200
    assert [row["id"] for row in author.get(url).json()["growth_proposals"]] == ["D"]


def test_escalation_receipt_and_overdue_followup(make_store, rules, monkeypatch, coordinator_clients):
    clock = FakeClock()
    store, path = make_store([make("RISK", priority_id=4, coordinator={"name": "Ольга"})], clock)
    clients = connect(store, path, rules, monkeypatch, coordinator_clients)
    author, colleague = clients["Ольга"], clients["Иван"]
    api = "/api/itsm/RISK"
    result = author.post(f"{api}/escalate", json={"expected_hash": store.incident("RISK")["hash"]})
    assert result.status_code == 200, result.text
    pending = result.json()
    data = {"expected_hash": pending["hash"], "request_id": pending["escalation_receipt"]["id"]}
    ack = f"{api}/escalation/acknowledge"
    assert author.post(ack, json=data).status_code == 403

    def queue_kinds(client):
        return {row["kind"] for row in client.get("/api/queue").json()["items"]}

    assert "escalation_ack" in queue_kinds(colleague)
    assert "escalation_followup" not in queue_kinds(author)
    clock._now += timedelta(minutes=15)
    assert "escalation_followup" in queue_kinds(author)
    accepted = colleague.post(ack, json=data)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["escalation_receipt"]["accepted_by"] == "Иван"
    assert "escalation_followup" not in queue_kinds(author)
    assert "escalation_ack" not in queue_kinds(colleague)
    assert colleague.post(ack, json=data).status_code == 409


def test_handoff_persists_snapshot_and_only_recipient_can_accept(
        make_store, rules, monkeypatch, coordinator_clients, tmp_path):
    store, path = make_store([make("CRIT", priority_id=4), make("ROUTINE")])
    handoff_file = tmp_path / "state" / "handoffs.json"
    store.handoffs = Handoffs(handoff_file)
    clients = connect(store, path, rules, monkeypatch, coordinator_clients)
    author, receiver, other = clients["Ольга"], clients["Иван"], clients["Анна"]
    url = "/api/handoffs"
    get = author.get(url).json()
    assert [row["id"] for row in get["preview"]["incidents"]] == ["CRIT"]
    recipient = next(row["id"] for row in get["recipients"] if row["name"] == "Иван")
    body = {"recipient_id": recipient, "note": "Запросить ETA"}
    result = author.post(url, json=body)
    assert result.status_code == 200, result.text
    sent = result.json()
    assert author.post(url, json=body).json()["id"] == sent["id"]
    assert other.get(url).json()["items"] == []
    store.handoffs = Handoffs(handoff_file)
    write(path, [make("CRIT", state_id=7), make("ROUTINE")])
    store.poll()
    incoming = receiver.get(url).json()
    assert incoming["preview"]["incidents"] == []
    assert incoming["items"][0]["incidents"] == sent["incidents"]
    ack = f"{url}/{sent['id']}/acknowledge"
    assert author.post(ack).status_code == 404
    assert other.post(ack).status_code == 404
    result = receiver.post(ack)
    assert result.status_code == 200, result.text
    accepted_at = result.json()["accepted_at"]
    assert accepted_at
    assert receiver.post(ack).json()["accepted_at"] == accepted_at
    assert author.get(url).json()["items"][0]["accepted_at"] == accepted_at
