from dataclasses import replace

from fastapi.testclient import TestClient

from backend.app import auth
from backend.app.itsm import Itsm

from .conftest import ORIGIN, TEST_PASSWORD, make


def test_demo_login_status_journal_wall_and_logout(make_store, monkeypatch, configure_auth):
    from backend.app import main

    store, path = make_store([make("DEMO", priority_id=4)])
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "itsm", Itsm(path, store.rules, store.clock.now, store=store))
    configure_auth(main.app, ["Демо-координатор"])
    wall = TestClient(main.app, follow_redirects=False)
    client = TestClient(main.app, headers={"Origin": ORIGIN}, follow_redirects=False)
    route = "/api/itsm/DEMO/state"
    detail = "/api/incidents/DEMO"
    login = {"login": "демо-координатор", "password": TEST_PASSWORD}
    body = {"state_id": 2, "by": "Другой координатор"}

    assert wall.get("/wall").status_code == 200
    initial = wall.get("/api/snapshot")
    assert wall.get("/api/snapshot", headers={"If-None-Match": initial.headers["etag"]}).status_code == 304
    direct = wall.get("/itsm/DEMO?return=//external.example")
    assert direct.status_code == 307 and direct.headers["location"] == "/?return=%2Fitsm%2FDEMO"
    assert 'class="session-content" hidden inert' in wall.get("/itsm.html").text
    unchanged = path.read_bytes()
    assert client.post(route, json=body).status_code == 401
    assert client.get(detail).status_code == client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json=login, headers={"Origin": ORIGIN + ":9999"}).status_code == 403
    wrong_password = client.post("/api/auth/login", json={**login, "password": "wrong"})
    wrong_user = client.post("/api/auth/login", json={**login, "login": "unknown"})
    assert wrong_password.status_code == wrong_user.status_code == 401
    assert wrong_password.json() == wrong_user.json() == {"detail": "Неверный логин или пароль"}

    logged_in = client.post("/api/auth/login", json=login)
    assert logged_in.status_code == 200
    assert logged_in.json() == {"id": "0", "login": login["login"], "display_name": "Демо-координатор"}
    attrs = logged_in.headers["set-cookie"].lower()
    assert all(attr in attrs for attr in ("httponly", "path=/", "samesite=lax", "max-age=7200"))
    me = client.get("/api/auth/me")
    assert me.json() == logged_in.json() and me.headers["Cache-Control"] == "no-store"
    assert client.get("/").status_code == client.get("/itsm/DEMO").status_code == 200
    body["expected_hash"] = client.get(detail).json()["hash"]
    assert client.post(route, json=body, headers={"Origin": ORIGIN + ":9999"}).status_code == 403
    token = client.cookies[auth.COOKIE]
    replay = TestClient(main.app, cookies={auth.COOKIE: token})
    assert replay.post(route, json=body).status_code == 403
    assert path.read_bytes() == unchanged
    updated = client.post(route, params={"by": "Чужое имя"}, json=body)
    assert updated.status_code == 200 and updated.json()["state_id"] == 2
    assert updated.json()["work_log"][-1]["author"] == "Демо-координатор"
    assert client.get(detail).json()["history"][-1]["by"] == "Демо-координатор"
    assert client.post(route, json=body).status_code == 409
    snapshot = wall.get("/api/snapshot", headers={"If-None-Match": initial.headers["etag"]})
    assert snapshot.status_code == 200 and snapshot.json()["incidents"]["DEMO"]["state_id"] == 2

    assert client.post("/api/auth/logout", headers={"Origin": "null"}).status_code == 403
    logout = client.post("/api/auth/logout")
    assert logout.status_code == 204 and "Max-Age=0" in logout.headers["set-cookie"]
    assert client.post("/api/auth/logout").status_code == 204
    assert replay.get("/api/auth/me").status_code == 401
    assert wall.get("/api/snapshot", headers={"If-None-Match": snapshot.headers["etag"]}).status_code == 304


def test_every_working_route_requires_a_session():
    from backend.app import main

    client = TestClient(main.app, headers={"Origin": ORIGIN})
    reads = ("workstation/snapshot", "search", "events", "queue", "incidents/I", "meta", "export",
             "outages/O", "outage-candidates/C")
    writes = ("incidents/I/claim", "itsm/I/assign", "itsm/I/state", "itsm/I/priority", "itsm/I/deadline",
              "itsm/I/comment", "itsm/I/escalate", "duplicates/confirm", "outages/confirm",
              "outages/O/actions", "incidents/I/ai-feedback", "queue/preferences", "import")
    deletes = ("incidents/I/claim", "outages/O/incidents/I", "outage-candidates/C/incidents/I")
    for method, paths in (("GET", reads), ("POST", writes), ("DELETE", deletes)):
        for path in paths:
            response = client.request(method, f"/api/{path}", json={} if method != "GET" else None)
            assert response.status_code == 401, (method, path, response.text)


def test_https_cookie_rotation_expiry_and_invalid_config(monkeypatch, configure_auth):
    from backend.app import main

    origin = "https://testserver:8443"
    settings = configure_auth(main.app, ["Alice"], ttl=5, origin=origin)
    clock = [100.0]
    monkeypatch.setattr(auth, "time", type("Clock", (), {"monotonic": staticmethod(lambda: clock[0])}))
    client = TestClient(main.app, base_url=origin, headers={"Origin": origin})
    login = {"login": "Alice", "password": TEST_PASSWORD}
    assert "Secure" in client.post("/api/auth/login", json=login).headers["set-cookie"]
    old = client.cookies[auth.COOKIE]
    assert client.post("/api/auth/login", json=login).status_code == 200
    assert client.cookies[auth.COOKIE] != old
    assert client.get("/api/auth/me", headers={"Cookie": f"{auth.COOKIE}={old}"}).status_code == 401
    clock[0] += 5
    assert client.get("/api/auth/me").status_code == 401
    main.app.state.auth = auth.DemoAuth(replace(settings, demo_accounts="[]"))
    assert client.post("/api/auth/login", json=login).status_code == 503
