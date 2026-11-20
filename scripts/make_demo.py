"""Генератор демо-файла «Сказочное королевство» в формате МТС (менторы разрешили свои данные).

    python scripts/make_demo.py                 # → data/demo/incidents.json, 5 000 инцидентов, даты от «сейчас»
    python scripts/make_demo.py --use           # и сразу сделать его рабочим data/incidents.json
    python scripts/make_demo.py --count 10000   # другой объём (для замеров)
    python scripts/make_demo.py --out путь.json

Смотреть с TIME_MODE=real (даты свежие). Детерминировано (seed): меняются только даты.

Что внутри (decisions.md, п. 2):
- история за 30 дней: решённые и закрытые — для подсказки «кому передать» и прогноза срока;
- ~15% активных: средние и низкие (вне групп — для поиска и счётчика «ещё N вне групп»), высокие, исследование;
- герои демо с постоянными номерами (их трогает scripts/demo.py):
  INC000030000001 критический в работе (мастер)       INC000030000002 критический И на координаторах
  INC000030000003 высокий, срок горит — кандидат в критические (риск)
  INC000030000004 на координаторах → передать          INC000030000005 исследование → назначат на координаторов
  INC000030000006 средний → назначат на координаторов  INC000030000007 нарастающий приоритет (2 повышения за час)
  INC000030000011…16 — 6 дублей «Зеркальца» (одна поломка, разные обращения; флаги дублей не стоят — их находит ИИ)
  ещё 5 инцидентов на координаторах с разным временем ожидания.
"""
from __future__ import annotations

import argparse
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
MIRROR_DUPLICATES = [  # 6 обращений об одной поломке «Зеркальца»
    ("Зеркальце не доставляет сообщения", "Сообщения висят с одной галочкой, получатели их не видят."),
    ("Не уходят сообщения в Зеркальце", "Клиенты пишут, что сообщения не отправляются уже полчаса."),
    ("Зеркальце: сообщения с одной галочкой", "Массово: сообщения не доходят до получателей, у всех одна галочка."),
    ("Не приходят сообщения от родных", "Абоненты жалуются, что не получают сообщения в Зеркальце."),
    ("Зависла отправка в Зеркальце", "Отправка сообщений зависает, через минуту ошибка «Не доставлено»."),
    ("Зеркальце молчит", "Сообщения в чатах Зеркальца не доставляются, звонки при этом работают."),
]
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
        self.items.append(rec)
        return rec

    def random_service(self) -> tuple[str, int]:
        return self.rnd.choice(self.services)


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

    # ---------- 2) Фон: активные (~15%) ----------
    active = int(count * 0.15)
    research = max(10, count // 200)
    # высокие (в группе «Высокий приоритет»), средние и низкие (вне групп — поиск и «ещё N вне групп»)
    n_high = max(8, count // 120)
    for i in range(active):
        p, s = g.random_service()
        if i < n_high:
            prio = 3
            ago = r.uniform(10, SLA_H[3] * 60 * 1.1)
        elif i < n_high + 2:
            prio = 4  # ещё пара критических, чтобы колонка не была пустой без демо-шагов
            ago = r.uniform(5, 80)
        else:
            prio = r.choice([2, 2, 2, 1, 1])
            ago = r.uniform(5, SLA_H[prio] * 60 * 1.05)
        rec = g.add(p, s, state=r.choice([0, 2, 2]), prio=prio, ago_min=ago, no_deadline=(r.random() < 0.02))
        if rec["stateId"] == 0 and r.random() < 0.3:  # назначен на команду, сотрудник ещё не выбран
            rec["assignedEmployee"] = rec["assignedEmployeeLogin"] = None
    for _ in range(research):
        p, s = g.random_service()
        prio = r.choice([2, 3, 3, 4])
        ago = r.uniform(120, 3 * 24 * 60)
        g.add(p, s, state=20, prio=prio, ago_min=ago, resolve_after_min=SLA_H[prio] * 60 * r.uniform(0.3, 0.9))

    # ---------- 3) История за 30 дней: решённые/закрытые своей командой ----------
    while len(g.items) < count:
        p, s = g.random_service()
        prio = r.choice([1, 2, 2, 2, 3, 3, 4])
        ago = r.uniform(3 * 60, 30 * 24 * 60)
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
    print(f"{out}: {len(items)} инцидентов, «сейчас» = {now.isoformat()} (смотреть с TIME_MODE=real)")
    if args.use:
        (ROOT / "data" / "incidents.json").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
        print("→ data/incidents.json")
    return out


if __name__ == "__main__":
    main()
