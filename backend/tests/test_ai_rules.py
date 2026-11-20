"""ИИ на правилах: выжимка, маршрут по истории, риск, тревожная динамика, дубли, скорость."""
import time
from datetime import timedelta

from backend.app import ai
from backend.app.adapter import adapt_all, iso
from backend.app.ai import dynamics
from backend.app.groups import assign_groups

from .ai_data import stream
from .conftest import COORD, NOW, make

SVC = "[Летающий ковер] Ворс"


def run(rules, records, now=NOW):
    incs, _ = adapt_all(records, rules, "https://itsm/{id}")
    assign_groups(incs, rules, now)
    ai.enrich(incs, now, rules)
    return incs


def test_summary_is_short():
    inc = {"short_description": "Абоненты Сказочного королевства не могут оплатить связь картой в приложении "
                                "Зеркальце уже третий час подряд, ошибка 500", "title": "x"}
    s = ai.rule_ai.summary(inc)
    assert len(s) <= 90 and s.endswith("…")


def test_route_candidates_from_history(rules):
    recs = [make("C", 0, 2, COORD, service=SVC)]
    recs += [make(f"S{i}", 7, 2, "КЦ Ступа", service=SVC, completionDate="2026-11-10T11:00:00Z") for i in range(3)]
    recs += [make("L1", 2, 2, "КЦ Леший", service=SVC)]
    recs += [make(f"O{i}", 7, 2, "КЦ Водяной", service="[Другое] Сервис", scenarioName="Другой",
                  productName="Другое", productMasterId="BI_9") for i in range(5)]
    incs = run(rules, recs)
    r = incs["C"]["ai"]["route"]
    assert r["team"] == "КЦ Ступа"
    assert r["confidence_text"] == "3 из 4 похожих" and r["confidence"] == 0.75
    assert "по сервису" in r["basis"] and "решила 3" in r["reason"]
    assert [a["team"] for a in r["alternatives"]] == ["КЦ Леший"]
    assert {s["id"] for s in r["similar"]} == {"S0", "S1", "S2"} and r["similar"][0]["url"] == "https://itsm/S0"
    assert r["source"] == "rules" and r["at"]
    assert "_cands" not in r


def test_route_falls_back_to_coarser_level(rules):
    recs = [make("C", 0, 2, COORD, service="[Летающий ковер] Новый сервис")]
    recs += [make(f"S{i}", 7, 2, "КЦ Ступа", service=f"[Летающий ковер] Старый {i}") for i in range(3)]
    r = run(rules, recs)["C"]["ai"]["route"]
    assert r["team"] == "КЦ Ступа" and "сценарию" in r["basis"]


def test_risk_only_for_high_and_explained(rules):
    hot = make("H", 2, 3, "КЦ Ступа", impactId=4, urgencyId=4, serviceType="Клиентский",
               submitDate="2026-11-10T06:00:00Z", deadline="2026-11-10T11:00:00Z")
    calm = make("M", 2, 2, "КЦ Ступа")
    incs = run(rules, [hot, calm])
    r = incs["H"]["ai"]["risk"]
    assert r["level"] == "high" and r["score"] >= 70
    assert "срок нарушен" in r["factors"] and sum(b["points"] for b in r["breakdown"]) == r["score"]
    assert incs["M"]["ai"].get("risk") is None   # средний без динамики — риск не считаем


def _history_run(rules, steps, start=NOW):
    """steps: список (минуты от начала, записи). Возвращает инциденты после последнего шага."""
    incs = None
    for minutes, recs in steps:
        incs = run(rules, recs, start + timedelta(minutes=minutes))
    return incs


def test_dynamics_priority_raised_twice(rules):
    inc = lambda p: make("D", 2, p, "КЦ Ступа")  # noqa: E731
    incs = _history_run(rules, [(0, [inc(1)]), (10, [inc(2)]), (40, [inc(3)])])
    d = incs["D"]["ai"]["dynamics"]
    assert d["flag"] and d["label"] == "нарастает"
    assert d["patterns"][0]["kind"] == "priority_up"
    assert "Низкий → Средний → Высокий за 30 мин" in d["text"]
    assert any(f.startswith("нарастает") for f in incs["D"]["ai"]["risk"]["factors"])


def test_dynamics_medium_gets_risk_when_flagged(rules):
    teams = ["КЦ Ступа", "КЦ Леший", "КЦ Водяной", "КЦ Жар-птица"]
    incs = _history_run(rules, [(i * 20, [make("D", 2, 2, t)]) for i, t in enumerate(teams)])
    d = incs["D"]["ai"]["dynamics"]
    assert d["patterns"][0]["kind"] == "reassign" and "3 раза" in d["text"]
    assert incs["D"]["ai"]["risk"] is not None   # средний, но отмечен динамикой → риск считается


def test_dynamics_reopened_and_deadline(rules):
    r1 = make("R", 7, 2, "КЦ Ступа", completionDate="2026-11-10T11:00:00Z")
    r2 = make("R", 2, 2, "КЦ Ступа")
    incs = _history_run(rules, [(0, [r1]), (5, [r2])])
    assert incs["R"]["ai"]["dynamics"]["patterns"][0]["kind"] == "reopened"

    dl = lambda h: make("T", 2, 2, "КЦ Ступа", deadline=f"2026-11-10T{h}:00:00Z")  # noqa: E731
    ai.reset()
    incs = _history_run(rules, [(0, [dl(14)]), (5, [dl(16)]), (10, [dl(18)])])
    d = incs["T"]["ai"]["dynamics"]
    assert d["patterns"][0]["kind"] == "deadline_moved" and "+2 ч" in d["text"]


