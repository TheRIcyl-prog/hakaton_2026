import json
from datetime import timedelta


from backend.app.adapter import iso
from backend.app.itsm import Itsm
from backend.app.queue_preferences import QueuePreferences
from scripts import make_demo

from .conftest import COORD, NOW, FakeClock, make, write


def queue_ids(store, by=""):
    return [item["id"] for item in store.queue(by)]


def test_queue_order_visibility_and_release_for_two_coordinators(make_store):
    records = [
        make("NEWER", team=COORD, lastAssignmentToTeamDateTime=iso(NOW - timedelta(minutes=5))),
        make("OLDER", team=COORD, lastAssignmentToTeamDateTime=iso(NOW - timedelta(minutes=55))),
        make("CRITICAL", priority_id=4, deadline=iso(NOW - timedelta(minutes=5))),
        make("IVAN", team=COORD, coordinator={"name": "Иван", "at": iso(NOW)}),
    ]
    store, path = make_store(records)
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    assert queue_ids(store)[:3] == ["claim:CRITICAL", "route:OLDER", "route:NEWER"]
    assert "route:IVAN" not in queue_ids(store, "Ольга")
    assert "route:IVAN" in queue_ids(store, " Иван ")
    assert "route:IVAN" not in queue_ids(store)
    assert "route:IVAN" in {action["id"] for action in store.snapshot()["actions"]}

    itsm.take("CRITICAL", "Ольга")
    assert "claim:CRITICAL" not in queue_ids(store, "Ольга")
    assert "escalate:CRITICAL" in queue_ids(store, "ольга")
    assert "escalate:CRITICAL" not in queue_ids(store, "Иван")
    assert "escalate:CRITICAL" not in queue_ids(store)
    itsm.release("CRITICAL", "Ольга")
    assert queue_ids(store, "Иван")[0] == "claim:CRITICAL"
    assert queue_ids(store, "Ольга")[0] == "claim:CRITICAL"


def test_queue_preferences_api_are_private_and_persist_until_expiry(make_store, tmp_path, monkeypatch, coordinator_clients):
    monkeypatch.setenv("LLM_PROVIDER", "rules")
    from backend.app import main

    store, path = make_store([make("CRITICAL", priority_id=4), make("ROUTE", team=COORD)])
    monkeypatch.setattr(main, "store", store)
    wall_clock = FakeClock()
    preferences_path = tmp_path / "queue_preferences.json"
    store.queue_preferences = QueuePreferences(preferences_path, wall_clock.now)
    clients = coordinator_clients(main.app, "Ольга", "Иван")
    client = clients["Ольга"]

    def items(by):
        response = clients[by].get("/api/queue")
        assert response.status_code == 200
        return {item["id"]: item for item in response.json()["items"]}

    by_olga = items("Ольга")
    claim = by_olga["claim:CRITICAL"]
    assert claim["outage_id"] is None
    assert set(claim["incident"]) == {"id", "title", "product", "deadline", "priority_level", "itsm_url"}
    assert claim["incident"]["id"] == claim["incident_id"] == "CRITICAL"
    before = path.read_bytes()
    for operation, item_id in [("snooze", "claim:CRITICAL"), ("dismiss", "route:ROUTE")]:
        response = client.post("/api/queue/preferences", json={"id": item_id, "operation": operation})
        assert response.status_code == 200 and response.json() == {"ok": True}
    assert path.read_bytes() == before
    assert preferences_path.is_file()
    assert {"claim:CRITICAL", "route:ROUTE"}.isdisjoint(items("Ольга"))
    assert {"claim:CRITICAL", "route:ROUTE"} <= items("Иван").keys()
    assert {"claim:CRITICAL", "route:ROUTE"} <= set(queue_ids(store))

    store.queue_preferences = QueuePreferences(preferences_path, wall_clock.now)
    wall_clock._now = NOW + timedelta(minutes=15, microseconds=-1)
    assert "claim:CRITICAL" not in items("Ольга")
    wall_clock._now = NOW + timedelta(minutes=15)
    assert "claim:CRITICAL" in items("Ольга")
    assert "route:ROUTE" not in items("Ольга")


