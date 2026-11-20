"""Фоновый LLM-слой: очередь в один поток (у бесплатного GigaChat один поток), кэш на диске.

Числа (уверенность, риск, прогноз) считает код. LLM пишет слова: выжимку, выбор команды
ТОЛЬКО из кандидатов, которых нашёл код, и объяснение. Если LLM недоступен — остаются правила.
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
import time
from pathlib import Path

from .gigachat import GigaChatClient

log = logging.getLogger("dashboard.ai")
PROMPT_VERSION = "v1"
RETRY_AFTER_ERROR = 30.0

SYSTEM = (
    "Ты помощник координатора массовых инцидентов в МТС. Отвечаешь кратко, по делу, на русском. "
    "Не выдумываешь факты, которых нет во входных данных. Отвечаешь строго одним JSON-объектом без пояснений."
)


def build_prompt(inc: dict, candidates: list[dict]) -> str:
    fields = {
        "номер": inc["id"],
        "заголовок": inc.get("title"),
        "описание": inc.get("description"),
        "продукт": inc.get("product"),
        "сервис": inc.get("service"),
        "сценарий": inc.get("scenario"),
        "статус": inc.get("state"),
        "приоритет": inc.get("priority"),
        "влияние": inc.get("impact"),
        "срочность": inc.get("urgency"),
        "тип сервиса": inc.get("service_type"),
        "текущая команда": inc.get("team"),
    }
    parts = ["Инцидент:", json.dumps(fields, ensure_ascii=False, indent=1)]
    risk = (inc.get("ai") or {}).get("risk")
    if risk:
        parts.append(f"Риск стать критическим (посчитан кодом): {risk['score']}/100, факторы: {', '.join(risk['factors']) or 'нет'}.")
    if candidates:
        parts.append("Команды-кандидаты для передачи (найдены по похожим инцидентам):")
        for c in candidates:
            parts.append(f"- {c['team']} — доля похожих случаев {round(c['score'] * 100)}%")
        parts.append("Выбери команду ТОЛЬКО из этого списка.")
    parts.append(
        "Верни JSON: {"
        '"summary": "что сломалось и на кого влияет, до 200 символов", '
        + ('"route_team": "точное название команды из списка", "route_reason": "почему она, 1 предложение", ' if candidates else "")
        + ('"risk_comment": "чем опасен этот инцидент и на что смотреть, 1 предложение"' if risk else '"risk_comment": null')
        + "}"
    )
    return "\n".join(parts)


def parse_answer(text: str, candidates: list[dict]) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("в ответе нет JSON")
    data = json.loads(m.group(0))
    out = {"summary": str(data.get("summary") or "").strip()[:240] or None,
           "risk_comment": (str(data["risk_comment"]).strip()[:240] if data.get("risk_comment") else None),
           "route_team": None, "route_reason": None}
    team = str(data.get("route_team") or "").strip()
    names = {c["team"].casefold(): c["team"] for c in candidates}
    if team and team.casefold() in names:  # выбор только из кандидатов
        out["route_team"] = names[team.casefold()]
        out["route_reason"] = str(data.get("route_reason") or "").strip()[:240] or None
    return out


class LLMWorker:
    def __init__(self, client: GigaChatClient, cache_file: Path):
        self.client = client
        self.cache_file = cache_file
        self.cache: dict[str, dict] = self._load()
        self.q: queue.Queue[tuple[int, float, str, dict, list[dict]]] = queue.Queue()
        self.pending: set[str] = set()
        self.lock = threading.Lock()
        self.dirty = False
        self.status = {"provider": "gigachat", "state": "starting", "error": None, "done": 0, "queue": 0}
        self._next_try = 0.0
        threading.Thread(target=self._run, daemon=True, name="llm-worker").start()

    # ----- кэш -----
    def _load(self) -> dict:
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.cache_file)
        except OSError:
            log.exception("не удалось сохранить кэш ИИ")

    @staticmethod
    def key(inc: dict, candidates: list[dict]) -> str:
        raw = json.dumps([PROMPT_VERSION, inc.get("hash"), [c["team"] for c in candidates],
                          bool((inc.get("ai") or {}).get("risk"))], ensure_ascii=False)
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    # ----- API для enrich -----
    def get(self, inc: dict, candidates: list[dict], priority: int) -> dict | None:
        k = self.key(inc, candidates)
        with self.lock:
            if k in self.cache:
                return self.cache[k]
            if k not in self.pending:
                self.pending.add(k)
                self.q.put((priority, time.monotonic(), k, inc, candidates))
                self.status["queue"] = self.q.qsize()
        return None

    def consume_dirty(self) -> bool:
        with self.lock:
            d, self.dirty = self.dirty, False
            return d

    # ----- поток -----
    def _run(self) -> None:
        # PriorityQueue-поведение вручную: берём всё, сортируем, обрабатываем самое важное
        while True:
            item = self.q.get()
            batch = [item]
            while not self.q.empty():
                batch.append(self.q.get_nowait())
            batch.sort(key=lambda x: (x[0], x[1]))
            for i, (_, _, k, inc, cands) in enumerate(batch):
                wait = self._next_try - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    text = self.client.chat([{"role": "system", "content": SYSTEM},
                                             {"role": "user", "content": build_prompt(inc, cands)}])
                    result = parse_answer(text, cands)
                    with self.lock:
                        self.cache[k] = result
                        self.pending.discard(k)
                        self.dirty = True
                        self.status.update(state="ok", error=None, done=self.status["done"] + 1)
                    self._save()
                except Exception as e:  # noqa: BLE001 — LLM не должен ронять сервер
                    log.warning("LLM: %s", e)
                    with self.lock:
                        self.pending.discard(k)  # попробуем снова при следующем enrich
                        self.status.update(state="error", error=str(e)[:200])
                    self._next_try = time.monotonic() + RETRY_AFTER_ERROR
                finally:
                    self.status["queue"] = self.q.qsize() + len(batch) - i - 1
