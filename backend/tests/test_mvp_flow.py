from datetime import timedelta


from backend.app import ai
from backend.app.itsm import Itsm

from .conftest import COORD, NOW, make


def test_mvp_two_coordinators_share_one_file_and_work_without_ai(make_store, monkeypatch, tmp_path, coordinator_clients):
    from backend.app import main

    monkeypatch.setenv("LLM_PROVIDER", "off")
    ai.setup(cache_dir=tmp_path)
    records = [make("SHARED", 0, 4, COORD, productMasterId="MVP"),
               make("PEER1", 0, 2, "Team B", productName="Product B", productMasterId="MVP",
                    submitDate=(NOW - timedelta(hours=4)).isoformat(), deadline=(NOW - timedelta(minutes=1)).isoformat()),
               make("PEER2", 0, 2, "Team C", productName="Product C", productMasterId="MVP")]
    store, path = make_store(records)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "itsm", Itsm(path, store.rules, store.clock.now, store=store))
    clients = coordinator_clients(main.app, "Alice", "Bob")
    alice, bob = clients["Alice"], clients["Bob"]
    assert alice.get("/").status_code == alice.get("/wall").status_code == alice.get("/itsm/SHARED").status_code == 200
    before = bob.get("/api/snapshot")
    snap = before.json()
    assert snap["ai_status"]["provider"] == "off" and not snap["incidents"]["SHARED"].get("ai")
    assert snap["alerts"] and len(snap["outage_candidates"]) == 1
    assert set(snap["outage_candidates"][0]["incident_ids"]) == {"SHARED", "PEER1", "PEER2"}
    assert bob.get("/api/snapshot", headers={"If-None-Match": before.headers["etag"]}).status_code == 304
    queue = bob.get("/api/queue").json()["items"]
    assert [item["kind"] for item in queue[:4]] == ["claim", "route", "outage", "deadline"]
    assert queue[1]["target_team"] is None

    url = "/api/incidents/SHARED/claim"
    take = alice.post(url, json={})
    assert take.status_code == 200
    assert bob.post(url, json={}).status_code == 409
    assert not any(i["incident_id"] == "SHARED" for i in bob.get("/api/queue").json()["items"])
    refreshed = bob.get("/api/snapshot", headers={"If-None-Match": before.headers["etag"]})
    assert refreshed.status_code == 200 and refreshed.json()["claims"]["SHARED"]["by"] == "Alice"

    before = path.read_bytes()
    same_team = alice.post("/api/itsm/SHARED/assign", json={"team": COORD.lower(),
                                                        "expected_hash": take.json()["hash"]})
    assert same_team.status_code == 400 and "уже в этой команде" in same_team.json()["detail"]
    assert path.read_bytes() == before

    sent = alice.post("/api/itsm/SHARED/assign", json={"team": "Team B", "expected_hash": take.json()["hash"]})
    assert sent.status_code == 200 and sent.json()["team"] == "Team B"
    assert "SHARED" not in bob.get("/api/snapshot").json()["claims"]
    assert all(i["id"] != "route:SHARED" for i in bob.get("/api/queue").json()["items"])
    current = bob.get("/api/incidents/SHARED").json()
    assert current["itsm_url"] == "/itsm/SHARED" and current["work_log"][-1]["author"] == "Alice"
    done = alice.post("/api/itsm/SHARED/state", json={"state_id": 7,
                                                    "reason": "Постоянное решение", "expected_hash": current["hash"]})
    assert done.status_code == 200
    assert "SHARED" in bob.get("/api/snapshot").json()["groups"][0]["ids"]
    assert not (tmp_path / "feedback.json").exists()
