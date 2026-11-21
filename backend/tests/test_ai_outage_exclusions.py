import pytest

from backend.app import ai

from .test_ai_outages import record, run


class CapturingQueue:
    def lookup(self, key):
        return None

    def known(self, key):
        return False

    def submit(self, tasks):
        self.tasks = tasks
        return {task.key for task in tasks}


def exclusions(*incident_ids):
    return [{"outage_id": "previous-outage", "incident_ids": list(incident_ids)}]


def test_outage_keeps_compatible_members_before_queueing(rules):
    queue = CapturingQueue()
    ai.reset(worker=queue)
    records = [record("A", "Продукт А", -4, outageExclusions=exclusions("B", "C")),
               record("B", "Продукт Б", -3), record("C", "Продукт В", -2), record("D", "Продукт Б", -1)]
    run(rules, list(reversed(records)))
    [candidate] = ai.outage_candidates()
    assert candidate["ids"] == ["B", "C", "D"] and candidate["master_id"] == "B"
    assert "3 активные заявки" in candidate["reason"] and "2 продуктах" in candidate["reason"]
    [task] = [task for task in queue.tasks if task.kind == "outage"]
    assert task.meta["ids"] == candidate["ids"]
    assert '"id": "A"' not in task.prompt


@pytest.mark.parametrize("last_product", [None, "Продукт Б"])
def test_outage_rechecks_minimum_size_and_product_count(rules, last_product):
    queue = CapturingQueue()
    ai.reset(worker=queue)
    records = [record("A", "Продукт А", -4, outageExclusions=exclusions("B")),
               record("B", "Продукт Б", -3), record("C", "Продукт Б", -2)]
    if last_product:
        records.append(record("D", last_product, -1))
    run(rules, records)
    assert ai.outage_candidates() == []
    assert not any(task.kind == "outage" for task in queue.tasks)


def test_outage_can_use_excluded_incident_with_other_members(rules):
    run(rules, [record("A", "Продукт А", -3, outageExclusions=exclusions("X")),
                record("B", "Продукт Б", -2), record("C", "Продукт Б", -1),
                record("X", "Продукт А", -365)])
    assert [candidate["ids"] for candidate in ai.outage_candidates()] == [["A", "B", "C"]]


def test_outage_slides_past_window_that_falls_below_threshold(rules):
    records = [record("A", "Продукт А", -365, outageExclusions=exclusions("B", "C")),
               record("B", "Продукт Б", -14), record("C", "Продукт В", -5), record("D", "Продукт Б", -2)]
    run(rules, records)
    assert [candidate["ids"] for candidate in ai.outage_candidates()] == [["B", "C", "D"]]


@pytest.mark.parametrize("owner", ["A", "C"])
def test_text_duplicates_block_either_direction_even_away_from_master(rules, owner):
    # B — мастер группы по времени. Запрещённая пара находится среди остальных участников.
    records = [record(inc_id, "Почта", minute, header="Письмо не отправляется",
                      outageExclusions=exclusions(other) if inc_id == owner else [])
               for inc_id, other, minute in [("B", None, -4), ("A", "C", -3), ("C", "A", -2), ("D", None, -1)]]
    incidents = run(rules, records)
    assert [group["ids"] for group in ai.duplicate_groups()] == [["B", "A", "D"]]
    assert not incidents["C"]["ai"].get("duplicates")


def test_burst_only_sends_compatible_members(rules):
    queue = CapturingQueue()
    ai.reset(worker=queue)
    titles = ["Отказ SMS", "Не открываются вложения", "Ошибка записи писем", "Очередь сообщений зависла"]
    records = [record(inc_id, "Почта", -4 + i, header=title, shortDescription="", description="",
                      outageExclusions=exclusions("B", "C") if inc_id == "A" else [])
               for i, (inc_id, title) in enumerate(zip("ABCD", titles))]
    records[-1]["priorityId"] = 3  # Всплеск уходит в очередь, если хотя бы один участник виден.
    run(rules, records)
    [task] = [task for task in queue.tasks if task.kind == "dup"]
    assert task.meta["ids"] == ["B", "C", "D"]
    assert "Отказ SMS" not in task.prompt
