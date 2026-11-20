"""Замер скорости на тысячах инцидентов (decisions.md, п. 0: правка → снимок ≤1 с на 5 000, ≤2 с на 10 000).

    python scripts/bench.py                 # 5 000 и 10 000
    python scripts/bench.py 3000 20000      # свои объёмы
    python scripts/bench.py --no-ai 10000   # без ИИ-правил (чтобы отделить время бэкенда от ai/)

Файл генерируется make_demo.py во временную папку, рабочий data/incidents.json не трогается.
Замеряется: первая загрузка; правка файла «как жюри» (повысить приоритет) → опрос → готовый снимок
(сериализация + gzip); «Взять в работу» из АРМ (чтение → запись файла → применение в памяти); поиск.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import make_demo  # noqa: E402

from backend.app import store as store_mod  # noqa: E402
from backend.app.clock import Clock  # noqa: E402
from backend.app.config import Settings, load_rules  # noqa: E402
from backend.app.itsm import Itsm  # noqa: E402
from backend.app.store import Store  # noqa: E402


def write(path: Path, data: list) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def edit_and_snapshot(store: Store, path: Path, data: list, idx: int, prio: int) -> float:
    data[idx] = {**data[idx], "priorityId": prio, "priority": make_demo.PRIO[prio]}
    write(path, data)
    t = time.perf_counter()
    assert store.poll(), "правка не замечена"
    store.snapshot_bytes(gzip_ok=True)
    return time.perf_counter() - t


def run(n: int, tmpdir: Path) -> dict:
    rules = load_rules(ROOT / "config" / "rules.yaml")
    path = tmpdir / f"incidents-{n}.json"
    data = make_demo.build(datetime.now(timezone.utc).replace(microsecond=0), n)
    write(path, data)
    s = Settings()
    s.incidents_file = path
    s.coalesce_seconds = 0
    store = Store(s, rules, Clock("real"))
    t = time.perf_counter()
    store.poll()
    load = time.perf_counter() - t

    # правка «как жюри»: средний активный → высокий, и обратно
    idx = next(i for i, r in enumerate(data) if r["stateId"] in (0, 2) and r["priorityId"] == 2)
    edits = [edit_and_snapshot(store, path, data, idx, p) for p in (3, 2, 3, 2)]

    itsm = Itsm(path, rules, store.clock.now, store=store, backup_dir=tmpdir / "backups")
    inc_id = data[idx]["id"]
    t = time.perf_counter()
    itsm.take(inc_id, "Замер")
    store.snapshot_bytes(gzip_ok=True)
    own = time.perf_counter() - t

    raw, _ = store.snapshot_bytes(False)
    gz, _ = store.snapshot_bytes(True)
    t = time.perf_counter()
    found = store.search("зеркальце сообщения", limit=50)
    search = time.perf_counter() - t
    return {"n": n, "load": load, "edit_max": max(edits), "edit_avg": sum(edits) / len(edits), "own": own,
            "snap_kb": len(raw) / 1024, "snap_gz_kb": len(gz) / 1024, "visible": len(json.loads(raw)["incidents"]),
            "search": search, "found": found["total"], "apply": store.last_apply_seconds}


def main(argv: list[str]) -> None:
    no_ai = "--no-ai" in argv
    sizes = [int(a) for a in argv if a.isdigit()] or [5000, 10000]
    if no_ai:
        store_mod.enrich = lambda *a, **k: None
        store_mod._ENRICH_TAKES_REFS = False
    with tempfile.TemporaryDirectory() as d:
        for n in sizes:
            r = run(n, Path(d))
            goal = 1.0 if n <= 5000 else 2.0
            ok = "OK" if r["edit_max"] <= goal and r["own"] <= goal else "МЕДЛЕННО"
            print(f"{r['n']:>6} инц.: загрузка {r['load']:.2f} с | правка файла → снимок {r['edit_avg']:.2f} с "
                  f"(макс {r['edit_max']:.2f}, цель ≤{goal:g}) | «взять в работу» {r['own']:.2f} с | "
                  f"снимок {r['snap_kb']:.0f} КБ, gzip {r['snap_gz_kb']:.0f} КБ, инцидентов в нём {r['visible']} | "
                  f"поиск {r['search'] * 1000:.0f} мс ({r['found']}) | {ok}{' | без ИИ' if no_ai else ''}")


if __name__ == "__main__":
    main(sys.argv[1:])
