import json

import httpx
import pytest

from backend.app import ai
from backend.app.ai.gigachat import GigaChatClient, GigaChatError, GigaChatModelUnavailable
from backend.app.ai.llm import LLMWorker

from .conftest import COORD, make
from .test_llm import run, wait_until


class ModelClient:
    model = "GigaChat-Lite-test"

    def __init__(self, unavailable=False):
        self.calls = []
        self.unavailable = unavailable

    def chat(self, messages, *, model=None):
        prompt = messages[-1]["content"]
        self.calls.append((model or self.model, prompt))
        if model and self.unavailable:
            raise GigaChatModelUnavailable("model unavailable")
        return json.dumps({"summary": "Выжимка", "route_team": "КЦ Ступа", "route_reason": "Пояснение маршрута",
                           "risk_comment": "Пояснение риска", "same": True, "name": "Общий сбой", "exclude": []})


def model_records():
    return [
        make("HOT", 2, 3, COORD, impactId=4, urgencyId=4, serviceType="Клиентский"),
        make("HIST", 7, team="КЦ Ступа"),
        *[make(f"X{i}", productMasterId="BI_SHARED", productName=f"Продукт {i}", service=f"Сервис {i}",
               scenarioName=f"Сценарий сбоя {i}")
          for i in range(3)],
    ]


@pytest.mark.parametrize("unavailable", [False, True])
def test_model_assignment_and_fallback_in_one_queue(tmp_path, rules, unavailable):
    client = ModelClient(unavailable)
    advanced = "GigaChat-Pro-test"
    cache = tmp_path / "llm.json"
    worker = LLMWorker(client, cache, advanced_model=advanced)
    ai.reset(worker=worker)
    records = model_records()
    run(rules, records)
    assert wait_until(lambda: worker.snapshot_status()["done"] == 3)
    enriched = run(rules, records)["HOT"]["ai"]
    assert enriched["summary"] == "Выжимка" and enriched["route"]["reason"] == "Пояснение маршрута"
    assert enriched["risk"]["comment"] == "Пояснение риска" and not enriched["pending"]
    assert ai.outage_candidates()[0]["source"] == "gigachat"
    assert ai.outage_candidates()[0]["name"] == "Общий сбой"
    assert [model for model, prompt in client.calls if '"summary":' in prompt] == [client.model]

    successful = client.model if unavailable else advanced
    route_calls = [(model, prompt) for model, prompt in client.calls if '"route_team":' in prompt]
    assert route_calls[-1][0] == successful
    assert '"risk_comment":' in route_calls[-1][1] and '"summary":' not in route_calls[-1][1]
    assert [model for model, prompt in client.calls if "Кандидат на сбой между продуктами" in prompt] == [successful]
    assert len(worker.calls) == (4 if unavailable else 3)  # Повтор через базовую модель тоже считается в RPM.
    if unavailable:
        assert [model for model, _ in client.calls].count(advanced) == 1
        worker.save(force=True)
        assert len(json.loads(cache.read_text(encoding="utf-8"))) == 1  # Сохранилась только базовая выжимка.
    else:
        worker.save(force=True)
        next_client = ModelClient()
        next_model = "GigaChat-Other-test"
        next_worker = LLMWorker(next_client, cache, advanced_model=next_model)
        ai.reset(worker=next_worker)
        assert run(rules, records)["HOT"]["ai"]["summary_meta"]["source"] == "cache"
        assert wait_until(lambda: next_worker.snapshot_status()["done"] == 2)
        assert [model for model, _ in next_client.calls] == [next_model, next_model]


def test_model_catalog_uses_oauth_and_prints_no_secrets(monkeypatch, capsys):
    from backend.app.ai import gigachat

    credentials = "fake-authorization-secret"
    token = "fake-bearer-secret"

    def auth(url, **kwargs):
        assert url == gigachat.OAUTH_URL and kwargs["headers"]["Authorization"] == f"Basic {credentials}"
        assert kwargs["verify"] is True
        return httpx.Response(200, json={"access_token": token})

    def models(url, **kwargs):
        assert url == f"{gigachat.API_URL}/models"
        assert kwargs["headers"]["Authorization"] == f"Bearer {token}" and kwargs["verify"] is True
        return httpx.Response(200, json={"data": [{"id": "GigaChat"}, {"id": "GigaChat-Pro"}]})

    monkeypatch.setattr(gigachat.httpx, "post", auth)
    monkeypatch.setattr(gigachat.httpx, "get", models)
    client = GigaChatClient(credentials)
    assert client.list_models() == ["GigaChat", "GigaChat-Pro"]
    monkeypatch.setattr(gigachat.httpx, "get", lambda *args, **kw: httpx.Response(401, text=token))
    with pytest.raises(GigaChatError, match="HTTP 401") as error:
        client.list_models()
    assert token not in str(error.value) and credentials not in str(error.value)
    assert capsys.readouterr() == ("", "")


def test_advanced_chat_model_override_and_provider_error(monkeypatch):
    from backend.app.ai import gigachat

    client = GigaChatClient("fake-authorization-secret", model="GigaChat")
    monkeypatch.setattr(client, "_auth", lambda: "fake-token")

    def post(url, **kwargs):
        assert url == f"{gigachat.API_URL}/chat/completions"
        if kwargs["json"]["model"] == "GigaChat-Advanced":
            return httpx.Response(404, text="model not found")
        assert kwargs["json"]["model"] == "GigaChat"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(gigachat.httpx, "post", post)
    with pytest.raises(GigaChatModelUnavailable, match="HTTP 404"):
        client.chat([], model="GigaChat-Advanced")
    assert client.chat([]) == "ok"