def test_history_persists_and_resets_on_replaced_file(tmp_path, rules):
    path = tmp_path / "hist.json"
    ai.reset(history=dynamics.History(path))
    run(rules, [make("P", 2, 1)])
    run(rules, [make("P", 2, 2)], NOW + timedelta(minutes=5))
    ai._history.flush(force=True)
    ai.reset(history=dynamics.History(path))              # «перезапуск»
    incs = run(rules, [make("P", 2, 3)], NOW + timedelta(minutes=10))
    assert incs["P"]["ai"]["dynamics"]["patterns"][0]["kind"] == "priority_up"
    # файл подменили целиком — история с чистого листа, без лавины изменений
    run(rules, [make(f"N{i}", 2, 2) for i in range(5)], NOW + timedelta(minutes=11))
    assert ai.history("P") == []


def test_duplicates_grouped_in_window(rules):
    base = dict(productName="Зеркальце", service="[Зеркальце] Сообщения", scenarioName="Сообщения")
    recs = [make(f"Z{i}", 2, 3, "КЦ Ступа", header="Не доставляются сообщения в Зеркальце",
                 shortDescription="Сообщения не доставляются получателям.",
                 submitDate=f"2026-11-10T10:{i * 10:02d}:00Z", **base) for i in range(4)]
    recs.append(make("ZLATE", 2, 3, "КЦ Ступа", header="Не доставляются сообщения в Зеркальце",
                     submitDate="2026-11-10T13:30:00Z", **base))
    recs.append(make("OTHER", 2, 3, "КЦ Ступа", header="Не доставляются сообщения в Зеркальце",
                     submitDate="2026-11-10T10:05:00Z", productName="Избушка", service="[Избушка] Хранилище"))
    incs = run(rules, recs)
    d = incs["Z2"]["ai"]["duplicates"]
    assert d["master_id"] == "Z0" and d["size"] == 4 and d["ids"] == ["Z0", "Z1", "Z2", "Z3"]
    assert not d["confirmed"] and d["source"] == "rules" and "4 похожих инцидента" in d["reason"]
    assert incs["Z0"]["ai"]["duplicates"]["is_master"]
    assert incs["ZLATE"]["ai"]["duplicates"] is None and incs["OTHER"]["ai"]["duplicates"] is None
    assert [g["id"] for g in ai.duplicate_groups()] == ["dup-Z0"]


def test_duplicates_confirmed_from_file(rules):
    recs = [make("A", 2, 3, isMaster=True, submitDate="2026-11-10T10:30:00Z"),
            make("B", 2, 3, isDouble=True, submitDate="2026-11-10T10:00:00Z")]
    d = run(rules, recs)["B"]["ai"]["duplicates"]
    assert d["confirmed"] and d["master_id"] == "A"


def test_enrich_5000_is_fast(rules):
    incs, _ = adapt_all(stream(5000), rules, "{id}")
    assign_groups(incs, rules, NOW)
    ai.enrich(incs, NOW, rules)            # первый проход — заполняет историю
    t0 = time.perf_counter()
    ai.enrich(incs, NOW, rules)
    took = time.perf_counter() - t0
    assert took < 0.5, f"ИИ на 5 000 инцидентов занял {took:.2f} с"
    assert sum(1 for i in incs.values() if i["ai"].get("route")) > 10


def test_dynamics_detect_ignores_closed():
    evs = [{"at": iso(NOW), "kind": "reopen", "before": "Завершен", "after": "Выполняется"}]
    assert dynamics.detect({"state_kind": "closed"}, evs, NOW) is None


def test_dynamics_from_backend_history_seen_on_first_load(rules):
    """История бэкенда (refs.history, засеяна из workLog) — нарастание видно сразу, без двух чтений файла."""
    class Hist:
        def get(self, inc_id):
            if inc_id != "W":
                return []
            return [  # формат backend/app/history.py: без "up", с before_id/after_id, другие поля игнорируем
                {"at": "2026-11-10T11:50:00.500000Z", "kind": "priority", "before": "Средний", "after": "Высокий",
                 "before_id": 2, "after_id": 3, "by": "Ольга"},
                {"at": "2026-11-10T11:20:00Z", "kind": "priority", "before": "Низкий", "after": "Средний",
                 "before_id": 1, "after_id": 2, "by": "itsm"},
                {"at": "2026-11-10T11:30:00Z", "kind": "assignee", "before": None, "after": "x"},
            ]

    class Refs:
        history = Hist()
        changed = {"W"}

    incs, _ = adapt_all([make("W", 2, 3), make("Q", 2, 3)], rules, "{id}")
    assign_groups(incs, rules, NOW)
    ai.enrich(incs, NOW, rules, refs=Refs())
    d = incs["W"]["ai"]["dynamics"]
    assert d and "Низкий → Средний → Высокий за 30 мин" in d["text"]
    assert incs["Q"]["ai"]["dynamics"] is None
    assert ai.history("W") == []          # своя история не копится, когда есть бэкенда
