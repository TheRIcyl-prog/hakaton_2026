"""Изолированные сценарии показа по вкладкам АРМ и видеостене."""
from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from tempfile import mkdtemp
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from scripts import demo, make_demo

ROOT = Path(__file__).resolve().parents[1]
OLGA = "olga"
IVAN = "ivan"
OUTAGE_MEMBERS = (21, 22, 23)
Mutation = Callable[[list[dict]], None]


def incident_id(number: int) -> str:
    return f"INC000030000{number:03d}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def promote(rows: list[dict]) -> None:
    demo.step1(rows)
    demo.find(rows, demo.HIGH)["deadline"] = demo.fmt(now() + timedelta(minutes=45))


def assign(rows: list[dict], step: Mutation, incident: tuple[str, ...]) -> None:
    step(rows)
    demo.find(rows, incident)["lastAssignmentToTeamDateTime"] = demo.fmt(now())


def resolve(rows: list[dict]) -> None:
    demo.step5(rows)
    demo.find(rows, demo.HIGH)["completionDate"] = demo.fmt(now())


def wait_for(check, message: str, seconds: float = 10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.1)
    raise RuntimeError(message)


class Demo:
    def __init__(self, origin: str, path: Path, catalog: dict[int, dict]):
        self.origin = origin
        self.path = path
        self.catalog = catalog
        self.http = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def request(self, path: str, body: dict | None = None) -> dict:
        headers = {"Origin": self.origin}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = Request(self.origin + path, data=data, headers=headers)
        try:
            with self.http.open(request, timeout=2) as response:
                return json.load(response)
        except HTTPError as exc:
            with exc:
                details = json.load(exc).get("detail")
            raise RuntimeError(f"{path}: {details or exc.reason} (HTTP {exc.code})") from exc

    def edit(self, change: Mutation) -> None:
        last_change = self.request("/api/health")["file"]["last_change_at"]
        records = demo.load(self.path)
        before = copy.deepcopy(records)
        change(records)
        if records == before:
            return
        demo.save(records, self.path)
        wait_for(lambda: self.request("/api/health")["file"]["last_change_at"] != last_change,
                 "Сервер не подхватил изменение демо-файла.")

    def add_outage_members(self) -> None:
        def add(rows):
            ids = {row["id"] for row in rows}
            rows.extend(self.catalog[n] for n in OUTAGE_MEMBERS if incident_id(n) not in ids)

        self.edit(add)
        wait_for(lambda: self.match_outage("outage_candidates"),
                 "Предложение BI_901 не появилось.")

    def match_outage(self, bucket: str) -> dict | None:
        ids = {incident_id(n) for n in OUTAGE_MEMBERS}
        return next((item for item in self.request("/api/snapshot")[bucket] if set(item["incident_ids"]) == ids), None)

    def confirm_outage(self) -> None:
        if self.match_outage("outages"):
            return
        candidate = wait_for(lambda: self.match_outage("outage_candidates"),
                             "Предложение BI_901 не появилось.")
        self.request("/api/outages/confirm", {"candidate_id": candidate["id"]})

    def claim(self, number: int) -> None:
        path = "/api/incidents/" + incident_id(number)
        current = self.request(path)
        self.request(path + "/claim", {"expected_hash": current["hash"]})

    def send_handoff(self) -> None:
        self.request("/api/handoffs", {
            "recipient_id": IVAN,
            "note": "Проверить восстановление меню по №003 и взять в работу новое назначение №006.",
        })

    def change_state(self, number: int, state: int) -> None:
        inc_id = incident_id(number)
        current = self.request("/api/incidents/" + inc_id)
        self.request(f"/api/itsm/{inc_id}/state", {"expected_hash": current["hash"], "state_id": state})

    def finish_outage(self) -> None:
        for number in OUTAGE_MEMBERS:
            self.change_state(number, 7)


@dataclass(frozen=True)
class Step:
    prompt: str
    action: Callable[[Demo], None]
    result: str


@dataclass(frozen=True)
class Scenario:
    title: str
    view: str
    records: tuple[int, ...]
    intro: str
    steps: tuple[Step, ...] = ()
    seed: tuple[Mutation, ...] = ()
    prepare: Callable[[Demo], None] | None = None
    login: str = OLGA


PROMOTE = Step("Повысить приоритет №003 прямо в JSON", lambda run: run.edit(promote),
               "№003 перешёл из «Высокий приоритет» в «Критические». Проверьте карточку и тревогу.")
DEADLINE = Step("Сдвинуть срок №003 на два часа", lambda run: run.edit(demo.step4),
                "Таймер №003 обновился. В карточке и ленте указаны старый и новый сроки.")
