"""Синтетический поток на тысячи инцидентов для тестов ИИ (детерминированно)."""
import random
from datetime import timedelta

from .conftest import COORD, NOW, make

PRODUCTS = {
    "Ковёр-самолёт": ["Взлёт", "Посадка", "Навигация"],
    "Скатерть-самобранка": ["Меню", "Доставка"],
    "Сапоги-скороходы": ["Шаг", "Подошва"],
    "Зеркальце": ["Сообщения", "Звонки", "Вход"],
    "Избушка": ["Хранилище", "Виртуальные машины"],
}
TEAMS = ["КЦ Ступа", "КЦ Избушка на курьих ножках", "КЦ Кощеева игла", "КЦ Жар-птица", "КЦ Леший", "КЦ Водяной"]
WORDS = ["не работает", "медленно", "ошибка", "недоступен", "падает", "зависает", "не открывается", "сбой"]


def _h(s: str) -> int:
    return sum(map(ord, s))


def stream(n: int, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        product = rnd.choice(list(PRODUCTS))
        service = rnd.choice(PRODUCTS[product])
        r = rnd.random()
        state = 7 if r < 0.55 else 10 if r < 0.65 else 20 if r < 0.68 else rnd.choice([0, 2])
        prio = rnd.choices([1, 2, 3, 4], weights=[3, 5, 2, 0.3])[0]
        team = COORD if state in (0, 2, 20) and rnd.random() < 0.04 else TEAMS[_h(product + service) % 6 if rnd.random() < 0.7 else rnd.randrange(6)]
        created = NOW - timedelta(minutes=rnd.randrange(0, 60 * 48))
        sla = {4: 2, 3: 6, 2: 12, 1: 72}[prio]
        word = rnd.choice(WORDS)
        out.append(make(
            f"INC{i:06d}", state, prio, team,
            productName=product, productMasterId=f"BI_{_h(product) % 1000}",
            service=f"[{product}] {service}", scenarioName=service,
            header=f"{service}: {word}", shortDescription=f"У клиентов {product}: {service.lower()} {word}.",
            submitDate=created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            deadline=(created + timedelta(hours=sla)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            completionDate=(created + timedelta(hours=rnd.uniform(0.5, sla * 1.3))).strftime("%Y-%m-%dT%H:%M:%SZ")
            if state in (7, 10, 20) else None,
            impactId=rnd.randint(1, 4), urgencyId=rnd.randint(1, 4),
            serviceType=rnd.choice(["Клиентский", "Технологический"]),
            initiatorName="Иван Петров (ipetrov)", assignedEmployee="Баба Яга (byaga)",
        ))
    return out
