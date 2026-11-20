import json
from datetime import timezone

from backend.app.adapter import adapt_all, adapt_one, parse_dt
from backend.app.config import ROOT

from .conftest import COORD, make

TPL = "https://itsm/{id}?s={sysId}"


def test_parse_dt_formats():
    assert parse_dt("2026-11-08T12:02:29Z").tzinfo == timezone.utc
    assert parse_dt("2026-11-10T12:40:02.472263Z").microsecond == 472263
    assert parse_dt("2026-11-10T12:40:02.4722631Z").microsecond == 472263  # 7 знаков .NET
    assert parse_dt("2026-11-08 12:02:29").hour == 12
    assert parse_dt("0001-01-01T00:00:00") is None
    assert parse_dt(None) is None
    assert parse_dt("мусор") is None


def test_real_file_parses(rules):
    data = json.loads((ROOT / "data" / "original" / "character_problems.json").read_text(encoding="utf-8"))
    incs, warnings = adapt_all(data, rules, TPL)
    assert len(incs) == 44 and not warnings
    kinds = {i["state_kind"] for i in incs.values()}
    assert kinds == {"active", "resolved", "closed", "research"}


def test_state_7_is_resolved_10_is_closed(rules):
    assert adapt_one(make(state_id=7), rules, TPL)["state_kind"] == "resolved"
    assert adapt_one(make(state_id=10), rules, TPL)["state_kind"] == "closed"
    assert adapt_one(make(state_id=20), rules, TPL)["state_kind"] == "research"


def test_fallbacks_when_ids_missing(rules):
    raw = make(state_id=2, priority_id=3)
    raw["stateId"] = None
    raw["priorityId"] = "мусор"
    inc = adapt_one(raw, rules, TPL)
    assert inc["state_id"] == 2  # из stateEnum
    assert inc["priority_id"] == 3  # из текста


def test_string_ids_and_team_spacing(rules):
    inc = adapt_one(make(state_id="0", priority_id="4", team="  КЦ ИТ  Координаторы Массовых Инцидентов "), rules, TPL)
    assert inc["state_id"] == 0 and inc["priority_level"] == "critical" and inc["is_coordinators"]


def test_solution_hidden_for_active(rules):
    assert adapt_one(make(state_id=0, solution="уже решили?"), rules, TPL)["solution"] is None
    assert adapt_one(make(state_id=7, solution="перезагрузили"), rules, TPL)["solution"] == "перезагрузили"


def test_itsm_url(rules):
    assert adapt_one(make(id_="INC123"), rules, TPL)["itsm_url"] == "https://itsm/INC123?s=1123"


def test_bad_records_skipped_duplicates_last_wins(rules):
    data = [make("A", priority_id=1), "строка", {"нет": "id"}, make("A", priority_id=3)]
    incs, warnings = adapt_all(data, rules, TPL)
    assert list(incs) == ["A"] and incs["A"]["priority_id"] == 3
    assert len(warnings) == 3


def test_coord_constant_matches_rules(rules):
    assert rules.coordinators_team == COORD
