"""Минимальный клиент GigaChat на httpx (OAuth + chat/completions).

TLS: сертификаты GigaChat выданы Russian Trusted Root CA (Минцифры), которого нет в системном
хранилище Windows. Путь к PEM задаётся в GIGACHAT_CA_BUNDLE. Проверку сертификата не отключаем.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1"


class GigaChatError(RuntimeError):
    pass


class GigaChatRateLimit(GigaChatError):
    """429: слишком много запросов. У бесплатного GIGACHAT_API_PERS — один поток одновременно."""
    rate_limited = True


class GigaChatModelUnavailable(GigaChatError):
    """Запрошенная расширенная модель недоступна для генерации."""


class GigaChatClient:
    def __init__(self, credentials: str, scope: str = "GIGACHAT_API_PERS", ca_bundle: Path | None = None,
                 model: str = "GigaChat", timeout: float = 40.0):
        if not credentials.strip():
            raise GigaChatError("GIGACHAT_CREDENTIALS не задан")
        self.credentials = credentials.strip()
        self.scope = scope
        self.model = model
        self.timeout = timeout
        self.verify: str | bool = str(ca_bundle) if ca_bundle and ca_bundle.exists() else True
        self._token: str | None = None
        self._token_exp = 0.0

    def _auth(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        try:
            r = httpx.post(
                OAUTH_URL,
                headers={
                    "Authorization": f"Basic {self.credentials}",
                    "RqUID": str(uuid.uuid4()),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data={"scope": self.scope},
                verify=self.verify,
                timeout=self.timeout,
            )
        except httpx.ConnectError as e:
            raise GigaChatError(_tls_hint(e)) from e
        if r.status_code != 200:
            raise GigaChatError(f"OAuth {r.status_code}")
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = data.get("expires_at", 0) / 1000 or time.time() + 1500
        return self._token

    def list_models(self) -> list[str]:
        """Запрос каталога для текущего OAuth-токена, без генерации и вывода ключа."""
        token = self._auth()
        try:
            r = httpx.get(f"{API_URL}/models", headers={"Authorization": f"Bearer {token}",
                                                       "Accept": "application/json"},
                          verify=self.verify, timeout=self.timeout)
        except httpx.ConnectError as e:
            raise GigaChatError(_tls_hint(e)) from e
        if r.status_code == 401:
            self._token = None
        if r.status_code != 200:
            raise GigaChatError(f"Список моделей GigaChat: HTTP {r.status_code}")
        return [model["id"] for model in r.json()["data"]]

    def chat(self, messages: list[dict], temperature: float = 0.2, max_tokens: int = 600,
             *, model: str | None = None) -> str:
        token = self._auth()
        try:
            r = httpx.post(
                f"{API_URL}/chat/completions",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                json={"model": model or self.model, "messages": messages, "temperature": temperature,
                      "max_tokens": max_tokens},
                verify=self.verify,
                timeout=self.timeout,
            )
        except httpx.ConnectError as e:
            raise GigaChatError(_tls_hint(e)) from e
        if r.status_code == 401:
            self._token = None
            raise GigaChatError("токен истёк, повторим")
        if r.status_code == 429:
            raise GigaChatRateLimit("лимит запросов GigaChat (429)")
        if model and r.status_code in (402, 403, 404):
            raise GigaChatModelUnavailable(f"Расширенная модель GigaChat недоступна (HTTP {r.status_code})")
        if r.status_code != 200:
            raise GigaChatError(f"chat {r.status_code}: {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]


def _tls_hint(e: Exception) -> str:
    msg = str(e)
    if "CERTIFICATE" in msg.upper() or "SSL" in msg.upper():
        return "TLS: нужен сертификат Russian Trusted Root CA (GIGACHAT_CA_BUNDLE)"
    return f"нет связи с GigaChat: {msg[:120]}"