def test_outage_queue_item_uses_candidate_id_and_detects_foreign_participants(make_store):
    records = [make(inc_id, productName=product, productMasterId="BI_SHARED", submitDate=iso(NOW - timedelta(minutes=i)))
               for i, (inc_id, product) in enumerate([("A", "Ковёр"), ("B", "Избушка"), ("C", "Зеркальце")], 1)]
    store, path = make_store(records)
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    [candidate] = store.snapshot()["outage_candidates"]
    [item] = [row for row in store.queue() if row["kind"] == "outage"]
    assert item["id"] == f"outage:{candidate['id']}"
    assert item["outage_id"] == candidate["id"]
    assert item["incident"] is None and item["incident_id"] is None

    itsm.take("A", "Ольга")
    assert not any(item["kind"] == "outage" for item in store.queue("Иван"))
    assert any(item["kind"] == "outage" for item in store.queue("Ольга"))
    itsm.release("A", "Ольга")
    assert any(item["kind"] == "outage" for item in store.queue())


def test_confirmed_escalation_hides_while_relevant(make_store):
    record = make("CRITICAL", priority_id=4, deadline=iso(NOW - timedelta(minutes=4)))
    store, path = make_store([record])
    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    assert "escalate:CRITICAL" in queue_ids(store, "Ольга")
    itsm.escalate("CRITICAL", "Ольга", expected_hash=store.incident("CRITICAL")["hash"])
    itsm.comment("CRITICAL", "Иван", "Проверяю")
    assert "escalate:CRITICAL" not in queue_ids(store, "Ольга")
    assert "escalate:CRITICAL" not in queue_ids(store, "Иван")
    assert "escalate:CRITICAL" in {action["id"] for action in store.snapshot()["actions"]}
    assert "_escalation" not in store.incident("CRITICAL")

    records = json.loads(path.read_text(encoding="utf-8"))
    records[0]["deadline"] = iso(NOW - timedelta(minutes=9))
    write(path, records)
    assert store.poll()
    assert "escalate:CRITICAL" in queue_ids(store, "Иван")
    itsm.escalate("CRITICAL", "Иван")
    itsm.set_state("CRITICAL", 7, "Иван")
    itsm.set_state("CRITICAL", 2, "Иван")
    assert "escalate:CRITICAL" in queue_ids(store, "Ольга")


def test_labels_and_deadline_are_independent_of_external_wait(make_store):
    history = make("PAST", state_id=7, completionDate=iso(NOW - timedelta(days=2)))
    changes = [{"at": iso(NOW - timedelta(hours=3, minutes=i)), "action": "assign", "kind": "team",
                "before": f"КЦ {i}", "after": f"КЦ {i - 1}"} for i in (3, 2, 1)]
    waiting = make("WAIT", priority_id=3, stateReason="Внешняя обработка", serviceType="Клиентский",
                   isNeedUp=True, deadline=iso(NOW - timedelta(minutes=1)), workLog=changes)
    aftermath = make("DONE", state_id=20, isNeedUp=True)
    store, _ = make_store([waiting, history, aftermath])
    labels = ["waiting_external", "aftermath", "clients_affected", "reassigned", "recurrence"]
    assert store.snapshot()["incidents"]["WAIT"]["labels"] == labels
    assert store.search("WAIT")["items"][0]["labels"] == labels
    assert store.incident("WAIT")["labels"] == labels
    assert store.incident("WAIT")["forecast"]["level"] == "overdue"
    assert "escalate:WAIT" in queue_ids(store)
    assert "aftermath:WAIT" not in queue_ids(store)
    assert "aftermath:DONE" in queue_ids(store)
    assert "recurrence:WAIT" in queue_ids(store)


def test_demo_label_examples_are_reachable_in_queue_and_groups(make_store):
    store, _ = make_store(make_demo.build(NOW))
    snap = store.snapshot()
    for number, label in [("41", "waiting_external"), ("42", "aftermath"), ("43", "reassigned"),
                          ("44", "clients_affected")]:
        assert label in snap["incidents"][f"INC0000300000{number}"]["labels"]
    ids = queue_ids(store)
    assert "deadline:INC000030000041" in ids
    assert "aftermath:INC000030000042" in ids
    assert "dynamics:INC000030000043" in ids
    assert "recurrence:INC000030000031" in ids
