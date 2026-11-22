from fastapi import FastAPI

from backend.app.auth import router as auth_router
from backend.app.itsm import Itsm
from backend.app.outage_api import outage_action_router

from .conftest import make, write


def test_outage_candidate_detail_includes_members_outside_snapshot_and_rejects_old_id(make_store, coordinator_clients):
    records = [make("ROOT", priority_id=3, productName="Избушка"),
               make("CHILD", causeIncidentId="ROOT", productName="Ковёр")]
    store, path = make_store(records)
    snapshot = store.snapshot()
    [candidate] = snapshot["outage_candidates"]
    assert "CHILD" not in snapshot["incidents"]

    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(outage_action_router(lambda: itsm))
    client = coordinator_clients(app, "Ольга")["Ольга"]
    response = client.get(f"/api/outage-candidates/{candidate['id']}")
    assert response.status_code == 200
    assert response.json() == {
        **candidate,
        "incidents": [
            {"id": "ROOT", "title": "Сбой ROOT", "product": "Избушка"},
            {"id": "CHILD", "title": "Сбой CHILD", "product": "Ковёр", "cause_incident_id": "ROOT"},
        ],
    }

    records[1]["header"] = "Обновлённое следствие"
    write(path, records)
    assert store.poll()
    assert client.get(f"/api/outage-candidates/{candidate['id']}").status_code == 404
    assert client.get("/api/outage-candidates/unknown").status_code == 404
    [updated] = store.snapshot()["outage_candidates"]
    response = client.get(f"/api/outage-candidates/{updated['id']}")
    assert response.status_code == 200 and response.json()["incidents"][1]["title"] == "Обновлённое следствие"
