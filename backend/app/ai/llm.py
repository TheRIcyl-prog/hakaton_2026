"""Фоновый GigaChat: очередь с приоритетами, лимит в минуту, кэш на диске.
Потоков по умолчанию 1: бесплатный GIGACHAT_API_PERS на второй одновременный запрос отвечает 429.
Платный тариф — GIGACHAT_CONCURRENCY=2..3.

Снимок никогда не ждёт GigaChat: enrich() берёт готовое из кэша, а чего нет — ставит в очередь.
Кэш — по hash входного текста (`cache/ai_cache.json`), поэтому перезапуск не тратит запросы.
Нет сети / лимит → все потоки ждут RETRY_SECONDS, статус «нет связи», остаются правила.
Числа (уверенность, риск) считает код; GigaChat пишет только слова и выбирает команду из кандидатов.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..adapter import iso
from .gigachat import GigaChatModelUnavailable
from .text import SUMMARY_MAX, clip, scrub

log = logging.getLogger("dashboard.ai")
PROMPT_VERSION = "v3"
RETRY_SECONDS = 60.0
RATE_BACKOFF = 5.0     # 429 (лимит/второй поток у бесплатного тарифа): 5 → 10 → 20 → … → RETRY_SECONDS
SAVE_EVERY = 2.0
DIRTY_EVERY = 2.0
MAX_FAILS = 2          # плохой ответ дважды — больше этот ключ не спрашиваем до перезапуска

SYSTEM = (
    "Ты помощник координатора массовых инцидентов в МТС. Отвечаешь кратко, по делу, на русском. "
    "Не выдумываешь факты, которых нет во входных данных. Отвечаешь строго одним JSON-объектом без пояснений."
)


def _h(*parts) -> str:
    return hashlib.sha1(json.dumps([PROMPT_VERSION, *parts], ensure_ascii=False, default=str).encode()).hexdigest()[:20]


def _digitless(s: str) -> str:
    return re.sub(r"\d+", "#", s)


# ---------- входные данные без персональных данных ----------
def incident_fields(inc: dict) -> dict:
    """То, что уходит в GigaChat. Имён сотрудников и инициаторов здесь нет и быть не должно."""
    names = [inc.get("assignee"), inc.get("initiator"), inc.get("assignee_login")]
    return {
        "заголовок": scrub(inc.get("title"), names),
        "краткое описание": scrub(inc.get("short_description"), names),
        "описание": clip(scrub(inc.get("description"), names), 600),
        "продукт": inc.get("product"),
        "сервис": inc.get("service"),
        "сценарий": inc.get("scenario"),
        "приоритет": inc.get("priority"),
        "влияние": inc.get("impact"),
        "срочность": inc.get("urgency"),
        "тип сервиса": inc.get("service_type"),
    }


def text_key(fields: dict) -> list:
    return [fields["заголовок"], fields["краткое описание"], fields["описание"], fields["продукт"], fields["сервис"],
            fields["сценарий"]]


# ---------- ключи кэша по частям ----------
def summary_key(fields: dict) -> str:
    return _h("summary", text_key(fields))


def route_key(fields: dict, cands: list[dict], feedback: dict | None = None) -> str:
    teams = [c["team"] for c in cands]
    if feedback:
        return _h("route_feedback", text_key(fields), teams, feedback)
    return _h("route", text_key(fields), teams)


def model_key(key: str, model: str | None) -> str:
    return _h("model", model, key) if model and model != "GigaChat" else key


def risk_key(fields: dict, risk: dict) -> str:
    return _h("risk", text_key(fields), fields["приоритет"], sorted(_digitless(f) for f in risk["factors"]))


def dynamics_key(fields: dict, dyn: dict) -> str:
    return _h("dyn", text_key(fields), [(p["kind"], p["count"]) for p in dyn["patterns"]])


def dup_key(members: list[dict]) -> str:
    return _h("dup", sorted((m["id"], m["title"]) for m in members))


OUTAGE_FIELDS = (
    ("product", "продукт", 120),
    ("service", "сервис", 120),
    ("channel", "канал", 80),
    ("created_at", "время", 40),
    ("title", "заголовок", 120),
    ("short_description", "краткое описание", 240),
    ("description", "описание", 400),
)
PERSON_FIELDS = ("assignee", "initiator", "assignee_login")


def outage_key(code: str, members: list[dict]) -> str:
    fields = ("id", *(field for field, _, _ in OUTAGE_FIELDS), *PERSON_FIELDS)
    return _h("outage", code, [[inc.get(field) for field in fields] for inc in members])


# ---------- промпты ----------
def build_incident_prompt(fields: dict, parts: dict) -> str:
    """parts: {"summary": True, "route": {"cands": [...], "total": n}, "risk": {...}, "dynamics": {...}}"""
    lines = ["Инцидент:", json.dumps({k: v for k, v in fields.items() if v}, ensure_ascii=False, indent=1)]
    answer = []
    if "summary" in parts:
        answer.append(f'"summary": "одна строка до {SUMMARY_MAX} знаков без восклицаний: что сломалось и у кого '
                      f'(продукт/сервис и кого затронуло)"')
    if "route" in parts:
        r = parts["route"]
        lines.append("Сейчас инцидент у координаторов, его надо передать команде. Кандидаты и история похожих инцидентов:")
        for c in r["cands"]:
            lines.append(f"- {c['team']}: вела {c['count']} из {r['total']} похожих, решила {c['solved']}")
        if feedback := r.get("feedback"):
            lines.append("Отдельно от истории передач — исправления координаторов для похожих заявок:")
            lines.append(json.dumps(feedback, ensure_ascii=False))
            lines.append("Исправления отражают выбор координаторов, а не подтверждённые решения.")
        lines.append("Выбери команду ТОЛЬКО из этого списка.")
        answer.append('"route_team": "точное название команды из списка"')
        answer.append('"route_reason": "1–2 предложения: почему по сути проблемы (какой сервис или компонент она '
                      'обслуживает, какой симптом); не пиши общих слов вроде «наибольший опыт», цифры не повторяй"')
    if "risk" in parts:
        rk = parts["risk"]
        lines.append(f"Риск стать критическим (посчитан кодом): {rk['score']}/100. Факторы: {'; '.join(rk['factors']) or 'нет'}.")
        answer.append('"risk_comment": "1 предложение: что конкретно может случиться дальше и что проверить первым; '
                      'не перечисляй факторы и не называй баллы"')
    if "dynamics" in parts:
        lines.append(f"Тревожная динамика по истории изменений: {parts['dynamics']['text']}.")
        answer.append('"dynamics_comment": "одна фраза до 120 знаков: что эта динамика говорит о сбое и что сделать '
                      'координатору; без общих слов вроде «требует вмешательства»"')
    lines.append("Верни JSON: {" + ", ".join(answer) + "}")
    return "\n".join(lines)


def build_dup_prompt(members: list[dict]) -> str:
    lines = ["Похожие инциденты, созданные почти одновременно по одному продукту и сервису:"]
    for m in members:
        lines.append(f"- {m['id']}: {m['title']}" + (f" — {m['text']}" if m.get("text") else ""))
    lines.append(
        "Это один и тот же сбой? Верни JSON: {\"same\": true/false, "
        "\"name\": \"короткое название сбоя до 60 знаков\", \"reason\": \"почему, 1 предложение\", "
        "\"exclude\": [\"номера, которые НЕ относятся к сбою\"]}"
    )
    return "\n".join(lines)


def build_outage_prompt(code: str, members: list[dict]) -> str:
    lines = [f"Кандидат на сбой между продуктами с общим кодом {clip(code, 80)}."]
    for inc in members:
        names = [inc.get(field) for field in PERSON_FIELDS]
        entry = {label: clip(scrub(inc[field], names), limit)
                 for field, label, limit in OUTAGE_FIELDS if inc.get(field)}
        lines.append(json.dumps({"id": inc["id"], **entry}, ensure_ascii=False))
    lines.append(
        "Общий код и близкое время — повод проверить связь. Оцени, есть ли общие технические признаки "
        "в симптомах и сервисах. Если признаков недостаточно, верни same=false. "
        "При частичном совпадении перечисли посторонние заявки в exclude. Причину формулируй как гипотезу. "
        'Верни JSON: {"same": true/false, "name": "название до 60 знаков", '
        '"reason": "краткое объяснение по наблюдаемым признакам", "exclude": ["id из списка"]}'
    )
    return "\n".join(lines)


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        data = json.loads(m.group(0))
    elif re.match(r'\s*"\w+"\s*:', text):
        data = json.loads("{" + text.strip().rstrip(",") + "}")   # модель забыла фигурные скобки
    else:
        raise ValueError("в ответе нет JSON")
    if not isinstance(data, dict):
        raise ValueError("ответ не объект")
    return data


def _s(v, limit: int) -> str | None:
    return clip(str(v).strip(), limit) if v not in (None, "", "null") else None


def parse_incident_answer(text: str, parts: dict) -> dict[str, dict | None]:
    try:
        data = _json(text)
    except ValueError:
        # Просили только выжимку, а модель ответила строкой без JSON — это и есть выжимка
        plain = text.strip().strip("`\"«» ").splitlines()[0].strip() if text.strip() else ""
        if set(parts) == {"summary"} and plain and "{" not in plain:
            data = {"summary": plain}
        else:
            raise
    out: dict[str, dict | None] = {}
    if "summary" in parts:
        s = _s(data.get("summary"), SUMMARY_MAX)
        out["summary"] = {"text": s} if s else None
    if "route" in parts:
        names = {c["team"].casefold(): c["team"] for c in parts["route"]["cands"]}
        team = str(data.get("route_team") or "").strip().strip("«»\"").casefold()
        out["route"] = {"team": names[team], "reason": _s(data.get("route_reason"), 300)} if team in names else None
    if "risk" in parts:
        c = _s(data.get("risk_comment"), 200)
        out["risk"] = {"comment": c} if c else None
    if "dynamics" in parts:
        c = _s(data.get("dynamics_comment"), 160)
        out["dynamics"] = {"comment": c} if c else None
    return out


def parse_dup_answer(text: str, ids: list[str]) -> dict:
    data = _json(text)
    same = data.get("same")
    if isinstance(same, str):
        same = same.strip().lower() in ("true", "да", "yes")
    exclude = [str(x).strip() for x in (data.get("exclude") or []) if str(x).strip() in ids]
    return {"same": bool(same), "name": _s(data.get("name"), 80), "reason": _s(data.get("reason"), 240),
            "exclude": exclude}


def parse_outage_answer(text: str, ids: list[str]) -> dict:
    data = _json(text)
    same, exclude = data.get("same"), data.get("exclude", [])
    if not isinstance(same, bool) or not isinstance(exclude, list) or any(inc_id not in ids for inc_id in exclude):
        raise ValueError("ожидались same и список id участников в exclude")
    name, reason = data.get("name"), data.get("reason")
    return {"same": same, "exclude": exclude, "name": _s(name, 60) if isinstance(name, str) else None,
            "reason": _s(reason, 240) if isinstance(reason, str) else None}


# ---------- очередь ----------
@dataclass
class Task:
    key: str                    # ключ задачи (набор частей)
    priority: float
    prompt: str
    parts: dict                 # {имя части: ключ кэша}
    kind: str = "incident"      # incident | dup | outage
    meta: dict = field(default_factory=dict)
    seq: int = 0
    advanced: bool = False


def _real_now() -> str:
    return iso(datetime.now(timezone.utc))


class LLMWorker:
    def __init__(self, client, cache_file, concurrency: int = 1, rpm: int = 20, retry_seconds: float = RETRY_SECONDS,
                 advanced_model: str | None = None):
        self.client = client
        self.base_model = getattr(client, "model", "GigaChat")
        self.advanced_model = advanced_model if advanced_model != self.base_model else None
        self._advanced_unavailable = False
        self.cache_file = cache_file
        self.rpm = max(1, rpm)
        self.retry_seconds = retry_seconds
        self.cache: dict[str, dict] = self._load()
        self.from_disk = set(self.cache)
        self.fails: dict[str, int] = {}
        self.wanted: dict[str, Task] = {}
        self.in_flight: set[str] = set()
        self.cv = threading.Condition()
        self.calls: deque[float] = deque()
        self.pause_until = 0.0
        self.dirty = False
        self._dirty_at = 0.0
        self._saved_at = 0.0
        self._unsaved = False
        self._seq = 0
        self._limit_hits = 0
        self.status = {"provider": "gigachat", "state": "starting", "label": "ИИ: GigaChat подключается", "error": None,
                       "queue": 0, "in_flight": 0, "done": 0, "cached": len(self.cache), "last_ok_at": None,
                       "next_retry_at": None}
        for n in range(max(1, concurrency)):
            threading.Thread(target=self._run, daemon=True, name=f"llm-worker-{n}").start()

    # ----- кэш -----
    def _load(self) -> dict:
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, force: bool = False) -> None:
        with self.cv:
            if not self._unsaved or (not force and time.monotonic() - self._saved_at < SAVE_EVERY):
                return
            # Ответы базовой модели на расширенные запросы живут до перезапуска.
            snapshot = json.dumps({k: v for k, v in self.cache.items() if not v.get("fallback")}, ensure_ascii=False)
            self._unsaved = False
            self._saved_at = time.monotonic()
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(snapshot, encoding="utf-8")
            tmp.replace(self.cache_file)
        except OSError:
            log.exception("не удалось сохранить кэш ИИ")

    def lookup(self, key: str) -> tuple[dict | None, str, str | None] | None:
        """→ (значение или None если GigaChat не дал ответа, источник gigachat|cache, время) или None, если не спрашивали."""
        e = self.cache.get(key)
        if e is None:
            return None
        return e.get("v"), ("cache" if key in self.from_disk else "gigachat"), e.get("at")

    def known(self, key: str) -> bool:
        return key in self.cache or self.fails.get(key, 0) >= MAX_FAILS

    # ----- API для enrich -----
    def submit(self, tasks: list[Task]) -> set[str]:
        """Заменяет очередь актуальным списком. → ключи задач, которые ждут или в работе."""
        with self.cv:
            new: dict[str, Task] = {}
            for t in tasks:
                if t.key in self.in_flight:
                    continue
                old = self.wanted.get(t.key)
                if old:
                    old.priority = t.priority
                    new[t.key] = old
                else:
                    self._seq += 1
                    t.seq = self._seq
                    new[t.key] = t
            self.wanted = new
            self.status["queue"] = len(new)
            self.cv.notify_all()
            return set(new) | set(self.in_flight)

    def consume_dirty(self) -> bool:
        with self.cv:
            if self.dirty and time.monotonic() - self._dirty_at >= DIRTY_EVERY:
                self.dirty = False
                self._dirty_at = time.monotonic()
                return True
            return False

    def snapshot_status(self) -> dict:
        with self.cv:
            st = dict(self.status)
            st["queue"] = len(self.wanted)
            st["in_flight"] = len(self.in_flight)
            st["cached"] = len(self.cache)
            if st["state"] == "starting" and not self.wanted and not self.in_flight:
                st.update(state="ok", label="ИИ: GigaChat")
            return st

    # ----- потоки -----
    def _take(self) -> Task:
        with self.cv:
            while True:
                now = time.monotonic()
                if now < self.pause_until:
                    self.cv.wait(self.pause_until - now)
                    continue
                while self.calls and now - self.calls[0] > 60:
                    self.calls.popleft()
                if len(self.calls) >= self.rpm:
                    self.cv.wait(60 - (now - self.calls[0]) + 0.05)
                    continue
                if not self.wanted:
                    self.cv.wait(5)
                    continue
                t = min(self.wanted.values(), key=lambda x: (x.priority, x.seq))
                del self.wanted[t.key]
                self.in_flight.add(t.key)
                self.calls.append(now)
                return t

    def _run(self) -> None:
        while True:
            t = self._take()
            model = self.advanced_model if t.advanced and not self._advanced_unavailable else None
            messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": t.prompt}]
            try:
                text = self.client.chat(messages, model=model) if model else self.client.chat(messages)
            except Exception as e:  # noqa: BLE001 — сеть/лимит/TLS: ждём и повторяем, сервер не роняем
                if model and isinstance(e, GigaChatModelUnavailable):
                    with self.cv:
                        self._advanced_unavailable = True
                        self.in_flight.discard(t.key)
                        self.wanted.setdefault(t.key, t)  # Повтор проходит через общий лимит запросов.
                        self.cv.notify_all()
                    log.info("%s. Запросы перейдут на базовую модель до перезапуска", e)
                    continue
                limited = getattr(e, "rate_limited", False)
                log.warning("GigaChat %s: %s", "лимит" if limited else "недоступен", e)
                with self.cv:
                    self.in_flight.discard(t.key)
                    self.wanted.setdefault(t.key, t)        # задача вернётся в очередь
                    if limited:
                        self._limit_hits += 1
                        wait = min(self.retry_seconds, RATE_BACKOFF * 2 ** (self._limit_hits - 1))
                        state, label = "limited", "ИИ: GigaChat — лимит запросов, ответы задерживаются"
                    else:
                        wait = self.retry_seconds
                        state, label = "offline", "ИИ: нет связи с GigaChat, работают правила"
                    self.pause_until = max(self.pause_until, time.monotonic() + wait)
                    self.status.update(state=state, label=label, error=str(e)[:200],
                                       next_retry_at=iso(datetime.fromtimestamp(time.time() + wait, timezone.utc)))
                    self.cv.notify_all()
                continue
            try:
                if t.kind == "dup":
                    results = {"dup": parse_dup_answer(text, t.meta["ids"])}
                elif t.kind == "outage":
                    results = {"outage": parse_outage_answer(text, t.meta["ids"])}
                else:
                    results = parse_incident_answer(text, t.meta["parts"])
                bad = False
            except Exception as e:  # noqa: BLE001 — кривой ответ модели
                log.warning("GigaChat: непонятный ответ (%s): %s", e, text[:200])
                results, bad = {}, True
            at = _real_now()
            with self.cv:
                self.in_flight.discard(t.key)
                if bad:
                    for k in t.parts.values():
                        self.fails[k] = self.fails.get(k, 0) + 1
                else:
                    for name, k in t.parts.items():
                        self.cache[k] = {"v": results.get(name), "at": at}
                        if t.advanced and self.advanced_model and not model:
                            self.cache[k]["fallback"] = True
                        self.from_disk.discard(k)
                    self._unsaved = True
                    self.dirty = True
                self._limit_hits = 0
                self.status.update(state="ok", label="ИИ: GigaChat", error=None, last_ok_at=at, next_retry_at=None,
                                   done=self.status["done"] + 1)
            self.save()
