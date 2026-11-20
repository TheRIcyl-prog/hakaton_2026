"""ИИ-слой. Сейчас — заглушка на правилах (rules.py), чтобы фронт уже рисовал ИИ-поля.

Контракт для C: enrich(incidents, now, rules) кладёт в каждый инцидент поле "ai":
    {
      "source": "rules" | "gigachat" | "cache",
      "summary": str | None,               # выжимка до 200 символов
      "route": {                           # только для инцидентов на координаторах
          "team": str, "confidence": float 0..1, "reason": str,
          "alternatives": [{"team": str, "score": float}], "similar_ids": [str]
      } | None,
      "risk": {"score": int 0..100, "level": "low|medium|high", "factors": [str]} | None,
    }
Числа считает код, слова пишет LLM. LLM-вызовы должны идти в фоне и не блокировать опрос файла.
"""
from .rules import enrich

__all__ = ["enrich"]
