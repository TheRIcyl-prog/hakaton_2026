"""Шаги демо на защите — запасной вариант, если ручная правка JSON сорвётся.

    python scripts/demo.py reset        # рабочий файл = оригинал
    python scripts/demo.py reset demo   # рабочий файл = свой демо-файл на 5 000 (make_demo.py), свежие даты, TIME_MODE=real
    python scripts/demo.py fresh        # оригинал со сдвигом всех дат так, что «выгрузка» = сейчас (для TIME_MODE=real)
    python scripts/demo.py list         # показать шаги
    python scripts/demo.py step 1       # применить шаг N к текущему рабочему файлу
    python scripts/demo.py all          # прогнать все шаги с паузой (по Enter)

Скрипт меняет только поля, которые есть в формате МТС, и пишет файл атомарно (через временный файл).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / os.getenv("INCIDENTS_FILE", "data/incidents.json")
ORIG = ROOT / "data" / "original" / "character_problems.json"

COORD = "КЦ ИТ Координаторы Массовых Инцидентов"
STATES = {0: ("Назначен", "Assigned"), 2: ("Выполняется", "WorkInProgress"), 7: ("Завершен", "Resolved"),
          10: ("Закрыт", "Closed"), 20: ("Исследование ситуации", "Research")}
PRIOS = {1: "Низкий", 2: "Средний", 3: "Высокий", 4: "Наивысший"}

# Герои демо: (исходная выгрузка, свой демо-файл из make_demo.py)
HIGH = ("INC000021938167", "INC000030000003")      # высокий в работе → станет критическим
MEDIUM = ("INC000021940779", "INC000030000006")    # средний → назначат на координаторов
RESEARCH = ("INC000021939297", "INC000030000005")  # исследование → назначат на координаторов
RISING = ("INC000030000007",)                      # только в демо-файле: уже дважды повышали приоритет


def load(path: Path = WORK) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def save(data: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(dir=WORK.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    for _ in range(20):  # на Windows файл может быть на миг занят читающим сервером
        try:
            os.replace(tmp, WORK)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, WORK)


def find(data: list[dict], ids: tuple[str, ...]) -> dict:
    for inc_id in ids:
        for r in data:
            if r.get("id") == inc_id:
                return r
    raise SystemExit(f"нет инцидентов {ids} — сначала reset")


def fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str | None) -> datetime | None:
    if not s or s.startswith("0001"):
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        frac = "".join(ch for ch in tail if ch.isdigit())[:6]
        s = f"{head}.{frac}+00:00"
    return datetime.fromisoformat(s)


def anchor(data: list[dict]) -> datetime:
    """«Сейчас» в логике дашборда (TIME_MODE=export): самая поздняя дата в файле."""
    best = None
    for r in data:
        for k in ("submitDate", "completionDate", "lastAssignmentToTeamDateTime"):
            dt = parse(r.get(k))
            if dt and (best is None or dt > best):
                best = dt
    return best or datetime.now(timezone.utc)


def set_state(r: dict, sid: int) -> None:
    r["stateId"], (r["state"], r["stateEnum"]) = sid, STATES[sid]


def set_prio(r: dict, pid: int) -> None:
    r["priorityId"], r["priority"] = pid, PRIOS[pid]


# ---------------- шаги ----------------
def step1(data):
    """Высокий → Наивысший (критический), срок через 45 мин."""
    r = find(data, HIGH)
    now = anchor(data)
    set_prio(r, 4)
    set_state(r, 2)
    r["deadline"] = fmt(now + timedelta(minutes=45))


def step2(data):
    """Средний инцидент назначен на координаторов."""
    r = find(data, MEDIUM)
    r["assignedGroupName"] = COORD
    r["assignedEmployee"], r["assignedEmployeeLogin"] = "Дядя Фёдор (unclefedor)", "unclefedor"
    set_state(r, 0)
    r["lastAssignmentToTeamDateTime"] = fmt(anchor(data))


def step3(data):
    """Инцидент на исследовании назначен на координаторов → он в двух группах."""
    r = find(data, RESEARCH)
    r["assignedGroupName"] = COORD
    r["lastAssignmentToTeamDateTime"] = fmt(anchor(data))


def step4(data):
    """Срок критического сдвинут на +2 часа."""
    r = find(data, HIGH)
    r["deadline"] = fmt(parse(r["deadline"]) + timedelta(hours=2))


def step5(data):
    """Критический решён (Завершен)."""
    r = find(data, HIGH)
    set_state(r, 7)
    r["completionDate"] = fmt(anchor(data))
    r["stateReason"] = "Постоянное решение"


def step6(data):
    """Критический переоткрыт (Завершен → Выполняется)."""
    r = find(data, HIGH)
    set_state(r, 2)
    r["completionDate"] = None


def step7(data):
    """Нарастающий приоритет (только демо-файл): Высокий → Наивысший — третье повышение за час."""
    r = find(data, RISING)
    log = r.get("workLog") if isinstance(r.get("workLog"), list) else []
    log.append({"at": fmt(anchor(data)), "author": "Жюри", "action": "priority",
                "text": f"приоритет: {r.get('priority')} → Наивысший", "kind": "priority",
                "before": r.get("priority"), "after": "Наивысший", "before_id": r.get("priorityId"), "after_id": 4, "up": True})
    r["workLog"] = log
    set_prio(r, 4)


STEPS = [step1, step2, step3, step4, step5, step6, step7]


def fresh() -> None:
    data = load(ORIG)
    shift = datetime.now(timezone.utc) - anchor(data)
    for r in data:
        for k in ("submitDate", "completionDate", "lastAssignmentToTeamDateTime", "deadline"):
            dt = parse(r.get(k))
            if dt:
                r[k] = fmt(dt + shift)
    save(data)
    print(f"даты сдвинуты на {shift}")


def main(argv: list[str]) -> None:
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "reset":
        if len(argv) > 2 and argv[2] == "demo":
            import make_demo  # свой демо-файл со свежими датами
            out = make_demo.main([])  # 5 000 инцидентов «Сказочного королевства»
            save(json.loads(out.read_text(encoding="utf-8")))
            print("рабочий файл = свой демо-файл")
        else:
            WORK.write_bytes(ORIG.read_bytes())
            print("рабочий файл = оригинал")
    elif cmd == "fresh":
        fresh()
    elif cmd == "list":
        for i, s in enumerate(STEPS, 1):
            print(f"{i}. {s.__doc__}")
    elif cmd == "step":
        n = int(argv[2])
        data = load()
        STEPS[n - 1](data)
        save(data)
        print(f"шаг {n}: {STEPS[n - 1].__doc__}")
    elif cmd == "all":
        for i, s in enumerate(STEPS, 1):
            input(f"Enter → шаг {i}: {s.__doc__}")
            data = load()
            s(data)
            save(data)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
