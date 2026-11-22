"""Local demo accounts and process-local coordinator sessions."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from threading import Lock
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from .config import Settings

COOKIE = "coordinator_session"
NO_STORE = {"Cache-Control": "no-store"}
ITERATIONS = 600_000
INVALID_LOGIN = "Неверный логин или пароль"
log = logging.getLogger(__name__)


def _digest(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"pbkdf2-sha256:{ITERATIONS}:{salt.hex()}:{_digest(password, salt).hex()}"


@dataclass(frozen=True)
class Profile:
    id: str
    login: str
    display_name: str


@dataclass(frozen=True)
class Account:
    profile: Profile
    salt: bytes
    digest: bytes


def _field(record: dict, key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 100 or len(value.splitlines()) != 1:
        raise ValueError
    return value.strip()


def _accounts(raw: str) -> dict[str, Account]:
    records = json.loads(raw)
    if not isinstance(records, list) or not records:
        raise ValueError
    accounts: dict[str, Account] = {}
    ids, names = set(), set()
    for record in records:
        profile = Profile(*(_field(record, key) for key in ("id", "login", "display_name")))
        match = re.fullmatch(rf"pbkdf2-sha256:{ITERATIONS}:([0-9a-f]{{32}}):([0-9a-f]{{64}})", record["password_hash"])
        login, name = profile.login.casefold(), profile.display_name.casefold()
        if not match or profile.id in ids or name in names or login in accounts:
            raise ValueError
        ids.add(profile.id)
        names.add(name)
        accounts[login] = Account(profile, *(bytes.fromhex(part) for part in match.groups()))
    return accounts


def _origins(raw: str) -> set[str]:
    origins = {origin.strip() for origin in raw.split(",")}
    for origin in origins:
        url = urlsplit(origin)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or url.path or url.query or url.fragment or any(char.isspace() for char in origin)
                or origin != f"{url.scheme}://{url.hostname if ':' not in url.hostname else '[' + url.hostname + ']'}"
                             + (f":{url.port}" if url.port is not None else "")):
            raise ValueError
    return origins


class DemoAuth:
    def __init__(self, settings: Settings):
        self.accounts: dict[str, Account] = {}
        self.origins: set[str] = set()
        self.sessions: dict[str, tuple[float, Profile]] = {}
        self.lock = Lock()
        setting = "DEMO_ORIGINS"
        try:
            self.origins = _origins(settings.demo_origins)
            setting = "DEMO_SESSION_SECONDS"
            self.ttl = int(settings.demo_session_seconds)
            if self.ttl <= 0:
                raise ValueError
            setting = "DEMO_ACCOUNTS"
            self.accounts = _accounts(settings.demo_accounts)
        except (ValueError, TypeError, KeyError):
            log.warning("Демо-вход недоступен: проверьте настройку %s", setting)

    def authenticate(self, login: str, password: str) -> Profile:
        if not self.accounts:
            raise HTTPException(503, "Демонстрационный доступ не настроен", headers=NO_STORE)
        account = self.accounts.get(login.strip().casefold())
        salt, expected = (account.salt, account.digest) if account else (bytes(16), bytes(32))
        verified = secrets.compare_digest(_digest(password, salt), expected)
        if not verified or account is None:
            raise HTTPException(401, INVALID_LOGIN, headers=NO_STORE)
        return account.profile

    def profile(self, request: Request) -> Profile | None:
        with self.lock:
            token = request.cookies.get(COOKIE)
            session = self.sessions.get(token)
            if session is None:
                return None
            if session[0] <= time.monotonic():
                self.sessions.pop(token)
                return None
            return session[1]

    def start(self, request: Request, response: Response, profile: Profile) -> None:
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.monotonic()
            self.sessions = {key: session for key, session in self.sessions.items() if session[0] > now}
            self.sessions.pop(request.cookies.get(COOKIE), None)
            self.sessions[token] = (now + self.ttl, profile)
        response.set_cookie(COOKIE, token, max_age=self.ttl, path="/", httponly=True,
                            samesite="lax", secure=request.url.scheme == "https")

    def end(self, request: Request, response: Response) -> None:
        with self.lock:
            self.sessions.pop(request.cookies.get(COOKIE), None)
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax",
                               secure=request.url.scheme == "https")


def check_origin(request: Request) -> None:
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0] not in request.app.state.auth.origins:
        raise HTTPException(403, "Недоверенный источник запроса", headers=NO_STORE)


def require_profile(request: Request, response: Response) -> Profile:
    profile = request.app.state.auth.profile(request)
    if profile is None:
        raise HTTPException(401, "Войдите снова", headers=NO_STORE)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
    response.headers.update(NO_STORE)
    return profile


CurrentProfile = Annotated[Profile, Depends(require_profile)]
router = APIRouter(prefix="/api/auth")


@router.post("/login", dependencies=[Depends(check_origin)])
async def login(request: Request, response: Response):
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Некорректный запрос", headers=NO_STORE) from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Некорректный запрос", headers=NO_STORE)
    username, password = body.get("login"), body.get("password")
    if (not isinstance(username, str) or not 1 <= len(username) <= 100
            or not isinstance(password, str) or not 1 <= len(password) <= 1024):
        raise HTTPException(401, INVALID_LOGIN, headers=NO_STORE)
    auth = request.app.state.auth
    try:
        profile = await run_in_threadpool(auth.authenticate, username, password)
    except UnicodeError:
        raise HTTPException(401, INVALID_LOGIN, headers=NO_STORE) from None
    auth.start(request, response, profile)
    response.headers.update(NO_STORE)
    return profile


@router.get("/me")
def me(profile: CurrentProfile):
    return profile


@router.post("/logout", status_code=204, dependencies=[Depends(check_origin)])
def logout(request: Request, response: Response):
    request.app.state.auth.end(request, response)
    response.headers.update(NO_STORE)
