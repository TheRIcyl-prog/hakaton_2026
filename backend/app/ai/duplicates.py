"""Кандидаты в дубли (п. 8.4) — дёшево, без LLM. Два уровня:

1. «text» — тот же продукт и сервис, в пределах 2 ч, похожий текст (заголовок + описания без слов из названий
   продукта/сервиса). Видны сразу, даже без GigaChat.
2. «burst» — всплеск: ≥3 активных по одному сервису за 2 ч, тексты разные (люди описывают одну поломку
   по-разному — правилами это не поймать). Видны, только если GigaChat подтвердил, что это один сбой.
Сравниваем только внутри корзины «продукт/сервис» и только в окне 2 ч → почти O(n).
GigaChat подтверждает группу одним запросом и даёт ей название (в __init__).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .exclusions import compatible_members, excluded_peers
from .index import Index
from .text import clip, minutes_text, plural, tokens

WINDOW = timedelta(hours=2)
SIMILARITY = 0.5     # доля общих основ слов от меньшего текста
MIN_COMMON = 2
BURST_MIN = 3
BURST_SHOWN = 12     # столько участников всплеска показываем GigaChat
MAX_COMPARE = 60     # сравнений на инцидент внутри окна — защита от корзины на тысячи
IDS_IN_CARD = 20


def _text(inc: dict) -> str:
    return " ".join(filter(None, [inc.get("title"), inc.get("short_description"), (inc.get("description") or "")[:300]]))


def _similar(a: frozenset[str], b: frozenset[str]) -> float:
    common = len(a & b)
    if common < MIN_COMMON:
        return 0.0
    return common / min(len(a), len(b))


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
    exclusions = excluded_peers(incidents)
    for items in buckets.values():
        if len(items) < 2:
            continue
        items.sort()
        first = incidents[items[0][1]]
        common = tokens(" ".join(filter(None, [first.get("product"), first.get("service"), first.get("scenario")])))
        # Время первого, заголовки, основы слов, участники и несовместимые с ними заявки.
        open_groups: list[tuple[float, set[str], list[frozenset[str]], list[str], set[str]]] = []
        for t, i in items:
            inc = incidents[i]
            tk = tokens(_text(inc)) - common
            title = (inc.get("title") or "").casefold()
            open_groups = [g for g in open_groups if t - g[0] <= w]
            best, best_sim = None, 0.0
            for g in open_groups[-MAX_COMPARE:]:
                if i in g[4]:
                    continue
                sim = 1.0 if title and title in g[1] else max((_similar(tk, x) for x in g[2][:20]), default=0.0)
                if sim >= SIMILARITY and sim > best_sim:
                    best, best_sim = g, sim
            if best:
                best[1].add(title)
                best[2].append(tk)
                best[3].append(i)
                best[4].update(exclusions.get(i, ()))
                pair_sim[i] = best_sim
            else:
                g = (t, {title}, [tk], [i], set(exclusions.get(i, ())))
                open_groups.append(g)
                members.append(g[3])

    groups = [_group(ids, incidents, index, "text", pair_sim) for ids in members if len(ids) >= 2]
    in_text = {i: g["id"] for g in groups for i in g["ids"]}
    groups += _bursts(buckets, incidents, index, in_text)
    groups.sort(key=lambda g: (g["tier"] != "text", -g["size"]))
    return groups


def _bursts(buckets, incidents: dict[str, dict], index: Index, in_text: dict[str, str]) -> list[dict]:
    """Всплески по сервису: окно 2 ч от первого, ≥3 активных, хотя бы один на экране, не одна text-группа."""
    w = WINDOW.total_seconds()
    out = []
    for items in buckets.values():
        if len(items) < BURST_MIN:
            continue
        k = 0
        while k < len(items):
            start = items[k][0]
            j = k
            while j < len(items) and items[j][0] - start <= w:
                j += 1
            ids = compatible_members([i for _, i in items[k:j]], incidents)
            k = j
            if len(ids) < BURST_MIN:
                continue
            if len({in_text.get(i, i) for i in ids}) < 2:
                continue        # всё уже одна группа по тексту
            if not any(x not in incidents[i].get("resolved_in", ()) for i in ids for x in incidents[i].get("groups", ())):
                continue        # никого не видно на экране — GigaChat не тратим
            g = _group(ids, incidents, index, "burst", {})
            g["id"] = f"burst-{ids[0]}"
            g["hidden"] = True
            out.append(g)
    return out


def _group(ids: list[str], incidents: dict[str, dict], index: Index, tier: str, pair_sim: dict[str, float]) -> dict:
    ids = sorted(ids, key=lambda i: (index.created[i], i))
    masters = [i for i in ids if incidents[i].get("is_master")]
    master = masters[0] if masters else ids[0]
    ids.remove(master)
    ids.insert(0, master)
    m = incidents[master]
    first, last = min(index.created[i] for i in ids), max(index.created[i] for i in ids)
    span = (last - first).total_seconds() / 60
    word = plural(len(ids), "похожий инцидент", "похожих инцидента", "похожих инцидентов") if tier == "text" else         plural(len(ids), "инцидент", "инцидента", "инцидентов")
    return {
        "id": f"dup-{master}",
        "tier": tier,
        "master_id": master,
        "ids": ids,
        "size": len(ids),
        "name": clip(f"{m.get('product')}: {m.get('title')}", 80),
        "reason": f"{len(ids)} {word} по «{m.get('service') or m.get('product')}» за {minutes_text(span)}",
        "confirmed": bool(masters) and all(incidents[i].get("is_double") for i in ids if i != master),
        "similarity": round(min((pair_sim.get(i, 1.0) for i in ids[1:]), default=1.0), 2) if tier == "text" else None,
    }
