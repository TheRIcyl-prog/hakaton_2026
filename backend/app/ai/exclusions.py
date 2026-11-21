"""Отклонённые координатором связи между заявками."""
from collections import defaultdict


def compatible_members(ids: list[str], incidents: dict[str, dict]) -> list[str]:
    """В порядке окна убираем ранее исключённую заявку, если её пара входит в состав."""
    exclusions = [(inc_id, rejected) for inc_id in ids
                  if (rejected := incidents[inc_id].get("_outage_excluded_with"))]
    if not exclusions:
        return ids
    kept = set(ids)
    for inc_id, rejected in exclusions:
        if not kept.isdisjoint(rejected):
            kept.discard(inc_id)
    return [inc_id for inc_id in ids if inc_id in kept]


def excluded_peers(incidents: dict[str, dict]) -> dict[str, set[str]]:
    """Для проверки при склейке запрет действует в обоих направлениях."""
    peers = defaultdict(set)
    for inc_id, inc in incidents.items():
        for other in inc.get("_outage_excluded_with", ()):
            peers[inc_id].add(other)
            peers[other].add(inc_id)
    return dict(peers)
