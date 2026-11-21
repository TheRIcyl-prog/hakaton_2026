"""Минимальный клиент GigaChat на httpx (OAuth + chat/completions).

TLS: сертификаты GigaChat выданы Russian Trusted Root CA (Минцифры), которого нет в системном
хранилище Windows. Путь к PEM задаётся в GIGACHAT_CA_BUNDLE. Проверку сертификата не отключаем.
"""
from __future__ import annotations

import ssl
import time
import uuid
from pathlib import Path

import httpx

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://api.giga.chat/v1"
DEFAULT_MODEL = "GigaChat-2"


class GigaChatError(RuntimeError):
    pass


class GigaChatRateLimit(GigaChatError):
    """429: слишком много запросов. У бесплатного GIGACHAT_API_PERS — один поток одновременно."""
    rate_limited = True


class GigaChatModelUnavailable(GigaChatError):
    """Запрошенная расширенная модель недоступна для генерации."""


class GigaChatClient:
    def __init__(self, credentials: str, scope: str = "GIGACHAT_API_PERS", ca_bundle: Path | None = None,
                 model: str = DEFAULT_MODEL, timeout: float = 40.0):
        if not credentials.strip():
            raise GigaChatError("GIGACHAT_CREDENTIALS не задан")
        self.credentials = credentials.strip()
        self.scope = scope
        self.model = model
        self.timeout = timeout
        self.verify: ssl.SSLContext | bool = True
        if ca_bundle and ca_bundle.exists():
            try:
                self.verify = ssl.create_default_context()
                self.verify.load_verify_locations(cafile=str(ca_bundle))
            except (OSError, ValueError) as e:
                raise GigaChatError("не удалось загрузить сертификат GIGACHAT_CA_BUNDLE") from e
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
        except httpx.RequestError as e:
            raise GigaChatError(_request_error("OAuth", e)) from e
        if r.status_code != 200:
            raise GigaChatError(f"OAuth {r.status_code}")
        data = r.json()
        self._token = data["access_token"]
        expires = data.get("expires_at") or time.time() + 1500
        self._token_exp = expires / 1000 if expires >= 100_000_000_000 else expires
        return self._token

    def list_models(self) -> list[str]:
        """Запрос каталога для текущего OAuth-токена, без генерации и вывода ключа."""
        token = self._auth()
        try:
            r = httpx.get(f"{API_URL}/models", headers={"Authorization": f"Bearer {token}",
                                                       "Accept": "application/json"},
                          verify=self.verify, timeout=self.timeout)
        except httpx.RequestError as e:
            raise GigaChatError(_request_error("Каталог моделей", e)) from e
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
        except httpx.RequestError as e:
            raise GigaChatError(_request_error("Генерация", e)) from e
        if r.status_code == 401:
            self._token = None
            raise GigaChatError("токен истёк, повторим")
        if r.status_code == 429:
            raise GigaChatRateLimit("лимит запросов GigaChat (429)")
        if model and r.status_code in (402, 403, 404):
            raise GigaChatModelUnavailable(f"Расширенная модель GigaChat недоступна (HTTP {r.status_code})")
        if r.status_code != 200:
            raise GigaChatError(f"Генерация GigaChat: HTTP {r.status_code}")
        return r.json()["choices"][0]["message"]["content"]


def _request_error(stage: str, e: httpx.RequestError) -> str:
    detail = str(e).upper()
    if isinstance(e, httpx.TimeoutException):
        message = "истекло время TLS-соединения" if "HANDSHAKE" in detail else "истекло время ожидания"
    elif "CERTIFICATE" in detail or "SSL" in detail:
        message = "ошибка TLS, проверьте GIGACHAT_CA_BUNDLE"
    else:
        message = "нет связи с GigaChat"
    return f"{stage}: {message}"
