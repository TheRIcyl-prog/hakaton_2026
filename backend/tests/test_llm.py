"""LLM-слой без сети: фейковый клиент вместо GigaChat."""
import json
import time

import pytest

from backend.app import ai as ai_layer
from backend.app.adapter import adapt_all
from backend.app.ai.llm import LLMWorker, build_prompt, parse_answer
from backend.app.groups import assign_groups

from .conftest import COORD, NOW, make

CANDS = [{"team": "КЦ ДОИ VMWare", "score": 0.6}, {"team": "КЦ ИТ Мой МТС", "score": 0.4}]


def test_parse_answer_only_candidates():
    ok = parse_answer('```json\n{"summary": "Сбой ворса", "route_team": "кц ит мой мтс", "route_reason": "их сервис"}\n```', CANDS)
    assert ok["route_team"] == "КЦ ИТ Мой МТС" and ok["summary"] == "Сбой ворса"
    bad = parse_answer('{"summary": "x", "route_team": "Выдуманная команда"}', CANDS)
    assert bad["route_team"] is None
    with pytest.raises(ValueError):
        parse_answer("не json", CANDS)


def test_prompt_lists_candidates():
    inc = {"id": "A", "title": "t", "hash": "h", "ai": {"risk": {"score": 50, "factors": ["срок"]}}}
    p = build_prompt(inc, CANDS)
    assert "КЦ ДОИ VMWare" in p and "ТОЛЬКО" in p and "50/100" in p


class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def chat(self, messages, **kw):
        self.calls += 1
        return self.answer


def wait_until(pred, timeout=3):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_worker_overlays_llm_answer(tmp_path, rules, monkeypatch):
    answer = json.dumps({"summary": "Ворс ковра сбоит у клиентов", "route_team": "КЦ ИТ Мой МТС",
                         "route_reason": "сервис принадлежит им", "risk_comment": None}, ensure_ascii=False)
    client = FakeClient(answer)
    worker = LLMWorker(client, tmp_path / "cache.json")
    monkeypatch.setattr(ai_layer, "_worker", worker)

    records = [
        make("C", 0, 2, COORD, service="[Летающий ковер] Ворс"),
        make("R1", 7, 2, "КЦ ДОИ VMWare", service="[Летающий ковер] Ворс", completionDate="2026-11-10T11:00:00Z"),
        make("R2", 7, 2, "КЦ ИТ Мой МТС", service="[Летающий ковер] Ворс", completionDate="2026-11-10T11:00:00Z"),
    ]
    incs, _ = adapt_all(records, rules, "{id}")
    assign_groups(incs, rules, NOW)
    ai_layer.enrich(incs, NOW, rules)
    assert incs["C"]["ai"]["pending"] and incs["C"]["ai"]["source"] == "rules"
    assert wait_until(lambda: worker.consume_dirty())

    ai_layer.enrich(incs, NOW, rules)
    a = incs["C"]["ai"]
    assert a["source"] == "gigachat" and a["summary"] == "Ворс ковра сбоит у клиентов"
    assert a["route"]["team"] == "КЦ ИТ Мой МТС" and a["route"]["reason"] == "сервис принадлежит им"
    assert (tmp_path / "cache.json").exists()
    assert client.calls == 1  # R1/R2 решены и не в группах — их не отправляем


def test_worker_error_keeps_rules(tmp_path, rules, monkeypatch):
    class Broken:
        def chat(self, *a, **k):
            raise RuntimeError("нет сети")

    worker = LLMWorker(Broken(), tmp_path / "cache.json")
    monkeypatch.setattr(ai_layer, "_worker", worker)
    incs, _ = adapt_all([make("K", 0, 4)], rules, "{id}")
    assign_groups(incs, rules, NOW)
    ai_layer.enrich(incs, NOW, rules)
    assert wait_until(lambda: worker.status["state"] == "error")
    assert incs["K"]["ai"]["source"] == "rules" and incs["K"]["ai"]["summary"]
