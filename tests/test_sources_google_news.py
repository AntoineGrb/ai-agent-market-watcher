from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from watcher.config import Source
from watcher.sources.base import SourceState
from watcher.sources.google_news import (
    ARTICLE_URL,
    BATCH_URL,
    FEED_URL,
    GoogleNewsFetcher,
    parse_batchexecute,
)
from tests.conftest import NOW, OFFLINE, Router, data_file, repo_agent

SOURCE = Source(name="Google News EN", type="google_news_rss", primary=False, params={
    "query": 'Ubisoft (shares OR refinancing OR Tencent)', "hl": "en-US", "gl": "US", "ceid": "US:en",
})
SINCE = NOW - timedelta(days=3)
ARTICLE = "CBMabc123"
LINK = f"https://news.google.com/rss/articles/{ARTICLE}?oc=5"
PUBLISHER_URL = "https://www.example-news.com/ubisoft-tencent"


def _fetcher(router: Router) -> GoogleNewsFetcher:
    return GoogleNewsFetcher(router.client(), clock=lambda: NOW)


def test_fetch_recorded_feed() -> None:
    router = Router({FEED_URL: httpx.Response(200, content=data_file("google_news_ubi_en.xml"))})
    items = _fetcher(router).fetch(SOURCE, repo_agent("ubi"), SINCE, SourceState())

    query = router.requests[0].url.params
    assert query["q"] == 'Ubisoft (shares OR refinancing OR Tencent) when:3d'   # when: toujours ajouté
    assert (query["hl"], query["gl"], query["ceid"]) == ("en-US", "US", "US:en")
    assert len(items) == 7                                                       # 13 entrées, 7 depuis SINCE
    assert all(it.published_at >= SINCE for it in items)
    first = items[0]
    assert first.title == "Ubisoft Stock Performance Shows Strong Momentum in September - The Cryptonomist"
    assert first.summary == "Éditeur : The Cryptonomist"
    assert (first.source_primary, first.source_type, first.agent_id) == (False, "google_news_rss", "UBI")
    assert str(first.url).startswith("https://news.google.com/rss/articles/")
    assert first.published_at == datetime(2026, 9, 22, 13, 19, 29, tzinfo=UTC)
    assert len({it.id for it in items}) == 7


def test_ids_are_stable() -> None:
    router = Router({FEED_URL: httpx.Response(200, content=data_file("google_news_ubi_en.xml"))})
    fetcher, agent = _fetcher(router), repo_agent("ubi")
    first = [it.id for it in fetcher.fetch(SOURCE, agent, SINCE, SourceState())]
    assert first == [it.id for it in fetcher.fetch(SOURCE, agent, SINCE, SourceState())]


def test_unreadable_feed_raises() -> None:
    router = Router({FEED_URL: httpx.Response(200, text="pas du xml <<<")})
    with pytest.raises(Exception, match="illisible"):
        _fetcher(router).fetch(SOURCE, repo_agent("ubi"), SINCE, SourceState())


@pytest.mark.parametrize("params, message", [
    ({"query": "x", "hl": "fr", "gl": "FR"}, "'ceid' requis"),
    ({"query": "x when:7d", "hl": "fr", "gl": "FR", "ceid": "FR:fr"}, "when:"),
    ({"query": "x", "hl": "fr", "gl": "FR", "ceid": "FR:fr", "url": "y"}, "inconnus"),
])
def test_validate_params(params, message) -> None:
    with pytest.raises(ValueError, match=message):
        GoogleNewsFetcher(OFFLINE).validate_params(params)


def test_repo_configs_are_valid() -> None:
    fetcher = GoogleNewsFetcher(OFFLINE)
    for agent in ("ubi", "nano"):
        for source in repo_agent(agent).sources:
            if source.type == "google_news_rss":
                fetcher.validate_params(source.params)


# --------------------------------------------------------------------------- texte (meilleur effort)


def _batch_body(url: str) -> str:
    inner = json.dumps(["garturlres", url, 1])
    return ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner, None, None, None, "generic"], ["di", 42]]) + "\n"


def _item():
    router = Router({FEED_URL: httpx.Response(200, content=data_file("google_news_ubi_en.xml"))})
    item = _fetcher(router).fetch(SOURCE, repo_agent("ubi"), SINCE, SourceState())[0]
    return item.model_copy(update={"url": LINK})


def test_parse_batchexecute() -> None:
    assert parse_batchexecute(_batch_body(PUBLISHER_URL)) == PUBLISHER_URL
    with pytest.raises(ValueError, match="URL absente"):
        parse_batchexecute(")]}'\n\n[[\"di\", 1]]")


