import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import ai
from backend.app.ai.llm import LLMWorker

from .conftest import COORD, make
from .test_llm import run, wait_until


def shown_incident():
    return {
        "id": "INC-FEEDBACK", "product": "Частное название продукта", "service": "Частное имя сервиса",
        "scenario": "Оплата", "description": "Текст инцидента Иван Петров", "initiator": "Иван Петров",
        "ai": {"route": {"team": "КЦ Ступа", "reason": "Текст маршрута Иван Петров"},
               "summary": "Текст выжимки Иван Петров", "risk": {"score": 70, "comment": "Текст риска Иван Петров"},
               "dynamics": {"text": "Текст динамики Иван Петров", "patterns": []}},
    }


def test_feedback_validation_privacy_and_dedup_survive_reload(tmp_path):
    path = tmp_path / "cache" / "ai_feedback.json"
    ai.reset(feedback_path=path)
    inc = shown_incident()
    for kind in ("route", "summary", "risk", "dynamics"):
        team = "КЦ Леший" if kind == "route" else None
        assert ai.record_feedback(inc, "  Мария Иванова  ", kind, team) is True
        assert ai.record_feedback(inc, "мария иванова", kind, team) is False
    assert ai.consume_dirty() and not ai.consume_dirty()

    stored = path.read_text(encoding="utf-8")
    rows = {row["kind"]: row for row in json.loads(stored)}
    assert set(rows) == {"route", "summary", "risk", "dynamics"}
    assert all(row["incident_id"] == inc["id"] and len(row["signature"]) == 64 and row["at"] for row in rows.values())
    assert rows["route"]["old"]["team"] == "КЦ Ступа" and rows["route"]["corrected_team"] == "КЦ Леший"
    assert rows["risk"]["old"]["score"] == 70
    assert all(len(rows[kind]["old"]["fingerprint"]) == 64 for kind in ("summary", "risk", "dynamics"))
    assert all(secret not in stored for secret in ("Иван Петров", "Мария Иванова", "Текст", inc["product"], inc["service"]))

    ai.reset(feedback_path=path)
    assert not ai.record_feedback(inc, "Мария Иванова", "route", "КЦ Леший")
    for by, kind, team in [("", "summary", None), ("Мария", "outage", None), ("Мария", "route", None),
                           ("Мария", "route", "КЦ Ступа"), ("Мария", "risk", "КЦ Леший")]:
        with pytest.raises(ValueError):
            ai.record_feedback(inc, by, kind, team)
    with pytest.raises(ValueError):
        ai.record_feedback({**inc, "ai": {}}, "Мария", "summary")
    assert not ai.consume_dirty() and path.read_text(encoding="utf-8") == stored

    assert ai.record_feedback(inc, "Мария Иванова", "route", "КЦ Избушка")
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert [row["corrected_team"] for row in rows if row["kind"] == "route"] == ["КЦ Избушка"]


def test_write_failure_keeps_previous_disk_and_memory(tmp_path, monkeypatch):
    path = tmp_path / "ai_feedback.json"
    ai.reset(feedback_path=path)
    inc = shown_incident()
    assert ai.record_feedback(inc, "Мария", "route", "КЦ Леший")
    assert ai.consume_dirty()
    saved = path.read_bytes()

    def fail_replace(self, target):
        raise OSError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError):
            ai.record_feedback(inc, "Мария", "route", "КЦ Избушка")

    assert path.read_bytes() == saved
    assert not ai.consume_dirty()
    assert not ai.record_feedback(inc, "Мария", "route", "КЦ Леший")
    assert list(tmp_path.iterdir()) == [path]


def records(incident_id):
    return [make(incident_id, team=COORD), *[make(f"S{i}", 7, team="КЦ Ступа") for i in range(3)],
            make("L", 7, team="КЦ Леший")]