RESOLVE = Step("Решить критический №003", lambda run: run.edit(resolve),
               "Появились статус «Решён» и уведомление. Тревога №003 снята.")
ASSIGN = Step("Назначить №006 на координаторов",
              lambda run: run.edit(lambda rows: assign(rows, demo.step2, demo.MEDIUM)),
              "№006 появился в «На координаторах» и в очереди на передачу.")

SCENARIOS = {
    "critical": Scenario(
        "Группы · критический, срок и решение", "board", (3, 4, 5, 6),
        "Найдите №003 в «Высокий приоритет». Откройте видеостену рядом, чтобы сравнить реакцию двух экранов.",
        (PROMOTE, DEADLINE, RESOLVE),
    ),
    "coordinators": Scenario(
        "Группы · новое назначение и пересечение", "board", (3, 5, 6),
        "В «На координаторах» пока пусто. №006 находится вне рабочих групп, №005 уже на исследовании.",
        (ASSIGN, Step("Назначить №005, сохранив статус исследования",
                      lambda run: run.edit(lambda rows: assign(rows, demo.step3, demo.RESEARCH)),
                      "№005 виден сразу в двух группах: «На координаторах» и «Исследование ситуации».")),
    ),
    "outages": Scenario(
        "Сбои · три продукта и подтверждение BI_901", "outages", (21, 24),
        "Предложений пока нет. №024 с другим кодом останется самостоятельной заявкой.",
        (Step("Подать две связанные заявки из выгрузки", Demo.add_outage_members,
              "Появилось предложение BI_901. Раскройте «Состав»: №021, №022 и №023 относятся к трём продуктам."),
         Step("Подтвердить BI_901 от Ольги или продолжить после подтверждения в браузере",
              Demo.confirm_outage, "Проверьте «Подтверждённые сбои» и «Сбои сейчас» на видеостене.")),
    ),
    "queue": Scenario(
        "Очередь · появляется задача на передачу", "queue", (6,),
        "У №006 пока обычный приоритет и профильная команда.",
        (ASSIGN,),
    ),
    "mine": Scenario(
        "Мои · заявка в работе у Ольги", "mine", (6,),
        "В «Мои» пусто. №006 ожидает обработки в группе «На координаторах».",
        (Step("Взять №006 в работу от Ольги", lambda run: run.claim(6),
              "В «Мои» появилась №006. В группе и во втором профиле виден её владелец."),),
        seed=(demo.step2,),
    ),
    "handoff": Scenario(
        "Передача смены · входящая смена у Ивана", "handoff", (3, 6),
        "Войдите как ivan. После отправки откройте входящую передачу, прочитайте записку и нажмите «Принять смену».",
        (Step("Отправить Ивану смену от Ольги", Demo.send_handoff,
              "Иван получил входящую передачу с активными задачами и следующим шагом."),),
        seed=(promote, demo.step2), prepare=lambda run: run.claim(3), login=IVAN,
    ),
    "feed": Scenario(
        "Лента · приоритет, назначение и решение", "feed", (3, 6),
        "Оставьте ленту открытой. После каждого шага сравните событие и карточку по номеру.",
        (PROMOTE, ASSIGN, RESOLVE),
    ),
    "recent": Scenario(
        "Недавно закрытые · инцидент и сбой", "recent", (3, *OUTAGE_MEMBERS),
        "№003 уже решён, BI_901 подтверждён и пока активен. Сначала откройте «Инциденты».",
        (Step("Закрыть №003", lambda run: run.change_state(3, 10),
              "№003 появился в «Инцидентах». Откройте карточку и историю изменений."),
         Step("Завершить заявки BI_901", Demo.finish_outage,
              "Во вложенной вкладке «Сбои» появится BI_901. Раскройте «Состав».")),
        seed=(promote, resolve), prepare=Demo.confirm_outage,
    ),
    "settings": Scenario(
        "Кастомизация · поля карточек и колонки АРМ", "board", (1, 3, 4, 5, 6, 31, 32),
        "Через «Ещё» откройте «Поля карточек». Включите краткую суть и сравните исходный и личный наборы. "
        "Сохраните изменения. В «Колонки АРМ» поднимите «На координаторах» и покажите флажок «Сворачивать пустые».",
    ),
    "wall": Scenario(
        "Видеостена · четыре группы и реакция на JSON", "wall", (1, 3, 4, 5, 6),
        "На стене видны все четыре группы. Найдите №003 по названию «Скатерть накрывает только кашу».",
        (PROMOTE, RESOLVE),
    ),
}