def test_fetch_text_decodes_publisher_url() -> None:
    paragraph = "Ubisoft confirme l'opération avec Tencent et précise son calendrier de refinancement. " * 5
    router = Router({
        ARTICLE_URL.format(article_id=ARTICLE): httpx.Response(
            200, text='<div data-n-a-sg="SIG" data-n-a-ts="1727000000"></div>'),
        BATCH_URL: httpx.Response(200, text=_batch_body(PUBLISHER_URL)),
        PUBLISHER_URL: httpx.Response(200, text=f"<html><nav><p>{'menu ' * 20}</p></nav><article><p>{paragraph}</p>"
                                                f"</article></html>"),
    })
    text = _fetcher(router).fetch_text(_item())
    assert PUBLISHER_URL in text and "calendrier de refinancement" in text and "menu" not in text
    assert router.requests[0].headers["cookie"] == "SOCS=CAI"
    form = dict(httpx.QueryParams(router.requests[1].content.decode()))
    assert '"Fbv4je"' in form["f.req"] and "SIG" in form["f.req"] and "1727000000" in form["f.req"]


def test_fetch_text_falls_back_to_title() -> None:
    router = Router({ARTICLE_URL.format(article_id=ARTICLE): httpx.Response(200, text="<html>consentement</html>")})
    item = _item()
    text = _fetcher(router).fetch_text(item)
    assert item.title in text and "Éditeur : The Cryptonomist" in text and "indisponible" in text


def test_fetch_text_falls_back_when_article_is_too_short() -> None:
    router = Router({
        ARTICLE_URL.format(article_id=ARTICLE): httpx.Response(200, text='data-n-a-sg="S" data-n-a-ts="1"'),
        BATCH_URL: httpx.Response(200, text=_batch_body(PUBLISHER_URL)),
        PUBLISHER_URL: httpx.Response(200, text="<p>Abonnez-vous pour lire la suite de cet article.</p>"),
    })
    assert "indisponible" in _fetcher(router).fetch_text(_item())


# --------------------------------------------------------------------------- débit (disjoncteur, plafond, pause)


def _decoding_router(article_status: int = 200) -> Router:
    paragraph = "Ubisoft confirme l'opération avec Tencent et précise son calendrier de refinancement. " * 5
    return Router({
        FEED_URL: httpx.Response(200, content=data_file("google_news_ubi_en.xml")),
        ARTICLE_URL.format(article_id=ARTICLE): httpx.Response(
            article_status, text='<div data-n-a-sg="SIG" data-n-a-ts="1727000000"></div>'),
        BATCH_URL: httpx.Response(200, text=_batch_body(PUBLISHER_URL)),
        PUBLISHER_URL: httpx.Response(200, text=f"<article><p>{paragraph}</p></article>"),
    })


def _google_calls(router: Router) -> int:
    return sum(1 for r in router.requests if r.url.host == "news.google.com" and r.url.path != "/rss/search")


def test_rate_limit_trips_the_breaker_without_retry() -> None:
    router = _decoding_router(article_status=429)
    fetcher = GoogleNewsFetcher(router.client(), clock=lambda: NOW, sleep=lambda s: None)
    item = _item()

    assert "indisponible" in fetcher.fetch_text(item)
    assert _google_calls(router) == 1                     # aucun retry sur un 429
    assert "indisponible" in fetcher.fetch_text(item)
    assert _google_calls(router) == 1                     # disjoncteur : plus aucun appel de décodage ce run
    # Les flux RSS restent servis (agents suivants).
    assert fetcher.fetch(SOURCE, repo_agent("ubi"), SINCE, SourceState())


def test_other_failures_do_not_trip_the_breaker() -> None:
    router = _decoding_router()
    router.routes[PUBLISHER_URL] = httpx.Response(403)    # anti-bot de l'éditeur : repli, mais pas de coupure
    fetcher = GoogleNewsFetcher(router.client(), clock=lambda: NOW, sleep=lambda s: None)
    assert "indisponible" in fetcher.fetch_text(_item())
    router.routes[PUBLISHER_URL] = httpx.Response(200, text="<p>" + "Texte complet de l'article. " * 20 + "</p>")
    assert "Texte complet" in fetcher.fetch_text(_item())


def test_decodes_are_paced_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    from watcher.sources import google_news

    monkeypatch.setattr(google_news, "MAX_DECODES_PER_RUN", 2)
    router = _decoding_router()
    sleeps: list[float] = []
    fetcher = GoogleNewsFetcher(router.client(), clock=lambda: NOW, sleep=sleeps.append)

    assert "calendrier" in fetcher.fetch_text(_item())
    assert "calendrier" in fetcher.fetch_text(_item())
    assert "indisponible" in fetcher.fetch_text(_item())  # plafond atteint : repli sans appel
    assert _google_calls(router) == 4                     # 2 décodages × (page + batchexecute)
    assert sleeps == [google_news.DECODE_PAUSE_S]          # pause avant chaque décodage sauf le premier
