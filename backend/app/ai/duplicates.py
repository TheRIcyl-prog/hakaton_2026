"""Кандидаты в дубли (п. 8.4) — дёшево, без LLM.

Тот же продукт и сервис, созданы в пределах 2 ч друг от друга, похожий текст (заголовок + краткое описание).
Сравниваем только внутри корзины «продукт/сервис» и только в скользящем окне 2 ч → почти O(n).
GigaChat потом подтверждает группу одним запросом и даёт ей название (в __init__).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .index import Index
from .text import clip, jaccard, minutes_text, plural, tokens

WINDOW = timedelta(hours=2)
SIMILARITY = 0.35
MAX_COMPARE = 60     # сравнений на инцидент внутри окна — защита от корзины на тысячи
IDS_IN_CARD = 20


def _text(inc: dict) -> str:
    return " ".join(filter(None, [inc.get("title"), inc.get("short_description")]))


def find_groups(incidents: dict[str, dict], index: Index) -> list[dict]:
    buckets: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
    for inc in incidents.values():
        if inc["state_kind"] != "active" or not inc.get("product"):
            continue
        created = index.created.get(inc["id"])
        if created is None:
            continue
        buckets[(inc["product"], inc.get("service") or "")].append((created.timestamp(), inc["id"]))

    # Жадная склейка по времени: инцидент присоединяется к открытой группе своей корзины,
    # если её первый инцидент создан не раньше чем за 2 ч и текст похож на текст первого.
    # Цепочек «A~B~C…» на сутки так не бывает — группа всегда укладывается в 2 ч.
    w = WINDOW.total_seconds()
    members: list[list[str]] = []
    pair_sim: dict[str, float] = {}
    for items in buckets.values():
        if len(items) < 2:
            continue
        items.sort()
        open_groups: list[tuple[float, str, frozenset[str], list[str]]] = []
        for t, i in items:
            inc = incidents[i]
            tk = tokens(_text(inc))
            title = (inc.get("title") or "").casefold()
            open_groups = [g for g in open_groups if t - g[0] <= w]
            best, best_sim = None, 0.0
            for g in open_groups[-MAX_COMPARE:]:
                sim = 1.0 if title and title == g[1] else jaccard(tk, g[2])
                if sim >= SIMILARITY and sim > best_sim:
                    best, best_sim = g, sim
            if best:
                best[3].append(i)
                pair_sim[i] = best_sim
            else:
                g = (t, title, tk, [i])
                open_groups.append(g)
                members.append(g[3])

    groups = []
    for ids in members:
        if len(ids) < 2:
            continue
        ids.sort(key=lambda i: (index.created[i], i))
        masters = [i for i in ids if incidents[i].get("is_master")]
        master = masters[0] if masters else ids[0]
        ids.remove(master)
        ids.insert(0, master)
        m = incidents[master]
        first, last = min(index.created[i] for i in ids), max(index.created[i] for i in ids)
        span = (last - first).total_seconds() / 60
        confirmed = bool(masters) and all(incidents[i].get("is_double") for i in ids if i != master)
        groups.append({
            "id": f"dup-{master}",
            "master_id": master,
            "ids": ids,
            "size": len(ids),
            "name": clip(f"{m.get('product')}: {m.get('title')}", 80),
            "reason": f"{len(ids)} {plural(len(ids), 'похожий инцидент', 'похожих инцидента', 'похожих инцидентов')} "
                      f"по «{m.get('service') or m.get('product')}» за {minutes_text(span)}",
            "confirmed": confirmed,
            "similarity": round(min((pair_sim.get(i, 1.0) for i in ids[1:]), default=1.0), 2),
        })
    groups.sort(key=lambda g: -g["size"])
    return groups
