"""Фоновый GigaChat без сети: фейковый клиент вместо настоящего."""
import json
import re
import threading
import time

import pytest

from backend.app import ai
from backend.app.adapter import adapt_all
from backend.app.ai.llm import (LLMWorker, build_incident_prompt, incident_fields, parse_dup_answer,
                                parse_incident_answer)
from backend.app.groups import assign_groups

from .conftest import COORD, NOW, make

SVC = "[Летающий ковер] Ворс"
CANDS = [{"team": "КЦ Ступа", "count": 3, "solved": 3, "score": 0.6},
         {"team": "КЦ Леший", "count": 2, "solved": 1, "score": 0.4}]


def wait_until(pred, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class FakeClient:
    """Отвечает как GigaChat: по инциденту — выбирает ВТОРОГО кандидата; по дублям — «да, один сбой»."""

    def __init__(self, delay=0.0):
        self.prompts: list[str] = []
        self.delay = delay
        self.lock = threading.Lock()

    @property
    def calls(self):
        return len(self.prompts)

    def chat(self, messages, **kw):
        prompt = messages[-1]["content"]
        with self.lock:
            self.prompts.append(prompt)
        if self.delay:
            time.sleep(self.delay)
        if "один и тот же сбой" in prompt:
            return json.dumps({"same": True, "name": "Ковёр не взлетает у всех", "reason": "одинаковые симптомы",
                               "exclude": []}, ensure_ascii=False)
        teams = re.findall(r"^- (.+?): вела", prompt, re.M)
        return "```json\n" + json.dumps({
            "summary": "Ковёр-самолёт не взлетает у клиентов королевства",
            "route_team": teams[1] if len(teams) > 1 else None,
            "route_reason": "Эта команда чинила такие же сбои ворса.",
            "risk_comment": "Срок уже сорван, клиенты ждут.",
            "dynamics_comment": "Приоритет растёт — похоже на начало массового сбоя.",
        }, ensure_ascii=False) + "\n```"


def setup_worker(tmp_path, client, **kw):
    w = LLMWorker(client, tmp_path / "ai_cache.json", **kw)
    ai.reset(worker=w)
    return w


def run(rules, records, now=NOW):
    incs, _ = adapt_all(records, rules, "{id}")
    assign_groups(incs, rules, now)
    ai.enrich(incs, now, rules)
    return incs


def coord_records():
    recs = [make("C", 0, 2, COORD, service=SVC, assignedEmployee="Баба Яга (byaga)",
                 initiatorName="Кощей Бессмертный (koschei)",
                 description="Звонил Кощей Бессмертный (koschei), тел. +7 999 123-45-67: ковёр не взлетает.")]
    recs += [make(f"S{i}", 7, 2, "КЦ Ступа", service=SVC, completionDate="2026-11-10T11:00:00Z") for i in range(3)]
    recs += [make(f"L{i}", 7, 2, "КЦ Леший", service=SVC, completionDate="2026-11-10T11:00:00Z") for i in range(2)]
    return recs


def test_parse_answer_only_candidates_and_short_summary():
    parts = {"summary": True, "route": {"cands": CANDS, "total": 5}}
    ok = parse_incident_answer('{"summary": "' + "очень длинная выжимка " * 10 + '", "route_team": "кц леший"}', parts)
    assert ok["route"]["team"] == "КЦ Леший" and len(ok["summary"]["text"]) <= 90
    bad = parse_incident_answer('{"summary": "x", "route_team": "Выдуманная команда"}', parts)
    assert bad["route"] is None
    with pytest.raises(ValueError):
        parse_incident_answer("не json", parts)
    plain = parse_incident_answer('"Золотая рыбка зависла при подтверждении платежа"', {"summary": True})
    assert plain["summary"]["text"] == "Золотая рыбка зависла при подтверждении платежа"
    braces = parse_incident_answer('"summary": "Сказки не запускаются", "risk_comment": "Проверить серверы видео."',
                                   {"summary": True, "risk": {}})
    assert braces["summary"]["text"] == "Сказки не запускаются" and braces["risk"]["comment"] == "Проверить серверы видео."
    d = parse_dup_answer('{"same": "да", "name": "Сбой", "exclude": ["X2", "чужой"]}', ["X1", "X2"])
    assert d["same"] and d["exclude"] == ["X2"]


def test_prompt_has_no_people(rules):
    incs, _ = adapt_all(coord_records(), rules, "{id}")
    fields = incident_fields(incs["C"])
    prompt = build_incident_prompt(fields, {"summary": True, "route": {"cands": CANDS, "total": 5}})
    for secret in ("Баба Яга", "byaga", "Кощей", "koschei", "999"):
        assert secret not in prompt
    assert "КЦ Ступа" in prompt and "ТОЛЬКО" in prompt


def test_worker_overlays_answer_and_confidence_from_history(tmp_path, rules):
    client = FakeClient()
    w = setup_worker(tmp_path, client)
    incs = run(rules, coord_records())
    a = incs["C"]["ai"]
    assert a["pending"] and a["source"] == "rules" and a["route"]["team"] == "КЦ Ступа"
    assert wait_until(lambda: w.snapshot_status()["done"] >= 1 and not w.in_flight)

    incs = run(rules, coord_records())
    a = incs["C"]["ai"]
    assert a["source"] == "gigachat" and not a["pending"]
    assert a["summary"] == "Ковёр-самолёт не взлетает у клиентов королевства"
    assert a["summary_meta"]["source"] == "gigachat" and a["summary_meta"]["at"]
    r = a["route"]
    # GigaChat выбрал «КЦ Леший», но уверенность — по истории этой команды, а не со слов модели
    assert r["team"] == "КЦ Леший" and r["confidence_text"] == "2 из 5 похожих" and r["confidence"] == 0.4
    assert r["rules_team"] == "КЦ Ступа" and r["reason"].startswith("Эта команда")
    assert [x["team"] for x in r["alternatives"]] == ["КЦ Ступа"]
    assert r["source"] == "gigachat"
    assert client.calls == 1     # решённые S*/L* не в группах — их не отправляем
    w.save(force=True)
    assert (tmp_path / "ai_cache.json").exists()

    # «Перезапуск»: новый воркер читает кэш с диска и не тратит запросы
    client2 = FakeClient()
    setup_worker(tmp_path, client2)
    a = run(rules, coord_records())["C"]["ai"]
    assert a["source"] == "cache" and a["route"]["team"] == "КЦ Леший" and not a["pending"]
    time.sleep(0.1)
    assert client2.calls == 0


def test_offline_keeps_rules_and_retries(tmp_path, rules):
    class Flaky(FakeClient):
        broken = True

        def chat(self, messages, **kw):
            if self.broken:
                raise RuntimeError("нет сети")
            return super().chat(messages, **kw)

    client = Flaky()
    w = setup_worker(tmp_path, client, retry_seconds=0.3)
    incs = run(rules, [make("K", 0, 4)])
    assert wait_until(lambda: w.snapshot_status()["state"] == "offline")
    st = ai.status()
    assert "нет связи" in st["label"] and st["next_retry_at"] and st["queue"] == 1
    assert incs["K"]["ai"]["source"] == "rules" and incs["K"]["ai"]["summary"]
    client.broken = False                     # сеть вернулась — задача из очереди уходит сама
    assert wait_until(lambda: w.snapshot_status()["state"] == "ok" and w.snapshot_status()["done"] == 1)
    assert run(rules, [make("K", 0, 4)])["K"]["ai"]["source"] == "gigachat"


def test_queue_order_critical_first(tmp_path, rules):
    gate = threading.Event()

    class Gated(FakeClient):
        def chat(self, messages, **kw):
            gate.wait(2)
            return super().chat(messages, **kw)

    client = Gated()
    w = setup_worker(tmp_path, client, concurrency=1)
    recs = [make(t, s, p, team, header=h, service=f"[Ковёр] {h}", productName=h, productMasterId=f"BI_{t}")
            for t, s, p, team, h in [("HI", 2, 3, "КЦ Ступа", "Высокий"), ("RS", 20, 2, "КЦ Ступа", "Исследование"),
                                     ("CO", 0, 2, COORD, "Координаторы"), ("CR", 2, 4, "КЦ Ступа", "Критический")]]
    run(rules, recs)
    gate.set()
    assert wait_until(lambda: client.calls == 4)
    first = [re.search(r'"заголовок": "(.+?)"', p).group(1) for p in client.prompts]
    # первый запрос мог уйти до сортировки; дальше строго по важности
    assert first[1:] == [t for t in ["Критический", "Координаторы", "Высокий", "Исследование"] if t != first[0]]
    assert w.snapshot_status()["queue"] == 0


def test_rate_limit_per_minute(tmp_path, rules):
    client = FakeClient()
    setup_worker(tmp_path, client, concurrency=3, rpm=2)
    run(rules, [make(f"K{i}", 2, 4, header=f"Сбой {i}") for i in range(4)])
    assert wait_until(lambda: client.calls == 2)
    time.sleep(0.3)
    assert client.calls == 2 and ai.status()["queue"] >= 2   # остальное ждёт следующей минуты


def test_enrich_never_waits_for_gigachat(tmp_path, rules):
    client = FakeClient(delay=2.0)
    setup_worker(tmp_path, client, concurrency=2)
    t0 = time.perf_counter()
    incs = run(rules, [make(f"K{i}", 2, 4, header=f"Сбой {i}") for i in range(10)])
    assert time.perf_counter() - t0 < 0.3
    assert all(i["ai"]["pending"] and i["ai"]["summary"] for i in incs.values())


def test_duplicates_confirmed_and_named_by_gigachat(tmp_path, rules):
    client = FakeClient()
    w = setup_worker(tmp_path, client)
    recs = [make(f"Z{i}", 2, 3, header="Ковёр не взлетает", submitDate=f"2026-11-10T10:{i * 10:02d}:00Z")
            for i in range(3)]
    run(rules, recs)
    assert wait_until(lambda: any("один и тот же сбой" in p for p in client.prompts) and not w.wanted
                      and not w.in_flight)
    d = run(rules, recs)["Z1"]["ai"]["duplicates"]
    assert d["name"] == "Ковёр не взлетает у всех" and d["source"] == "gigachat" and d["size"] == 3
    assert sum("один и тот же сбой" in p for p in client.prompts) == 1    # одна группа — один запрос


def test_risk_explained_only_for_top(tmp_path, rules, monkeypatch):
    monkeypatch.setattr(ai, "RISK_EXPLAIN_TOP", 1)
    client = FakeClient()
    w = setup_worker(tmp_path, client)
    hot = dict(impactId=4, urgencyId=4, serviceType="Клиентский", submitDate="2026-11-10T06:00:00Z",
               deadline="2026-11-10T11:00:00Z")
    run(rules, [make("H1", 2, 3, header="Первый", **hot), make("H2", 2, 3, header="Второй", impactId=3,
                                                              productName="Избушка", service="[Избушка] Печь", **{
        k: v for k, v in hot.items() if k != "impactId"})])
    assert wait_until(lambda: client.calls == 2 and not w.in_flight)
    with_risk = [p for p in client.prompts if "risk_comment" in p]
    assert len(with_risk) == 1 and "Первый" in with_risk[0]
    incs = run(rules, [make("H1", 2, 3, header="Первый", **hot)])
    assert incs["H1"]["ai"]["risk"]["comment"] == "Срок уже сорван, клиенты ждут."


def test_rules_mode_status():
    st = ai.status()
    assert st["provider"] == "rules" and st["state"] == "off"


def test_burst_confirmed_by_gigachat_absorbs_text_group(tmp_path, rules):
    from .test_ai_rules import mirror_records

    client = FakeClient()
    w = setup_worker(tmp_path, client)
    run(rules, mirror_records())
    assert wait_until(lambda: sum("один и тот же сбой" in p for p in client.prompts) == 2 and not w.wanted
                      and not w.in_flight)
    incs = run(rules, mirror_records())
    cards = {i: incs[i]["ai"]["duplicates"] for i in ("M0", "M1", "M2", "M3")}
    assert all(c and c["group_id"] == "burst-M0" and c["kind"] == "burst" for c in cards.values())
    assert cards["M3"]["size"] == 4 and cards["M3"]["name"] == "Ковёр не взлетает у всех"
    assert [g["id"] for g in ai.duplicate_groups()] == ["burst-M0"]


def test_rate_limit_429_short_backoff_not_offline(tmp_path, rules, monkeypatch):
    """Бесплатный GigaChat на второй одновременный запрос отвечает 429: короткая пауза, а не «нет связи» на минуту."""
    from backend.app.ai import llm
    from backend.app.ai.gigachat import GigaChatRateLimit

    monkeypatch.setattr(llm, "RATE_BACKOFF", 0.2)

    class Limited(FakeClient):
        hits = 0

        def chat(self, messages, **kw):
            if self.hits < 1:
                self.hits += 1
                raise GigaChatRateLimit("лимит запросов GigaChat (429)")
            return super().chat(messages, **kw)

    client = Limited()
    w = setup_worker(tmp_path, client)            # retry_seconds = 60: «нет связи» ждала бы минуту
    run(rules, [make("K", 0, 4)])
    assert wait_until(lambda: w.snapshot_status()["state"] == "limited", 1)
    assert "лимит" in ai.status()["label"]
    assert wait_until(lambda: w.snapshot_status()["done"] == 1, 2)   # повтор через 0,2 с, а не через минуту
    assert w.snapshot_status()["state"] == "ok"


def test_gigachat_offered_only_weighty_candidates(tmp_path, rules):
    client = FakeClient()
    w = setup_worker(tmp_path, client)
    recs = [make("C", 0, 2, COORD, service=SVC)]
    recs += [make(f"S{i}", 7, 2, "КЦ Ступа", service=SVC, completionDate="2026-11-10T11:00:00Z") for i in range(10)]
    recs += [make(f"L{i}", 7, 2, "КЦ Леший", service=SVC, completionDate="2026-11-10T11:00:00Z") for i in range(3)]
    recs += [make("V0", 7, 2, "КЦ Водяной", service=SVC, completionDate="2026-11-10T11:00:00Z")]
    incs = run(rules, recs)
    assert [c["team"] for c in incs["C"]["ai"]["route"]["candidates"]] == ["КЦ Ступа", "КЦ Леший", "КЦ Водяной"]
    assert wait_until(lambda: client.calls == 1 and not w.in_flight)
    prompt = client.prompts[0]
    assert "КЦ Ступа" in prompt and "КЦ Леший" in prompt and "КЦ Водяной" not in prompt   # 1 из 14 — не кандидат
