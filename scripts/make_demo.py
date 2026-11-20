"""Генератор своего демо-файла в формате МТС (менторы разрешили свои данные).

    python scripts/make_demo.py            # → data/demo/incidents.json (даты относительно «сейчас»)
    python scripts/make_demo.py --use      # и сразу сделать его рабочим data/incidents.json

Что внутри: история решённых инцидентов (для подсказки «куда передать» и прогноза срока),
активные инциденты всех групп, всплеск по одному сервису (массовость: мастер + дубли),
инцидент на исследовании у координаторов. Детерминировано (seed), меняются только даты.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "demo" / "incidents.json"
COORD = "КЦ ИТ Координаторы Массовых Инцидентов"

STATES = {0: ("Назначен", "Assigned"), 2: ("Выполняется", "WorkInProgress"), 7: ("Завершен", "Resolved"),
          10: ("Закрыт", "Closed"), 20: ("Исследование ситуации", "Research")}
PRIO = {1: "Низкий", 2: "Средний", 3: "Высокий", 4: "Наивысший"}
URG = {1: "Низкая", 2: "Средняя", 3: "Высокая", 4: "Наивысшая"}
IMP = {1: "Низкое", 2: "Среднее", 3: "Высокое", 4: "Наивысшее"}
SLA_H = {4: 2, 3: 6, 2: 12, 1: 72}  # срок решения по приоритету, часов

# продукт → (код, [(сервис, сценарий, команда, тип сервиса)])
PRODUCTS = {
    "Мобильный интернет": ("BI_101", [
        ("[Мобильный интернет] Пакетное ядро", "Передача данных 4G", "КЦ ТБ ДМиМС DC-NET Аварии", "Клиентский"),
        ("[Мобильный интернет] Радиосеть", "Доступ в сеть", "ОГЭ Приморье", "Клиентский"),
    ]),
    "Мой МТС": ("BI_202", [
        ("[Мой МТС] Авторизация", "Вход по SMS", "КЦ ИТ Мой МТС", "Клиентский"),
        ("[Мой МТС] Платежи", "Оплата связи", "КЦ ИТ ЦП Billing Platform", "Клиентский"),
    ]),
    "Биллинг": ("BI_303", [
        ("[Биллинг] Тарификация", "Списание абонплаты", "КЦ ИТ ЦРМП ОРМП Foris Аварии Тарификация", "Клиентский"),
        ("[Биллинг] Каталог продуктов", "Подключение услуг", "КЦ ИТ EP Product Catalog", "Пользовательский"),
    ]),
    "KION": ("BI_404", [
        ("[KION] Видеоплатформа", "Просмотр фильмов", "КЦ ИТ Media L2 KION", "Клиентский"),
    ]),
    "Корпоративная сеть": ("BI_505", [
        ("[Корпоративная сеть] VPN", "Удалённый доступ сотрудников", "КЦ ББ ДИБ УРМС VPN Support", "Пользовательский"),
        ("[Корпоративная сеть] Каталог пользователей", "Вход в рабочие системы", "КЦ ИТ Active Directory", "Пользовательский"),
    ]),
    "Инфраструктура ЦОД": ("BI_606", [
        ("[Инфраструктура ЦОД] Хранилище данных", "Доступ к СХД", "КЦ ДОИ Администрирование SAN", "Технологический"),
        ("[Инфраструктура ЦОД] Виртуализация", "Работа виртуальных машин", "КЦ ДОИ VMWare", "Технологический"),
    ]),
}

PROBLEMS = {
    "Пакетное ядро": ("Деградация передачи данных", "Абоненты жалуются на низкую скорость мобильного интернета, рост ошибок сессий на шлюзе."),
    "Радиосеть": ("Нет сети на базовых станциях", "Мониторинг фиксирует недоступность группы базовых станций, абоненты без связи."),
    "Авторизация": ("Не приходит SMS-код для входа", "Клиенты не могут войти в приложение Мой МТС: SMS с кодом не доставляется."),
    "Платежи": ("Ошибка оплаты в приложении", "При оплате связи картой в Мой МТС возвращается ошибка, платежи не проходят."),
    "Тарификация": ("Двойное списание абонплаты", "Часть абонентов получила повторное списание абонентской платы."),
    "Каталог продуктов": ("Не подключаются услуги", "Заявки на подключение опций зависают в статусе «в обработке»."),
    "Видеоплатформа": ("Не запускается видео в KION", "Пользователи видят ошибку воспроизведения, буферизация не завершается."),
    "VPN": ("Сотрудники не могут подключиться к VPN", "Удалённые сотрудники получают отказ при подключении к корпоративному VPN."),
    "Каталог пользователей": ("Ошибки входа в рабочие системы", "Сотрудники не могут авторизоваться, контроллер домена отвечает с задержкой."),
    "Хранилище данных": ("Деградация производительности СХД", "Рост задержек ввода-вывода на массиве, влияние на зависимые сервисы."),
    "Виртуализация": ("Недоступны виртуальные машины кластера", "Часть виртуальных машин в кластере перестала отвечать."),
}
SOLUTIONS = ["Перезапущен сервис, работа восстановлена.", "Откат конфигурации после обновления.",
             "Переключение на резервный узел.", "Исправлена ошибка маршрутизации.", "Увеличены ресурсы, нагрузка нормализована."]
PEOPLE = [("Иван Петров", "ipetrov"), ("Анна Смирнова", "asmirnova"), ("Олег Кузнецов", "okuznetsov"),
          ("Мария Соколова", "msokolova"), ("Дмитрий Орлов", "dorlov"), ("Елена Волкова", "evolkova")]
COORDS = [("Сергей Лебедев", "slebedev"), ("Ольга Новикова", "onovikova")]
CHANNELS = ["Мониторинг", "Обращение клиента", "Звонок в контакт-центр"]


def fmt(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


class Gen:
    def __init__(self, now: datetime, seed: int = 7):
        self.now = now
        self.rnd = random.Random(seed)
        self.n = 30000100
        self.items: list[dict] = []

    def add(self, product: str, svc_i: int, *, state: int, prio: int, ago_min: float, team: str | None = None,
            resolve_after_min: float | None = None, deadline_min: float | None = None, inc_id: str | None = None,
            urgency: int | None = None, impact: int | None = None, master=False, double=False, assignee=None,
            header: str | None = None) -> dict:
        code, services = PRODUCTS[product]
        service, scenario, own_team, stype = services[svc_i]
        key = service.split("] ")[1]
        title, descr = PROBLEMS[key]
        self.n += self.rnd.randint(3, 40)
        submit = self.now - timedelta(minutes=ago_min)
        deadline = submit + timedelta(hours=SLA_H[prio]) if deadline_min is None else self.now + timedelta(minutes=deadline_min)
        completion = submit + timedelta(minutes=resolve_after_min) if resolve_after_min is not None else None
        if completion and completion > self.now - timedelta(minutes=10):
            completion = self.now - timedelta(minutes=self.rnd.uniform(10, 60))  # решение не может быть в будущем
        team = team or own_team
        person = assignee or (self.rnd.choice(COORDS) if team == COORD else self.rnd.choice(PEOPLE))
        init = self.rnd.choice(PEOPLE)
        rec = {
            "sysId": str(179400000000000000 + self.n),
            "id": inc_id or f"INC0000{self.n}",
            "state": STATES[state][0], "stateEnum": STATES[state][1], "stateId": state,
            "stateReason": "Постоянное решение" if state in (7, 10, 20) else "Нет",
            "stateReasonId": "430" if state in (7, 10, 20) else "1",
            "submitDate": fmt(submit), "deadline": fmt(deadline), "completionDate": fmt(completion),
            "lastAssignmentToTeamDateTime": fmt(submit + timedelta(minutes=min(5, ago_min))),
            "header": header or title, "description": descr,
            "shortDescription": descr.split(",")[0].split(":")[0] + ".",
            "assignedGroupName": team,
            "assignedEmployee": f"{person[0]} ({person[1]})", "assignedEmployeeLogin": person[1],
            "initiatorName": f"{init[0]} ({init[1]})", "initiatorLogin": init[1],
            "isEffect": False, "isReason": double, "isDouble": double, "isMaster": master,
            "incType": "МИ",
            "serviceTypeId": {"Клиентский": 10, "Пользовательский": 20, "Технологический": 30}[stype], "serviceType": stype,
            "priorityId": prio, "priority": PRIO[prio],
            "urgencyId": urgency or min(4, prio + 1), "urgency": URG[urgency or min(4, prio + 1)],
            "impactId": impact or prio, "impact": IMP[impact or prio],
            "waitingExitDate": "0001-01-01T00:00:00", "isRootIncident": not double,
            "productName": product, "productId": str(174000000000000000 + int(code[3:])), "productMasterId": code,
            "productUrl": None, "service": service, "serviceId": str(174100000000000000 + int(code[3:]) * 10 + svc_i),
            "scenarioName": scenario, "isNeedUp": False if state in (7, 10, 20) else None,
            "eliminationType": None, "eliminationTypeId": None,
            "receivingChannel": self.rnd.choice(CHANNELS),
            # solution только у решённых (как просили менторы)
            "solution": self.rnd.choice(SOLUTIONS) if state in (7, 10, 20) else None,
        }
        self.items.append(rec)
        return rec


def build(now: datetime) -> list[dict]:
    g = Gen(now)
    r = g.rnd
    # 1) История за неделю: по 2–4 решённых на каждый сервис своей командой (для подсказок и прогноза)
    for product, (_, services) in PRODUCTS.items():
        for i in range(len(services)):
            for _ in range(r.randint(2, 4)):
                prio = r.choice([2, 2, 3, 3, 4, 1])
                ago = r.uniform(24 * 60, 7 * 24 * 60)
                g.add(product, i, state=r.choice([7, 10, 10]), prio=prio, ago_min=ago,
                      resolve_after_min=SLA_H[prio] * 60 * r.uniform(0.25, 1.1))

    # 2) Активные — герои демо (фиксированные номера, их трогает scripts/demo.py)
    g.add("Мобильный интернет", 0, state=2, prio=4, ago_min=35, deadline_min=50, inc_id="INC000030000001",
          urgency=4, impact=4, master=True)                                      # критический в работе
    g.add("Мой МТС", 0, state=0, prio=4, ago_min=12, deadline_min=95, inc_id="INC000030000002",
          team=COORD, urgency=4, impact=4)                                       # критический И на координаторах
    g.add("Биллинг", 0, state=2, prio=3, ago_min=200, deadline_min=40, inc_id="INC000030000003",
          urgency=4, impact=3)                                                   # высокий, срок горит → станет критическим
    g.add("KION", 0, state=0, prio=2, ago_min=25, inc_id="INC000030000004", team=COORD)  # на координаторах → передать
    g.add("Корпоративная сеть", 0, state=20, prio=3, ago_min=300, resolve_after_min=180,
          inc_id="INC000030000005", deadline_min=None)                           # исследование → назначат на координаторов
    g.add("Мой МТС", 1, state=2, prio=2, ago_min=90, inc_id="INC000030000006")  # средний → решат / переназначат

    # 3) Всплеск: мастер + дубли по пакетному ядру за последние 30 минут
    for ago in (28, 19, 11):
        g.add("Мобильный интернет", 0, state=0, prio=2, ago_min=ago, double=True)

    # 4) Фон: высокий, средние, низкие, исследование
    g.add("Инфраструктура ЦОД", 0, state=2, prio=3, ago_min=150, urgency=3, impact=3)
    g.add("Корпоративная сеть", 1, state=0, prio=3, ago_min=70)
    g.add("Биллинг", 1, state=2, prio=2, ago_min=400)
    g.add("Инфраструктура ЦОД", 1, state=0, prio=1, ago_min=600)
    g.add("KION", 0, state=20, prio=2, ago_min=900, resolve_after_min=240)
    g.add("Мобильный интернет", 1, state=20, prio=3, ago_min=1300, resolve_after_min=200, team=COORD)
    g.add("Мой МТС", 0, state=7, prio=3, ago_min=140, resolve_after_min=110)     # недавно решён (может вернуться)
    return g.items


def main() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    items = build(now)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)}: {len(items)} инцидентов, «сейчас» = {now.isoformat()}")
    if "--use" in sys.argv:
        (ROOT / "data" / "incidents.json").write_text(OUT.read_text(encoding="utf-8"), encoding="utf-8")
        print("→ data/incidents.json")


if __name__ == "__main__":
    main()
