from __future__ import annotations

import logging
from pathlib import Path

import httpx

from watcher.monitoring import PING_BODY_MAX_CHARS, Healthchecks, setup_logging


def _client(statuses: list[int], requests: list[httpx.Request]) -> httpx.Client:
    codes = iter(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(next(codes))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pings_with_run_id() -> None:
    requests: list[httpx.Request] = []
    hc = Healthchecks("https://hc-ping.com/abc/", client=_client([200, 200, 200], requests))
    hc.start()
    hc.success("ok")
    hc.fail("x" * (PING_BODY_MAX_CHARS + 50))
    paths = [r.url.path for r in requests]
    assert paths == ["/abc/start", "/abc", "/abc/fail"]
    assert {r.url.params["rid"] for r in requests} == {hc.run_id}
    assert requests[1].content == b"ok"
    assert len(requests[2].content) == PING_BODY_MAX_CHARS


def test_retries_then_gives_up_without_raising(caplog) -> None:
    requests: list[httpx.Request] = []
    hc = Healthchecks("https://hc-ping.com/abc", retries=2, client=_client([500, 503, 502], requests))
    hc.success()
    assert len(requests) == 3
    assert "abandonné après 3 tentatives" in caplog.text


def test_retry_succeeds() -> None:
    requests: list[httpx.Request] = []
    Healthchecks("https://hc-ping.com/abc", client=_client([500, 200], requests)).start()
    assert len(requests) == 2


def test_network_error_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    hc = Healthchecks("https://hc-ping.com/abc", retries=0,
                      client=httpx.Client(transport=httpx.MockTransport(handler)))
    hc.fail("boom")


def test_disabled_does_nothing() -> None:
    requests: list[httpx.Request] = []
    for hc in (Healthchecks(None, client=_client([], requests)),
               Healthchecks("https://hc-ping.com/abc", enabled=False, client=_client([], requests))):
        hc.start()
        hc.success()
        assert not hc.enabled
    assert requests == []


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    root = logging.getLogger()
    before = len(root.handlers)
    setup_logging(tmp_path / "logs")
    setup_logging(tmp_path / "logs")
    try:
        assert len(root.handlers) == before + 2
        logging.getLogger("watcher.test").info("ligne accentuée é")
        for h in root.handlers:
            h.flush()
        assert "ligne accentuée é" in (tmp_path / "logs" / "watcher.log").read_text(encoding="utf-8")
    finally:
        for h in [h for h in root.handlers if getattr(h, "_watcher_handler", False)]:
            root.removeHandler(h)
            h.close()
