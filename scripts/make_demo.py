"""Генератор демо-файла «Сказочное королевство» в формате МТС (менторы разрешили свои данные).

    python scripts/make_demo.py                 # → data/demo/incidents.json, 5 000 инцидентов, даты от «сейчас»
    python scripts/make_demo.py --use           # и сразу сделать его рабочим data/incidents.json
    python scripts/make_demo.py --count 10000   # другой объём (для замеров)
    python scripts/make_demo.py --out путь.json

Смотреть с TIME_MODE=real (даты свежие). Детерминировано (seed): меняются только даты.

Что внутри (decisions.md, п. 2):
- история за 30 дней: решённые и закрытые — для подсказки «кому передать» и прогноза срока;
- ~10% активных (по сервису не чаще раза в 2 ч — без ложных дублей): средние и низкие (вне групп — для поиска и счётчика «ещё N вне групп»), высокие, исследование;
- герои демо с постоянными номерами (их трогает scripts/demo.py):
  INC000030000001 критический в работе (мастер)       INC000030000002 критический И на координаторах
  INC000030000003 высокий, срок горит — кандидат в критические (риск)
  INC000030000004 на координаторах → передать          INC000030000005 исследование → назначат на координаторов
  INC000030000006 средний → назначат на координаторов  INC000030000007 нарастающий приоритет (2 повышения за час)
  INC000030000011…16 — 6 дублей «Зеркальца» (одна поломка, разные обращения; флаги дублей не стоят — их находит ИИ)
  INC000030000021…23 — межпродуктовый сбой по общему коду; …24 — одновременное обращение с другим кодом
  INC000030000031 — повтор на координаторах; …32 — прошлое постоянное решение на том же узле
  INC000030000041…44 — ожидаем внешних, проверяем последствия, перекидывали между командами, задеты клиенты
  INC000030000051 — причина сбоя, …52 и …53 — два явных следствия
  ещё 5 инцидентов на координаторах с разным временем ожидания.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def say(text: str = "") -> str:
    """Текст для консоли: в Windows-консоли cp1251 нет «→» — печатаем «->», остальное непечатаемое заменяем «?»
    (раньше `demo.py list` падал с UnicodeEncodeError прямо на защите)."""
    enc = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    if enc != "utf8":
        text = text.replace("→", "->")
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    return text

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "demo" / "incidents.json"
COORD = "КЦ ИТ Координаторы Массовых Инцидентов"

STATES = {0: ("Назначен", "Assigned"), 2: ("Выполняется", "WorkInProgress"), 7: ("Завершен", "Resolved"),
          10: ("Закрыт", "Closed"), 20: ("Исследование ситуации", "Research")}
PRIO = {1: "Низкий", 2: "Средний", 3: "Высокий", 4: "Наивысший"}
URG = {1: "Низкая", 2: "Средняя", 3: "Высокая", 4: "Наивысшая"}
IMP = {1: "Низкое", 2: "Среднее", 3: "Высокое", 4: "Наивысшее"}
SLA_H = {4: 2, 3: 6, 2: 12, 1: 72}  # срок решения по приоритету, часов (как sla_hours в rules.yaml)
# Приоритет = влияние × срочность (в данных организаторов пара полностью определяет приоритет)
IMPACT_URGENCY = {4: (4, 4), 3: (3, 4), 2: (2, 3), 1: (1, 2)}

# продукт → (код, [(сервис, сценарий, команда, тип сервиса, [(заголовок, описание), ...])])
PRODUCTS = {
    "Ковёр-самолёт": ("BI_101", [
        ("[Ковёр-самолёт] Взлёт и посадка", "Полёт по расписанию", "КЦ Ступа", "Клиентский", [
            ("Ковёр не взлетает после обновления прошивки", "После ночного обновления ковры-самолёты не отрываются от земли, пассажиры ждут у терема."),
            ("Ковёр садится не на ту поляну", "Ковры приземляются в соседнем лесу вместо Тридевятого царства, жалобы пассажиров."),
        ]),
        ("[Ковёр-самолёт] Бронирование полётов", "Покупка билета", "КЦ Ковёрные мастера", "Клиентский", [
            ("Не проходит бронирование полёта", "При оплате полёта ковёр уходит в «обработку» и билет не выдаётся."),
        ]),
    ]),
    "Скатерть-самобранка": ("BI_202", [
        ("[Скатерть-самобранка] Меню", "Заказ блюд", "КЦ Скатерть L2", "Клиентский", [
            ("Скатерть накрывает только кашу", "Вместо выбранных блюд скатерть у всех клиентов выдаёт кашу из топора."),
            ("Пропали пироги из меню", "В меню скатерти исчез раздел «Пироги», клиенты не могут заказать."),
        ]),
        ("[Скатерть-самобранка] Доставка", "Доставка к столу", "КЦ Сапоги-скороходы Логистика", "Клиентский", [
            ("Скатерть не разворачивается у клиентов", "Скатерть приходит свёрнутой и не реагирует на «Развернись!»."),
        ]),
    ]),
    "Сапоги-скороходы": ("BI_303", [
        ("[Сапоги-скороходы] Маршруты", "Построение маршрута", "КЦ Сапоги-скороходы Логистика", "Клиентский", [
            ("Сапоги бегут не в ту сторону", "Навигация сапог ведёт за тридевять земель вместо соседней деревни."),
        ]),
        ("[Сапоги-скороходы] Скорость", "Семимильный шаг", "КЦ Скороходы Ходовая часть", "Технологический", [
            ("Сапоги шагают на одну милю вместо семи", "Деградация семимильного шага, курьеры опаздывают."),
        ]),
    ]),
    "Зеркальце": ("BI_404", [
        ("[Зеркальце] Сообщения", "Отправка сообщений", "КЦ Зеркальце Мессенджер", "Клиентский", [
            ("Зеркальце не доставляет сообщения", "Сообщения в Зеркальце висят с одной галочкой, получатели их не видят."),
        ]),
        ("[Зеркальце] Видеозвонки", "Звонок «покажи мне»", "КЦ Зеркальце Мессенджер", "Клиентский", [
            ("Зеркальце показывает не того абонента", "При видеозвонке «Свет мой, зеркальце» показывает чужое лицо."),
        ]),
        ("[Зеркальце] Вход", "Вход по волшебному слову", "КЦ Кощеева сеть", "Пользовательский", [
            ("Не принимается волшебное слово", "Пользователи не могут войти: Зеркальце отвечает «Слово неверное» на верное слово."),
        ]),
    ]),
    "Избушка": ("BI_505", [
        ("[Избушка] Хранилище", "Хранение сундуков", "КЦ Избушка на курьих ножках", "Технологический", [
            ("Избушка повернулась к лесу передом", "Хранилище недоступно: избушка стоит к клиентам задом и не открывает дверь."),
            ("Растут задержки сундуков", "Сундуки в облачной избушке открываются с задержкой до минуты."),
        ]),
        ("[Избушка] Виртуальные избушки", "Работа виртуальных избушек", "КЦ Избушка на курьих ножках", "Технологический", [
            ("Виртуальные избушки убежали в лес", "Часть виртуальных избушек кластера перестала отвечать — ушли на курьих ножках."),
        ]),
    ]),
    "Золотая рыбка": ("BI_606", [
        ("[Золотая рыбка] Исполнение желаний", "Оплата желания", "КЦ Золотая рыбка Платежи", "Клиентский", [
            ("Желания списываются дважды", "У части клиентов рыбка исполнила желание один раз, а списала два."),
            ("Рыбка отвечает «Чего тебе надобно» и молчит", "Платёж зависает на шаге подтверждения желания."),
        ]),
    ]),
    "Жар-птица ТВ": ("BI_707", [
        ("[Жар-птица ТВ] Видеоплатформа", "Просмотр сказок", "КЦ Жар-птица Медиа", "Клиентский", [
            ("Сказки не запускаются в Жар-птице", "У зрителей бесконечная буферизация, перо крутится и не грузит сказку."),
        ]),
    ]),
    "Кощеева сеть": ("BI_808", [
        ("[Кощеева сеть] Каталог пользователей", "Вход в рабочие системы", "КЦ Кощеева сеть", "Пользовательский", [
            ("Сотрудники не могут войти в терем", "Игла в яйце не отвечает: каталог пользователей отвечает с задержкой."),
        ]),
        ("[Кощеева сеть] Мосты", "Связь между царствами", "ОГЭ Тридевятое царство", "Технологический", [
            ("Калинов мост недоступен", "Пропала связь с Тридевятым царством, мониторинг фиксирует обрыв на мосту."),
        ]),
    ]),
}
# 6 обращений об одной поломке «Зеркальца»: разные слова, но общее ядро «не доставляются, висит одна галочка» —
# правила ИИ склеивают их по тексту и без GigaChat (≥50% общих основ слов, в пределах 2 ч)
MIRROR_DUPLICATES = [
    ("Зеркальце не доставляет сообщения", "Сообщения не доставляются, висит одна галочка, получатели их не видят."),
    ("Не уходят сообщения в Зеркальце", "Клиенты пишут: сообщения не доставляются уже полчаса, висит одна галочка."),
    ("Зеркальце: одна галочка у всех", "Массово сообщения не доставляются получателям, висит одна галочка."),
    ("Не приходят сообщения от родных", "Сообщения от родных не доставляются, у отправителя висит одна галочка."),
    ("Зависла отправка в Зеркальце", "Отправка зависает: сообщения не доставляются, висит одна галочка, потом ошибка."),
    ("Зеркальце молчит", "В чатах сообщения не доставляются, висит одна галочка; звонки при этом работают."),
]
BG_GAP_MIN = 130  # фоновые активные одного сервиса — не чаще раза в 2 ч (иначе ИИ честно видит дубли и всплески)
SOLUTIONS = ["Перезапущена волшебная палочка, работа восстановлена.", "Откат заклинания после обновления.",
             "Переключение на резервную ступу.", "Исправлена ошибка в путеводном клубочке.",
             "Добавлено волшебной силы, нагрузка нормализована.", "Избушку развернули к лесу задом, к клиентам передом."]
PEOPLE = [("Баба Яга", "babayaga"), ("Кощей Бессмертный", "koschei"), ("Иван-царевич", "ivan"),
          ("Василиса Премудрая", "vasilisa"), ("Елена Прекрасная", "elena"), ("Емеля", "emelya"),
          ("Конёк-Горбунок", "konek"), ("Змей Горыныч", "gorynych"), ("Илья Муромец", "muromets"),
          ("Добрыня Никитич", "dobrynya"), ("Алёша Попович", "alesha"), ("Царевна-лягушка", "frog"),
          ("Кот Баюн", "bayun"), ("Серый Волк", "wolf"), ("Марья-искусница", "marya"), ("Финист Ясный Сокол", "finist")]
INITIATORS = [("Мельник", "melnik"), ("Старик со старухой", "starik"), ("Колобок", "kolobok"), ("Леший", "leshiy"),
              ("Водяной", "vodyanoy"), ("Царь Горох", "goroh"), ("Красная Шапочка", "shapka"), ("Курочка Ряба", "ryaba")]
COORD_STAFF = [("Мудрый Ворон", "voron"), ("Сова Совушка", "sova")]
CHANNELS = ["Мониторинг", "Обращение клиента", "Звонок в контакт-центр", "Сорока на хвосте принесла"]


def fmt(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


class Gen:
    def __init__(self, now: datetime, seed: int = 7):
        self.now = now
        self.rnd = random.Random(seed)
        self.n = 30000100
        self.items: list[dict] = []
        self.services = [(p, i) for p, (_, svcs) in PRODUCTS.items() for i in range(len(svcs))]
        self.busy: dict[tuple[str, int], list[float]] = {}  # сервис → «сколько минут назад» созданы активные

    def add(self, product: str, svc_i: int, *, state: int, prio: int, ago_min: float, team: str | None = None,
            resolve_after_min: float | None = None, deadline_min: float | None = None, inc_id: str | None = None,
            master=False, double=False, assignee=None, header: str | None = None, descr: str | None = None,
            assigned_ago_min: float | None = None, work_log: list | None = None, no_deadline=False,
            stype: str | None = None) -> dict:
        code, services = PRODUCTS[product]
        service, scenario, own_team, own_stype, problems = services[svc_i]
        title, text = self.rnd.choice(problems)
        stype = stype or own_stype
        if inc_id is None:
            self.n += 1
            inc_id = f"INC0000{self.n}"
        submit = self.now - timedelta(minutes=ago_min)
        if no_deadline:
            deadline = None
        else:
            deadline = submit + timedelta(hours=SLA_H[prio]) if deadline_min is None else self.now + timedelta(minutes=deadline_min)
        completion = submit + timedelta(minutes=resolve_after_min) if resolve_after_min is not None else None
        if completion and completion > self.now - timedelta(minutes=5):
            completion = self.now - timedelta(minutes=self.rnd.uniform(5, 60))  # решение не может быть в будущем
        team = team or own_team
        person = assignee or (self.rnd.choice(COORD_STAFF) if team == COORD else self.rnd.choice(PEOPLE))
        init = self.rnd.choice(INITIATORS)
        imp, urg = IMPACT_URGENCY[prio]
        assigned = self.now - timedelta(minutes=assigned_ago_min) if assigned_ago_min is not None \
            else submit + timedelta(minutes=min(5.0, ago_min))
        done = state in (7, 10, 20)
        n = int(inc_id[-9:]) if inc_id[-9:].isdigit() else self.n
        rec = {
            "sysId": str(179400000000000000 + n),
            "id": inc_id,
            "state": STATES[state][0], "stateEnum": STATES[state][1], "stateId": state,
            "stateReason": "Постоянное решение" if done else "Нет",
            "stateReasonId": "430" if done else "1",
            "submitDate": fmt(submit), "deadline": fmt(deadline), "completionDate": fmt(completion),
            "lastAssignmentToTeamDateTime": fmt(assigned),
            "header": header or title, "description": descr or text,
            "shortDescription": (descr or text).split(",")[0].split(":")[0].rstrip(".") + ".",
            "assignedGroupName": team,
            "assignedEmployee": f"{person[0]} ({person[1]})" if person else None,
            "assignedEmployeeLogin": person[1] if person else None,
            "initiatorName": f"{init[0]} ({init[1]})", "initiatorLogin": init[1],
            "isEffect": False, "isReason": double, "isDouble": double, "isMaster": master,
            "incType": "МИ",
            "serviceTypeId": {"Клиентский": 10, "Пользовательский": 20, "Технологический": 30}[stype], "serviceType": stype,
            "priorityId": prio, "priority": PRIO[prio],
            "urgencyId": urg, "urgency": URG[urg], "impactId": imp, "impact": IMP[imp],
            "waitingExitDate": "0001-01-01T00:00:00", "isRootIncident": not double,
            "productName": product, "productId": str(174000000000000000 + int(code[3:])), "productMasterId": code,
            "productUrl": None, "service": service, "serviceId": str(174100000000000000 + int(code[3:]) * 10 + svc_i),
            "scenarioName": scenario, "isNeedUp": False if done else None,
            "eliminationType": None, "eliminationTypeId": None,
            "receivingChannel": self.rnd.choice(CHANNELS),
            # solution только у решённых (как просили менторы)
            "solution": self.rnd.choice(SOLUTIONS) if done else None,
        }
        if work_log:
            rec["workLog"] = work_log
        if state in (0, 2):
            self.busy.setdefault((product, svc_i), []).append(ago_min)
        self.items.append(rec)
        return rec

    def random_service(self) -> tuple[str, int]:
        return self.rnd.choice(self.services)

    def free_slots(self, n: int) -> list[tuple[float, str, int]]:
        """n моментов для фоновых активных: в одном сервисе не чаще BG_GAP_MIN и не ближе 2 ч к героям демо.
        → [(минут назад, продукт, сервис)], от свежих к старым."""
        slots: list[tuple[float, str, int]] = []
        k = 0
        while len(slots) < n:
            for si, (p, s) in enumerate(self.services):
                ago = 10 + si * 7 + k * BG_GAP_MIN + self.rnd.uniform(0, 5)
                if all(abs(ago - b) >= BG_GAP_MIN for b in self.busy.get((p, s), ())):
                    slots.append((ago, p, s))
            k += 1
        slots.sort()
        return slots[:n]


def _prio_log(now: datetime, steps: list[tuple[float, int, int]], author="Мудрый Ворон") -> list[dict]:
    """Записи журнала о прошлых повышениях приоритета (наш формат workLog: kind/before/after)."""
    out = []
    for ago, a, b in steps:
        out.append({"at": fmt(now - timedelta(minutes=ago)), "author": author, "action": "priority",
                    "text": f"приоритет: {PRIO[a]} → {PRIO[b]}", "kind": "priority", "before": PRIO[a], "after": PRIO[b],
                    "before_id": a, "after_id": b, "up": b > a})
    return out


def build(now: datetime, count: int = 5000, seed: int = 7) -> list[dict]:
    g = Gen(now, seed)
    r = g.rnd

    # ---------- 1) Герои демо (постоянные номера) ----------
    g.add("Ковёр-самолёт", 0, state=2, prio=4, ago_min=35, deadline_min=50, inc_id="INC000030000001", master=True,
          header="Ковёр не взлетает после обновления прошивки")                         # критический в работе
    g.add("Золотая рыбка", 0, state=0, prio=4, ago_min=12, deadline_min=95, inc_id="INC000030000002", team=COORD,
          assigned_ago_min=6, header="Желания списываются дважды")                       # критический И на координаторах
    g.add("Скатерть-самобранка", 0, state=2, prio=3, ago_min=320, deadline_min=40, inc_id="INC000030000003",
          header="Скатерть накрывает только кашу")                                       # высокий, срок горит → кандидат
    g.items[-1].update(impactId=4, impact=IMP[4])                                        # влияние наивысшее — риск выше
    g.add("Жар-птица ТВ", 0, state=0, prio=2, ago_min=25, inc_id="INC000030000004", team=COORD, assigned_ago_min=18)
    g.add("Кощеева сеть", 0, state=20, prio=3, ago_min=300, resolve_after_min=180, inc_id="INC000030000005", no_deadline=True)
    g.add("Сапоги-скороходы", 0, state=2, prio=2, ago_min=90, inc_id="INC000030000006")
    g.add("Избушка", 0, state=2, prio=3, ago_min=80, inc_id="INC000030000007", deadline_min=200,
          header="Избушка повернулась к лесу передом",
          work_log=_prio_log(now, [(70, 1, 2), (25, 2, 3)]))                             # нарастает: Низкий → Средний → Высокий
    # 6 дублей «Зеркальца»: одна поломка, разные обращения за последние 1,5 часа
    for k, (ago, (title, text)) in enumerate(zip((84, 61, 47, 33, 20, 9), MIRROR_DUPLICATES)):
        g.add("Зеркальце", 0, state=0 if k % 2 else 2, prio=3 if k == 0 else 2, ago_min=ago,
              inc_id=f"INC0000300000{11 + k}", header=title, descr=text)
    # Ещё на координаторах: разное время ожидания (порядок колонки — дольше ждёт выше)
    for svc, wait, prio, state in ((("Ковёр-самолёт", 1), 41, 2, 0), (("Избушка", 1), 27, 3, 2), (("Зеркальце", 2), 14, 2, 0),
                                   (("Скатерть-самобранка", 1), 8, 1, 0), (("Кощеева сеть", 1), 3, 3, 0)):
        g.add(svc[0], svc[1], state=state, prio=prio, ago_min=wait + r.uniform(5, 60), team=COORD, assigned_ago_min=wait)

    # Три симптома общего сбоя за 10 минут и одна посторонняя заявка в том же окне.
    g.add("Избушка", 1, state=2, prio=3, ago_min=13, deadline_min=42, inc_id="INC000030000021",
          header="Часть виртуальных избушек потеряла сеть",
          descr="Мониторинг зафиксировал обрыв соединения с несколькими экземплярами.").update(
              productMasterId="BI_901", receivingChannel=CHANNELS[0])
    g.add("Ковёр-самолёт", 1, state=0, prio=2, ago_min=8, inc_id="INC000030000022",
          header="Клиенту не выдали оплаченный билет",
          descr="После списания монет маршрут исчез из списка покупок.").update(
              productMasterId="BI_901", receivingChannel=CHANNELS[1])
    g.add("Скатерть-самобранка", 1, state=2, prio=2, ago_min=3, inc_id="INC000030000023",
          header="Курьеры не видят адреса доставки",
          descr="В диспетчерской ведомости пустая графа с адресом, заказы уже приготовлены.").update(
              productMasterId="BI_901", receivingChannel=CHANNELS[2])
    g.add("Сапоги-скороходы", 1, state=2, prio=2, ago_min=8, inc_id="INC000030000024",
          header="Сапоги замедлились после замены подошв",
          descr="Мастер заметил, что новая партия подошв скользит на мокрой брусчатке.").update(
              productMasterId="BI_902", receivingChannel=CHANNELS[3])

    bridge_scenario = "Обмен маршрутами через Калинов мост"
    g.add("Кощеева сеть", 1, state=0, prio=2, ago_min=85, team=COORD, assigned_ago_min=70,
          inc_id="INC000030000031", header="Повторный сбой общего узла · Калинов мост",
          descr="Маршруты через Калинов мост снова пропали из таблиц соседних узлов.").update(
              scenarioName=bridge_scenario)
    g.add("Кощеева сеть", 1, state=7, prio=2, ago_min=3 * 24 * 60, resolve_after_min=95,
          inc_id="INC000030000032", header="Обрыв обмена маршрутами на Калиновом мосту",
          descr="Соседние узлы перестали получать объявления маршрутов.").update(
              scenarioName=bridge_scenario, stateReason="Постоянное решение",
              solution="Заменили неисправный контроллер моста и восстановили обмен маршрутами.")

    g.add("Кощеева сеть", 0, state=2, prio=2, ago_min=70, team=COORD, assigned_ago_min=58, deadline_min=-8,
          inc_id="INC000030000041", header="Ждём внешних · поставщик не продлил ключи",
          descr="Поставщик подтверждает выпуск нового сертификата для каталога.").update(
              stateReason="Внешняя обработка")
    g.add("Избушка", 0, state=20, prio=3, ago_min=70, resolve_after_min=45, inc_id="INC000030000042",
          header="Проверить последствия · пропавшие квитанции",
          descr="Хранилище работает, но квитанции по части сундуков ещё не восстановлены.").update(isNeedUp=True)
    transfers = [(96, "КЦ Ступа", "КЦ Избушка на курьих ножках"),
                 (61, "КЦ Избушка на курьих ножках", "КЦ Кощеева сеть"),
                 (44, "КЦ Кощеева сеть", COORD)]
    transfer_log = [{"at": fmt(now - timedelta(minutes=ago)), "author": "Мудрый Ворон", "action": "assign",
                     "text": f"передан: {before} → {after}", "kind": "team", "before": before, "after": after}
                    for ago, before, after in transfers]
    g.add("Ковёр-самолёт", 0, state=0, prio=2, ago_min=105, team=COORD, assigned_ago_min=44,
          inc_id="INC000030000043", header="Три передачи · диспетчеры не видят экипаж",
          descr="В расписании рейс есть, но экипаж исчез из журнала диспетчерской.", work_log=transfer_log)
    g.add("Жар-птица ТВ", 0, state=2, prio=3, ago_min=21, stype="Клиентский", inc_id="INC000030000044",
          header="Задеты клиенты · пропали субтитры",
          descr="Во время показов у зрителей пропадают субтитры на нескольких каналах.")

    cause_id = "INC000030000051"
    g.add("Кощеева сеть", 1, state=2, prio=3, ago_min=38, inc_id=cause_id,
          header="Сбой общего сервера времени",
          descr="Мониторинг зафиксировал расхождение часов на общем сервере.",
          ).update(receivingChannel=CHANNELS[0])
    g.add("Сапоги-скороходы", 0, state=0, prio=2, ago_min=27, inc_id="INC000030000052",
          header="Навигация отвергает свежие карты",
          descr="Новые карты считаются просроченными из-за ошибочной метки времени.",
          ).update(causeIncidentId=cause_id, receivingChannel=CHANNELS[1])
    g.add("Золотая рыбка", 0, state=2, prio=2, ago_min=19, inc_id="INC000030000053",
          header="Подтверждения желаний теряют силу",
          descr="Проверка подписи отклоняет токены со временем выпуска в будущем.",
          ).update(causeIncidentId=cause_id, receivingChannel=CHANNELS[2])

    # ---------- 2) Фон: активные (~10%), по сервису не чаще раза в 2 ч ----------
    active = int(count * 0.10)
    research = max(10, count // 200)
    # высокие (в группе «Высокий приоритет») — самые свежие слоты; средние и низкие — вне групп
    # (поиск и «ещё N вне групп»); пара критических, чтобы колонка не была пустой без демо-шагов
    n_high = max(8, count // 120)
    slots = g.free_slots(active)
    young = slots[:n_high + 2]
    crit_at = {young[1], young[5 % len(young)]}
    for i, slot in enumerate(slots):
        ago, p, s = slot
        if slot in crit_at:
            prio = 4
        elif i < n_high + 2:
            prio = 3
        elif ago < 13 * 60:
            prio = r.choice([2, 2, 2, 1])
        else:
            prio = r.choice([1, 1, 2])
        rec = g.add(p, s, state=r.choice([0, 2, 2]), prio=prio, ago_min=ago, no_deadline=(r.random() < 0.02))
        if rec["stateId"] == 0 and r.random() < 0.3:  # назначен на команду, сотрудник ещё не выбран
            rec["assignedEmployee"] = rec["assignedEmployeeLogin"] = None
    for _ in range(research):
        p, s = g.random_service()
        prio = r.choice([2, 3, 3, 4])
        ago = r.uniform(120, 3 * 24 * 60)
        g.add(p, s, state=20, prio=prio, ago_min=ago, resolve_after_min=SLA_H[prio] * 60 * r.uniform(0.3, 0.9))

    # ---------- 3) Архив для маршрутизации и поиска: решённые/закрытые своей командой ----------
    while len(g.items) < count:
        p, s = g.random_service()
        prio = r.choice([1, 2, 2, 2, 3, 3, 4])
        # Фоновый архив старше свежих примеров. Иначе сотни случайных записей
        # одного узла попадают в окно повторов и затмевают сценарий «Калинов мост».
        ago = r.uniform(11 * 24 * 60, 30 * 24 * 60)
        team = None
        if r.random() < 0.12:  # иногда решала соседняя команда — подсказка «кому передать» не однозначна
            team = r.choice([sv[2] for _, (_, svcs) in PRODUCTS.items() for sv in svcs])
        g.add(p, s, state=r.choice([7, 10, 10, 10]), prio=prio, ago_min=ago, team=team,
              resolve_after_min=SLA_H[prio] * 60 * r.uniform(0.2, 1.15))
    return g.items[:count]


def main(argv: list[str] | None = None) -> Path:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=5000)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--use", action="store_true", help="сделать рабочим data/incidents.json")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    now = datetime.now(timezone.utc).replace(microsecond=0)
    items = build(now, args.count)
    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print(say(f"{out}: {len(items)} инцидентов, «сейчас» = {now.isoformat()} (смотреть с TIME_MODE=real)"))
    if args.use:
        (ROOT / "data" / "incidents.json").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
        print(say("→ data/incidents.json"))
    return out


if __name__ == "__main__":
    main()
