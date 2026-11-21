import json

from backend.app.itsm import Itsm
from scripts import make_demo

from .conftest import NOW, make


def test_explicit_chain_groups_across_products_and_requires_confirmation(make_store):
    records = [
        make("ROOT", priority_id=3, productName="Ковёр", productMasterId="BI_ROOT"),
        make("E1", causeIncidentId=" ROOT ", productName="Избушка", productMasterId="BI_E1"),
        make("E2", causeIncidentId="E1", productName="Скатерть", productMasterId="BI_E2"),
        make("UNLINKED", isEffect=True, isReason=True, productName="Ковёр", productMasterId="BI_ROOT"),
    ]
    store, path = make_store(records)
    snapshot = store.snapshot()
    [candidate] = snapshot["outage_candidates"]
    assert candidate["incident_ids"] == ["ROOT", "E1", "E2"]
    assert candidate["source"] == "rules" and candidate["active_count"] == 3
    assert snapshot["outages"] == []
    assert store.incident("E1")["cause_incident_id"] == "ROOT"
    assert "cause_incident_id" not in store.incident("UNLINKED")
    assert any(item["outage_id"] == candidate["id"] for item in store.queue("Ольга"))

    itsm = Itsm(path, store.rules, store.clock.now, store=store)
    result = itsm.confirm_outage(candidate["id"], "Ольга")
    assert result["incident_ids"] == candidate["incident_ids"]
    assert store.snapshot()["outage_candidates"] == []
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[1]["causeIncidentId"] == " ROOT "
    assert saved[2]["outage"]["master_id"] == "ROOT"
    assert "outage" not in saved[3]


def test_cycles_missing_and_inactive_parents_do_not_produce_chains(make_store):
    records = [make("ROOT"), make("VALID", causeIncidentId="ROOT"),
               make("A", causeIncidentId="B"), make("B", causeIncidentId="A"),
               make("AB-CHILD", causeIncidentId="A"), make("SELF", causeIncidentId="SELF"),
               make("MISSING", causeIncidentId="UNKNOWN"), make("MISSING-CHILD", causeIncidentId="MISSING"),
               make("DONE", state_id=7), make("DONE-CHILD", causeIncidentId="DONE")]
    store, _ = make_store(records)
    [candidate] = store.snapshot()["outage_candidates"]
    assert candidate["incident_ids"] == ["ROOT", "VALID"]


def test_demo_chain_keeps_bi_901_and_recurrence(make_store):
    store, _ = make_store(make_demo.build(NOW))
    chain_ids = [f"INC0000300000{i}" for i in (51, 52, 53)]
    candidates = [item["incident_ids"] for item in store.snapshot()["outage_candidates"]]
    assert chain_ids in candidates
    assert [f"INC0000300000{i}" for i in (21, 22, 23)] in candidates
    assert len({store.incident(inc_id)["product_code"] for inc_id in chain_ids}) == 3
    assert store.incident("INC000030000031")["recurrence"]["cases"][0]["id"] == "INC000030000032"