def test_route_uses_matching_corrections_and_keeps_history_denominator(tmp_path, rules):
    path = tmp_path / "ai_feedback.json"
    ai.reset(feedback_path=path)
    original = run(rules, records("ORIGINAL"))["ORIGINAL"]
    assert original["ai"]["route"]["team"] == "КЦ Ступа"
    ai.record_feedback(original, "Ольга", "route", "КЦ Леший")
    ai.record_feedback(original, "Иван", "route", "КЦ Леший")
    ai.record_feedback(original, "Мария", "route", "КЦ Избушка")
    assert ai.consume_dirty()
    ai.reset(feedback_path=path)

    original_again = run(rules, records("ORIGINAL"))["ORIGINAL"]["ai"]["route"]
    assert original_again["team"] == "КЦ Ступа" and "feedback" not in original_again
    future = run(rules, records("FUTURE"))["FUTURE"]["ai"]["route"]
    assert future["team"] == "КЦ Леший"
    assert future["confidence"] == 0.25 and future["confidence_text"] == "1 из 4 похожих"
    assert future["feedback"] == {"count": 2, "total": 3, "by_team": {"КЦ Избушка": 1, "КЦ Леший": 2}}
    assert "2 из 3 исправлений" in future["reason"]
    other = records("OTHER")
    other[0]["scenarioName"] = "Другая операция"
    unrelated = run(rules, other)["OTHER"]["ai"]["route"]
    assert unrelated["team"] == "КЦ Ступа" and "feedback" not in unrelated


def test_llm_receives_only_team_pairs_and_keeps_feedback_evidence(tmp_path, rules):
    path = tmp_path / "ai_feedback.json"
    ai.reset(feedback_path=path)
    original = run(rules, records("LOCAL_ID"))["LOCAL_ID"]
    ai.record_feedback(original, "Частное имя координатора", "route", "КЦ Леший")

    class Client:
        def chat(self, messages):
            self.prompt = messages[-1]["content"]
            return json.dumps({"summary": "Выжимка", "route_team": "КЦ Леший", "route_reason": "Профильная команда"})

    client = Client()
    worker = LLMWorker(client, tmp_path / "cache.json")
    ai.reset(worker=worker, feedback_path=path)
    run(rules, records("FUTURE"))
    assert wait_until(lambda: worker.snapshot_status()["done"] == 1)
    route = run(rules, records("FUTURE"))["FUTURE"]["ai"]["route"]
    assert route["source"] == "gigachat" and route["team"] == "КЦ Леший"
    assert all(text in route["reason"] for text in ("Профильная команда", "1 из 4 похожих", "1 из 1 исправлений"))
    assert '"suggested_team": "КЦ Ступа", "corrected_team": "КЦ Леший"' in client.prompt
    assert "Частное имя координатора" not in client.prompt and "LOCAL_ID" not in client.prompt
    for private in json.loads(path.read_text(encoding="utf-8"))[0].values():
        if isinstance(private, str) and len(private) >= 32:
            assert private not in client.prompt


def test_feedback_api_rejects_stale_suggestion_and_applies_valid_correction(make_store, tmp_path, monkeypatch):
    from backend.app import main

    ai.reset(feedback_path=tmp_path / "ai_feedback.json")
    store, _ = make_store(records("ORIGINAL"))
    monkeypatch.setattr(main, "store", store)
    client = TestClient(main.app)
    inc = client.get("/api/incidents/ORIGINAL").json()
    url = "/api/incidents/ORIGINAL/ai-feedback"
    body = {"by": "Ольга", "kind": "route", "expected_hash": inc["hash"],
            "expected_team": inc["ai"]["route"]["team"], "corrected_team": "КЦ Леший"}

    assert client.post(url, json={**body, "expected_hash": "old"}).status_code == 409
    assert client.post(url, json={**body, "expected_team": "old"}).status_code == 409
    assert client.post(url, json={**body, "corrected_team": "Неизвестная"}).status_code == 400
    assert client.post(url, json=body).json() == {"ok": True, "saved": True}
    assert client.post(url, json=body).json() == {"ok": True, "saved": False}
    future, _ = make_store(records("FUTURE"))
    assert future.incident("FUTURE")["ai"]["route"]["team"] == "КЦ Леший"