def start_demo(key: str, scenario: Scenario, open_browser: bool) -> None:
    from backend.app.auth import hash_password

    cache = ROOT / "cache"
    cache.mkdir(exist_ok=True)
    directory = Path(mkdtemp(prefix=f"defense-{key}-", dir=cache))
    path = directory / "incidents.json"
    sample = {row["id"]: row for row in make_demo.build(now(), count=64)}
    catalog = {number: sample[incident_id(number)] for number in {*scenario.records, *OUTAGE_MEMBERS}}
    records = [copy.deepcopy(catalog[number]) for number in scenario.records]
    for edit in scenario.seed:
        edit(records)
    demo.save(records, path)

    with socket.socket() as free_port:
        free_port.bind(("127.0.0.1", 0))
        port = free_port.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    password = secrets.token_hex(4)
    accounts = [
        {"id": login, "login": login, "display_name": name, "password_hash": hash_password(password)}
        for login, name in ((OLGA, "Ольга"), (IVAN, "Иван"))
    ]
    env = dict(os.environ, PYTHON_DOTENV_DISABLED="1", INCIDENTS_FILE=str(path),
               RULES_FILE=str(ROOT / "config" / "rules.yaml"), TIME_MODE="real",
               POLL_INTERVAL="0.25", COALESCE_SECONDS="0.5",
               CACHE_DIR=str(directory / "state"), BACKUPS_DIR=str(directory / "backups"),
               LLM_PROVIDER="rules", ITSM_URL_TEMPLATE="/itsm/{id}",
               DEMO_ORIGINS=origin, DEMO_ACCOUNTS=json.dumps(accounts),
               DEMO_SESSION_SECONDS="7200", PYTHONIOENCODING="utf-8")
    backend_log = directory / "server.log"
    run = Demo(origin, path, catalog)
    with backend_log.open("wb") as log, subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--no-access-log"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    ) as server:
        try:
            def ready():
                if server.poll() is not None:
                    raise RuntimeError(f"Сервер завершился при запуске. Журнал: {backend_log}")
                try:
                    file = run.request("/api/health")["file"]
                except URLError:
                    return False
                return Path(file["path"]) == path and file["status"] == "ok"

            wait_for(ready, f"Демо-сервер не готов. Журнал: {backend_log}", seconds=15)
            run.request("/api/auth/login", {"login": OLGA, "password": password})
            if scenario.prepare:
                scenario.prepare(run)
            arm = f"{origin}/?view={scenario.view if scenario.view != 'wall' else 'board'}"
            wall = f"{origin}/wall"
            print(f"\n{scenario.title}\nАРМ: {arm}\nВидеостена: {wall}\n"
                  f"Вход: {scenario.login} / {password} (второй профиль: "
                  f"{IVAN if scenario.login == OLGA else OLGA}, тот же пароль)\n"
                  f"Демо-файл: {path}\n\n{scenario.intro}\n", flush=True)
            if open_browser:
                webbrowser.open(wall if scenario.view == "wall" else arm)
            for index, step in enumerate(scenario.steps, 1):
                if not proceed(f"{index}. {step.prompt}"):
                    return
                step.action(run)
                print(step.result, flush=True)
            input("\nEnter — завершить показ и остановить этот демо-сервер. ")
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def proceed(prompt: str) -> bool:
    while True:
        answer = input(f"\n{prompt}. Enter — выполнить, 0 — закончить: ").strip()
        if answer in ("", "0"):
            return answer == ""


def choose() -> str | None:
    items = list(SCENARIOS)
    print("\nСценарии защиты")
    for index, key in enumerate(items, 1):
        print(f"{index:2}. {SCENARIOS[key].title}")
    print(" 0. Выход")
    while True:
        choice = input("Номер сценария: ").strip()
        if choice == "0":
            return None
        if choice.isascii() and choice.isdigit() and len(choice) <= 2 and 1 <= int(choice) <= len(items):
            return items[int(choice) - 1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", choices=SCENARIOS, help="без аргументов — меню")
    parser.add_argument("--no-browser", action="store_true", help="показать ссылки в терминале")
    args = parser.parse_args()
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    try:
        if args.scenario:
            start_demo(args.scenario, SCENARIOS[args.scenario], not args.no_browser)
        else:
            while key := choose():
                start_demo(key, SCENARIOS[key], not args.no_browser)
    except (KeyboardInterrupt, EOFError):
        return 0
    except (OSError, RuntimeError, URLError) as exc:
        print(f"Демо остановлено. {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
