import json
from datetime import timedelta

import pytest

from backend.app import ai
from backend.app.adapter import adapt_all
from backend.app.ai.llm import LLMWorker
from backend.app.ai.outages import MAX_LLM_GROUPS, MAX_LLM_MEMBERS
from backend.app.groups import assign_groups

from .conftest import NOW, make
from .test_llm import wait_until


def record(inc_id, product, minute, code="BI_3244", state=0, service=None, **extra):
    return make(inc_id, state_id=state, productName=product, productMasterId=code, service=service or product,
                submitDate=(NOW + timedelta(minutes=minute)).isoformat(), **extra)


def run(rules, records, outages=None):
    incidents, _ = adapt_all(records, rules, "{id}")
    for inc_id, outage in (outages or {}).items():
        incidents[inc_id]["outage"] = outage
    assign_groups(incidents, rules, NOW)
    ai.enrich(incidents, NOW, rules)
    return incidents


def test_separate_cross_product_candidate_and_legacy_duplicates(rules):
    records = [record("C", "Вход", -1), record("B", "Сообщения", -9),
               record("X", "Доставка", -8, code="BI_0001"), record("A", "Хранилище", -14),
               record("D1", "Звонки", -8, code="BI_0002", header="Нет звука"),
               record("D2", "Звонки", -7, code="BI_0002", header="Нет звука")]
    incidents = run(rules, records)
    [candidate] = ai.outage_candidates()
    assert set(candidate) == {"master_id", "ids", "name", "reason", "source"}
    assert candidate["master_id"] == "A" and candidate["ids"] == ["A", "B", "C"]
    assert candidate["name"] == "Сбой · BI_3244"
    assert candidate["source"] == "rules"
    assert all(value in candidate["reason"] for value in ("BI_3244", "13 мин", "3 продуктах", "проверки"))
    assert all(not incidents[inc_id]["ai"].get("duplicates") for inc_id in candidate["ids"])
    assert [group["ids"] for group in ai.duplicate_groups()] == [["D1", "D2"]]

    run(rules, list(reversed(records)))
    assert ai.outage_candidates() == [candidate]
    run(rules, records[:2])
    assert ai.outage_candidates() == []


@pytest.mark.parametrize(("minute", "expected"), [(-360, ["A", "B", "C"]), (-360.001, None)])
def test_outage_window_includes_exactly_six_hours(rules, minute, expected):
    run(rules, [record("A", "Хранилище", minute), record("B", "Хранилище", -5), record("C", "Вход", 0)])
    assert [candidate["ids"] for candidate in ai.outage_candidates()] == ([expected] if expected else [])


def test_skips_non_active_missing_code_and_existing_outage(rules):
    records = [record("A", "Хранилище", -9), record("B", "Хранилище", -8),
               record("C", "Вход", -7), record("D", "Доставка", -6, state=7),
               record("E", "Почта", -5, code=None)]
    run(rules, records, outages={"C": {"id": "outage-1"}})
    assert not ai.outage_candidates()
    run(rules, records, outages={"C": {}})
    assert [candidate["ids"] for candidate in ai.outage_candidates()] == [["A", "B", "C"]]


def test_search_slides_past_one_product_window(rules):
    records = [record("Early", "Хранилище", -375), record("A", "Хранилище", -11),
               record("C", "Почта", -8), record("B", "Вход", -8)]
    run(rules, records)
    assert [candidate["ids"] for candidate in ai.outage_candidates()] == [["A", "B", "C"]]


class OutageClient:
    def __init__(self, same, exclude):
        self.prompts = []
        self.answer = {"same": same, "exclude": exclude, "name": "Общий отказ хранилища",
                       "reason": "Возможен отказ общего хранилища, несколько сервисов сообщают об ошибках записи."}

    def chat(self, messages):
        self.prompts.append(messages[-1]["content"])
        return json.dumps(self.answer, ensure_ascii=False)


@pytest.mark.parametrize(("same", "exclude", "expected"), [
    (True, ["X"], ["A", "B", "C"]),
    (False, [], None),
    (True, ["X", "B"], None),
    (True, ["C"], None),  # трое остаются, но они относятся к одному продукту
])
def test_outage_model_refines_candidate_without_updating_incidents(tmp_path, rules, same, exclude, expected):
    # X делит код с остальными, но описывает другой симптом.
    records = [record("X", "Хранилище", -14, service="SMS", header="Не приходит SMS", receivingChannel="Обращение"),
               record("A", "Хранилище", -13, service="Архив", header="Запись не проходит", receivingChannel="Мониторинг"),
               record("B", "Хранилище", -12, service="Выгрузка", header="Не пишутся сегменты"),
               record("C", "Почта", -11, header="Ошибка записи", initiatorName="Иван Петров",
                      shortDescription="Иван Петров сообщил об ошибке")]
    client = OutageClient(same, exclude)
    cache_file = tmp_path / "ai_cache.json"
    worker = LLMWorker(client, cache_file)
    ai.reset(worker=worker)
    run(rules, records)
    assert ai.outage_candidates()[0]["source"] == "rules"
    assert wait_until(lambda: worker.snapshot_status()["done"] == 1)
    incidents = run(rules, records)
    assert len(client.prompts) == 1
    assert all(value in client.prompts[0] for value in ("BI_3244", "Мониторинг", "Обращение", "Почта"))
    assert "Иван Петров" not in client.prompts[0]
    assert all(not inc.get("outage") and not inc["is_master"] and not inc["is_double"] and not inc["work_log"]
               for inc in incidents.values())
    if expected is None:
        assert ai.outage_candidates() == []
        return

    [candidate] = ai.outage_candidates()
    assert candidate == {"master_id": expected[0], "ids": expected, "name": client.answer["name"],
                         "reason": client.answer["reason"], "source": "gigachat"}
    assert wait_until(cache_file.exists)
    restarted_client = OutageClient(False, [])
    ai.reset(worker=LLMWorker(restarted_client, cache_file))
    run(rules, list(reversed(records)))
    assert ai.outage_candidates() == [{**candidate, "source": "cache"}]
    assert not restarted_client.prompts


def test_limits_background_work_without_losing_rule_candidates(rules):
    class Queue:
        def lookup(self, key):
            return None

        def known(self, key):
            return False

        def submit(self, tasks):
            self.tasks = tasks
            return {task.key for task in tasks}

    queue = Queue()
    ai.reset(worker=queue)
    records = [record(f"S{i}-{j}", f"Продукт {j}", -i, code=f"BI_{i}")
               for i in range(MAX_LLM_GROUPS + 1) for j in range(3)]
    records += [record(f"L{j}", f"Продукт {j}", 0, code="BI_LARGE") for j in range(MAX_LLM_MEMBERS + 1)]
    run(rules, records)
    assert len(ai.outage_candidates()) == MAX_LLM_GROUPS + 2
    outage_tasks = [task for task in queue.tasks if task.kind == "outage"]
    assert len(outage_tasks) == MAX_LLM_GROUPS
    assert all(len(task.meta["ids"]) == 3 for task in outage_tasks)
    assert any(task.kind == "dup" for task in queue.tasks)
    assert max(len(candidate["ids"]) for candidate in ai.outage_candidates()) == MAX_LLM_MEMBERS + 1
