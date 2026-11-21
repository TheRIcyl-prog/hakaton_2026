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
