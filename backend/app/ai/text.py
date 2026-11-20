"""Мелкие текстовые помощники ИИ-слоя: выжимка, токены для сравнения, склонения, вычистка имён."""
from __future__ import annotations

import re

SUMMARY_MAX = 90
_WORD = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)
_SENT = re.compile(r"(?<=[.!?])\s")
# Логин в скобках «Иван Петров (ipetrov)», почта, телефон
_LOGIN = re.compile(r"\b[А-ЯЁA-Z][а-яёa-z]+\s+[А-ЯЁA-Z][а-яёa-z]+\s*\([a-z0-9_.-]+\)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}")
_STOP = {
    "и", "в", "во", "на", "не", "по", "с", "со", "у", "к", "о", "об", "от", "до", "за", "из", "для", "при", "что",
    "как", "а", "но", "или", "же", "то", "это", "все", "всё", "нет", "есть", "без", "под", "над",
}


def clip(text: str | None, limit: int = SUMMARY_MAX) -> str | None:
    """Обрезает по границе слова, с многоточием."""
    if not text:
        return None
    s = " ".join(str(text).split())
    if len(s) <= limit:
        return s
    cut = s[: limit - 1]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:—-") + "…"


def first_sentence(text: str | None) -> str | None:
    if not text:
        return None
    return _SENT.split(text.strip(), maxsplit=1)[0]


def tokens(text: str | None) -> frozenset[str]:
    """Основы слов (первые 5 букв) без служебных — для грубого сравнения текстов."""
    if not text:
        return frozenset()
    return frozenset(w[:5] for w in (m.group(0).casefold() for m in _WORD.finditer(text))
                     if len(w) > 2 and w not in _STOP)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def scrub(text: str | None, names: list[str | None] = ()) -> str | None:
    """Убирает из текста имена сотрудников/инициаторов, логины, почту и телефоны — в GigaChat их не шлём."""
    if not text:
        return text
    s = text
    parts: set[str] = set()
    for n in names:
        if not n:
            continue
        parts.add(n.strip())                                         # «Иван Петров (ipetrov)»
        parts.add(re.sub(r"\s*\(.*?\)\s*", " ", n).strip())          # «Иван Петров»
        parts.update(m.strip() for m in re.findall(r"\(([^)]*)\)", n))  # «ipetrov»
    for part in sorted(parts, key=len, reverse=True):               # длинные первыми, иначе остаются хвосты
        if len(part) >= 3:
            s = re.sub(rf"\(?{re.escape(part)}\)?", "[сотрудник]", s)
    s = _LOGIN.sub("[сотрудник]", s)
    s = _EMAIL.sub("[почта]", s)
    return _PHONE.sub("[телефон]", s)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def minutes_text(minutes: float) -> str:
    m = max(1, round(minutes))
    if m < 60:
        return f"{m} мин"
    h, rest = divmod(m, 60)
    return f"{h} ч {rest} мин" if rest else f"{h} ч"
