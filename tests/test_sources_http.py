from __future__ import annotations

import httpx
import pytest

from watcher.sources import http
from watcher.sources.http import USER_AGENT, FetchError, get_json, request
from tests.conftest import Router

URL = "https://example.com/data"


def _sequence(*responses: httpx.Response | Exception):
    queue = list(responses)

    def handler(req: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handler


def test_user_agent_and_success() -> None:
    router = Router({URL: httpx.Response(200, json={"ok": True})})
    assert get_json(router.client(), URL) == {"ok": True}
    assert router.requests[0].headers["User-Agent"] == USER_AGENT


def test_retries_on_5xx_and_network_errors_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "RETRY_BACKOFF_S", 2.0)
    router = Router({URL: _sequence(httpx.Response(503), httpx.ConnectError("boom"), httpx.Response(200, text="x"))})
    sleeps: list[float] = []
    assert request(router.client(), "GET", URL, sleep=sleeps.append).text == "x"
    assert len(router.requests) == 3
    assert sleeps == [2.0, 4.0]      # attente croissante entre tentatives


def test_gives_up_after_two_retries() -> None:
    router = Router({URL: httpx.Response(429)})
    with pytest.raises(FetchError, match="HTTP 429 après 3 tentative") as exc:
        request(router.client(), "GET", URL, sleep=lambda s: None)
    assert len(router.requests) == 3
    assert exc.value.status_code == 429


def test_client_error_is_not_retried() -> None:
    router = Router({URL: httpx.Response(403)})
    with pytest.raises(FetchError, match="HTTP 403") as exc:
        request(router.client(), "GET", URL, sleep=lambda s: None)
    assert exc.value.status_code == 403
    assert len(router.requests) == 1


def test_unreadable_json() -> None:
    router = Router({URL: httpx.Response(200, text="<html>")})
    with pytest.raises(FetchError, match="JSON illisible"):
        get_json(router.client(), URL)
