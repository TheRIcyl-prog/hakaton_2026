"""Скорость на тысячах (decisions.md, п. 0): 5 000 инцидентов, правка в файле → готовый снимок ≤ 1 с.

Файл — демо-генератор «Сказочное королевство» (scripts/make_demo.py). Полный замер 5 000 / 10 000 —
scripts/bench.py.
"""
import gzip
import json
import sys
import time
from datetime import datetime, timezone

import pytest

from backend.app import ai
from backend.app.clock import Clock
from backend.app.config import ROOT, Settings
from backend.app.itsm import Itsm
from backend.app.store import Store

sys.path.insert(0, str(ROOT / "scripts"))
import make_demo  # noqa: E402

N = 5000
GOAL = 1.0


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("provider", ["off", "rules"])
def test_5000_edit_to_snapshot_under_1s(tmp_path, rules, monkeypatch, provider):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    ai.setup(cache_dir=tmp_path)
    data = make_demo.build(datetime.now(timezone.utc).replace(microsecond=0), N)
    f = tmp_path / "incidents.json"
    _write(f, data)
    s = Settings()
    s.incidents_file = f
    s.coalesce_seconds = 0
    store = Store(s, rules, Clock("real"))
    store.poll()
    assert len(store.incidents) == N

    # правка «как жюри»: средний активный инцидент → высокий
    idx = next(i for i, r in enumerate(data) if r["stateId"] in (0, 2) and r["priorityId"] == 2)
    inc_id = data[idx]["id"]
    worst = 0.0
    for prio, name in ((3, "Высокий"), (2, "Средний"), (3, "Высокий")):
        data[idx] = {**data[idx], "priorityId": prio, "priority": name}
        _write(f, data)
        t = time.perf_counter()
        assert store.poll()
        body, _ = store.snapshot_bytes(gzip_ok=True)
        worst = max(worst, time.perf_counter() - t)
    assert inc_id in json.loads(gzip.decompress(body))["groups"][2]["ids"]  # попал в «Высокий приоритет»
    assert worst <= GOAL, f"правка → снимок {worst:.2f} с на {N} инцидентов (цель ≤ {GOAL} с)"

    # своя запись из АРМ: «Взять в работу» → файл → применение в памяти
    itsm = Itsm(f, rules, store.clock.now, store=store, backup_dir=tmp_path / "b")
    t = time.perf_counter()
    itsm.take(inc_id, "Ольга")
    store.snapshot_bytes(gzip_ok=True)
    own = time.perf_counter() - t
    assert store.incidents[inc_id]["coordinator"]["name"] == "Ольга"
    assert own <= GOAL, f"«Взять в работу» {own:.2f} с на {N} инцидентов (цель ≤ {GOAL} с)"


def test_5000_snapshot_is_light(tmp_path, rules):
    data = make_demo.build(datetime.now(timezone.utc).replace(microsecond=0), N)
    f = tmp_path / "incidents.json"
    _write(f, data)
    s = Settings()
    s.incidents_file = f
    store = Store(s, rules, Clock("real"))
    store.poll()
    snap = store.snapshot()
    visible = set(snap["incidents"])
    in_groups = {i for g in snap["groups"] for i in g["ids"]}
    assert in_groups <= visible and len(visible) < N // 10
    assert snap["outside_active"] > 100 and snap["total_incidents"] == N
    gz, _ = store.snapshot_bytes(gzip_ok=True)
    assert len(gz) < 200_000  # было 22 МБ на 10 000
