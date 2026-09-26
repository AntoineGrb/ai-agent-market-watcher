from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from watcher.config import Source
from watcher.sources.base import SourceState
from watcher.sources.rss import RssFetcher
from tests.conftest import NOW, OFFLINE, Router, repo_agent

FEED = "https://ir.example.com/feed/"
SOURCE = Source(name="IR", type="rss", primary=True, params={"url": FEED})
SINCE = NOW - timedelta(days=3)

# Flux synthétique : aucune source rss n'est active dans les configs (docs/sources.md), rien n'a été enregistré.
XML = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>IR</title>
<item><title>Refinancing announced</title><link>https://ir.example.com/pr/1</link><guid>pr-1</guid>
<pubDate>Thu, 24 Sep 2026 18:00:00 GMT</pubDate><description>&lt;p&gt;The company &lt;b&gt;announces&lt;/b&gt;
a refinancing.&lt;/p&gt;</description></item>
<item><title>Old news</title><link>https://ir.example.com/pr/0</link><pubDate>Mon, 01 Jun 2026 08:00:00 GMT</pubDate></item>
<item><title>Undated</title><link>https://ir.example.com/pr/2</link></item>
<item><link>https://ir.example.com/pr/3</link></item>
</channel></rss>"""


def test_fetch_feed() -> None:
    router = Router({FEED: httpx.Response(200, text=XML)})
    items = RssFetcher(router.client()).fetch(SOURCE, repo_agent("ubi"), SINCE, SourceState())
    assert [it.title for it in items] == ["Refinancing announced", "Undated"]
    first = items[0]
    assert first.published_at == datetime(2026, 9, 24, 18, tzinfo=UTC)
    assert first.summary == "The company announces a refinancing."    # balise en ligne : pas de coupure
    assert first.source_primary and items[1].published_at is None


def test_unreadable_feed_raises() -> None:
    router = Router({FEED: httpx.Response(200, text="<<< pas un flux")})
    with pytest.raises(Exception, match="illisible"):
        RssFetcher(router.client()).fetch(SOURCE, repo_agent("ubi"), SINCE, SourceState())


def test_fetch_text() -> None:
    body = "La société annonce le refinancement de son échéance obligataire de novembre 2027. " * 3
    router = Router({
        FEED: httpx.Response(200, text=XML),
        "https://ir.example.com/pr/1": httpx.Response(200, text=f"<html><body><p>{body}</p></body></html>"),
        "https://ir.example.com/pr/2": httpx.Response(200, text="<html><body><p>court</p></body></html>"),
    })
    fetcher = RssFetcher(router.client())
    first, undated = fetcher.fetch(SOURCE, repo_agent("ubi"), SINCE, SourceState())
    assert "novembre 2027" in fetcher.fetch_text(first)
    assert fetcher.fetch_text(undated).startswith("Undated")      # page sans paragraphe exploitable : repli


@pytest.mark.parametrize("params", [{}, {"url": "ftp://x"}, {"url": FEED, "query": "x"}])
def test_validate_params(params) -> None:
    with pytest.raises(ValueError):
        RssFetcher(OFFLINE).validate_params(params)
