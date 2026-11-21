"""Кандидаты на межпродуктовый сбой по общему коду и времени регистрации."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from math import ceil

from .index import Index
from .llm import LLMWorker, Task, build_outage_prompt, outage_key
from .text import clip, minutes_text, plural

WINDOW = timedelta(minutes=15)
MIN_INCIDENTS = 3
MIN_PRODUCTS = 2
MAX_LLM_GROUPS = 8
MAX_LLM_MEMBERS = 12
LLM_PRIORITY = 3.8


def find_candidates(incidents: dict[str, dict], index: Index) -> list[dict]:
    buckets: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for inc in incidents.values():
        code = inc.get("product_code")
        created = index.created[inc["id"]]
        if inc["state_kind"] == "active" and code and created is not None and not inc.get("outage"):
            buckets[code].append((created, inc["id"]))

    candidates = []
    for items in buckets.values():
        if len(items) < MIN_INCIDENTS:
            continue
        items.sort()
        products = [incidents[inc_id].get("product") for _, inc_id in items]
        counts: Counter[str] = Counter()
        left = right = 0
        # Первое подходящее окно каждого кода забирает своих участников. Если окно
        # не подходит, сдвигаем начало на одну заявку, сохраняя остальные в поиске.
        while left + MIN_INCIDENTS <= len(items):
            start = items[left][0]
            while right < len(items) and items[right][0] - start <= WINDOW:
                if products[right]:
                    counts[products[right]] += 1
                right += 1
            if right - left >= MIN_INCIDENTS and len(counts) >= MIN_PRODUCTS:
                ids = [inc_id for _, inc_id in items[left:right]]
                candidates.append(_candidate(ids, incidents, index, len(counts)))
                counts.clear()
                left = right
                continue
            product = products[left]
            if product:
                counts[product] -= 1
                if not counts[product]:
                    del counts[product]
            left += 1

    return sorted(candidates, key=lambda c: (index.created[c["ids"][-1]], c["master_id"]), reverse=True)


def _candidate(ids: list[str], incidents: dict[str, dict], index: Index, product_count: int) -> dict:
    master_id = ids[0]
    code = clip(incidents[master_id]["product_code"], 55)
    span = index.created[ids[-1]] - index.created[master_id]
    elapsed = minutes_text(ceil(span.total_seconds() / 60))
    incident_word = plural(len(ids), "активная заявка", "активные заявки", "активных заявок")
    product_word = plural(product_count, "продукте", "продуктах", "продуктах")
    return {
        "master_id": master_id,
        "ids": ids,
        "name": f"Сбой · {code}",
        "reason": (f"В пределах {elapsed} зарегистрировано {len(ids)} {incident_word} "
                   f"в {product_count} {product_word}. У заявок общий код {code}. Связь требует проверки."),
        "source": "rules",
    }


def refine(candidates: list[dict], incidents: dict[str, dict], index: Index,
           worker: LLMWorker) -> tuple[list[dict], list[Task]]:
    refined = []
    tasks = []
    review_count = 0
    for candidate in candidates:
        ids = candidate["ids"]
        # Группу целиком оставляем правилам, если модель увидела бы только её часть.
        if len(ids) > MAX_LLM_MEMBERS:
            refined.append(candidate)
            continue

        review_count += 1
        members = [incidents[inc_id] for inc_id in ids]
        code = members[0]["product_code"]
        key = outage_key(code, members)
        found = worker.lookup(key)
        if found is None:
            if review_count <= MAX_LLM_GROUPS and not worker.known(key):
                tasks.append(Task(key=f"outage:{key}", priority=LLM_PRIORITY,
                                  prompt=build_outage_prompt(code, members), parts={"outage": key},
                                  kind="outage", meta={"ids": ids}))
            refined.append(candidate)
            continue

        answer, source, _ = found
        if answer is None:
            refined.append(candidate)
            continue
        if not answer["same"]:
            continue
        excluded = set(answer["exclude"])
        kept = [inc_id for inc_id in ids if inc_id not in excluded]
        products = {incidents[inc_id].get("product") for inc_id in kept} - {None, ""}
        if len(kept) < MIN_INCIDENTS or len(products) < MIN_PRODUCTS:
            continue
        updated = _candidate(kept, incidents, index, len(products)) if excluded else dict(candidate)
        updated.update(source=source, name=answer["name"] or updated["name"], reason=answer["reason"] or updated["reason"])
        refined.append(updated)
    return refined, tasks
