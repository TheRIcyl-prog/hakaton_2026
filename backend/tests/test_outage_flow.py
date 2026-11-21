import json

from fastapi.testclient import TestClient

from backend.app.itsm import Itsm

from .conftest import make, write


def test_cross_product_outage_through_api_and_restart(make_store, rules, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "rules")
    from backend.app import main

    related = [
        make(f"INC{i:03}", priority_id=3 if i == 1 else 2, productName=product,
             productMasterId="BI_SHARED", submitDate=f"2026-11-10T10:0{i}:00Z")
        for i, product in enumerate(("Зеркальце", "Ковёр", "Скатерть"), 1)
    ]
    unrelated = make("INC004", productName="Избушка", productMasterId="BI_OTHER",
                     submitDate="2026-11-10T10:02:00Z")
    records = [*related, unrelated]
    store, path = make_store(records)

    def use_store(current):
        monkeypatch.setattr(main, "store", current)
        monkeypatch.setattr(main, "itsm", Itsm(path, rules, current.clock.now, store=current))

    use_store(store)
    client = TestClient(main.app)
    initial = client.get("/api/snapshot").json()
    [candidate] = initial["outage_candidates"]
    members = {record["id"] for record in related}
    assert set(candidate["incident_ids"]) == members
    assert initial["outages"] == []

    # The coordinator saw the previous version. An external edit must survive the attempt to confirm it.
    related[0]["header"] = "Новое описание после проверки инженером"
    write(path, records)
    response = client.post("/api/outages/confirm", json={"candidate_id": candidate["id"], "by": "Ольга"})
    assert response.status_code == 409, response.text
    assert json.loads(path.read_text(encoding="utf-8")) == records

    store.poll()
    [current] = client.get("/api/snapshot").json()["outage_candidates"]
    response = client.post("/api/outages/confirm", json={"candidate_id": current["id"], "by": "Ольга"})
    assert response.status_code == 200, response.text
    outage = response.json()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert {item["id"] for item in saved if item.get("outage", {}).get("id") == outage["id"]} == members

    restarted, _ = make_store(saved)
    use_store(restarted)
    snapshot = client.get("/api/snapshot").json()
    assert snapshot["groups"] == initial["groups"]
    assert snapshot["counts"] == initial["counts"]
    assert snapshot["outage_candidates"] == []
    assert [item["id"] for item in snapshot["outages"]] == [outage["id"]]
    detail = client.get(f"/api/outages/{outage['id']}")
    assert detail.status_code == 200, detail.text
    assert {item["id"] for item in detail.json()["incidents"]} == members


def test_group_actions_are_atomic_and_skip_completed_members(make_store, rules, monkeypatch):
    from backend.app import main

    link = {"id": "group", "master_id": "INC001", "name": "Сбой", "reason": "Общий компонент", "source": "rules"}
    records = [make("INC001", outage=link), make("INC002", outage=link),
               make("INC003", state_id=7, outage=link), make("INC004", team="КЦ ИТ Сеть")]
    store, path = make_store(records)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "itsm", Itsm(path, rules, store.clock.now, store=store))
    client = TestClient(main.app)
    url = "/api/outages/group/actions"

    def hashes(outage):
        return {i["id"]: i["hash"] for i in outage["incidents"] if i["state_kind"] == "active"}

    def saved():
        return json.loads(path.read_text(encoding="utf-8"))

    previous = hashes(client.get("/api/outages/group").json())
    records[1]["header"] = "Изменено во внешней системе"
    write(path, records)
    claim = {"by": "Ольга", "operation": "claim", "expected_hashes": previous}
    assert client.post(url, json=claim).status_code == 409
    assert saved() == records

    store.poll()
    claim["expected_hashes"] = hashes(client.get("/api/outages/group").json())
    response = client.post(url, json=claim)
    assert response.status_code == 200, response.text
    after_claim = saved()
    assert [i["coordinator"]["name"] for i in after_claim[:2]] == ["Ольга", "Ольга"]
    escalation = {"operation": "escalate", "by": "Иван", "expected_hashes": hashes(response.json())}
    assert client.post(url, json=escalation).status_code == 409
    assert saved() == after_claim

    response = client.post(url, json={**escalation, "by": "Ольга"})
    assert response.status_code == 200, response.text
    assert all(i["workLog"][-1]["escalation_scope"] for i in saved()[:2])
    response = client.post(url, json={"operation": "assign", "team": "КЦ ИТ Сеть", "by": "Ольга",
                                      "expected_hashes": hashes(response.json())})
    assert response.status_code == 200, response.text
    assert all(i["assignedGroupName"] == "КЦ ИТ Сеть" and "coordinator" not in i for i in saved()[:2])
    assert saved()[2:] == records[2:]
