"""Flux RSS / Atom générique (`rss`, primaire selon la config), pour les pages investisseurs qui en proposent.

Aucune source MVP ne l'utilise (docs/sources.md : flux absents ou redondants), mais il permet d'ajouter une
société disposant d'un flux sans toucher au code.
"""

from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser
import httpx

from watcher.config import AgentConfig, Source
from watcher.models import NewsItem
from watcher.sources.base import SourceState, item_id, reject_unknown, require_str
from watcher.sources.http import FetchError, request
from watcher.sources.text import article_text, html_to_text, truncate

log = logging.getLogger(__name__)

SUMMARY_CHARS = 500


class RssFetcher:
    source_type = "rss"

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def validate_params(self, params: dict[str, Any]) -> None:
        reject_unknown(params, {"url"})
        url = require_str(params, "url")
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError(f"url invalide : {url!r}")

    def fetch(self, source: Source, agent: AgentConfig, since: datetime, state: SourceState) -> list[NewsItem]:
        feed = feedparser.parse(request(self._client, "GET", source.params["url"]).content)
        if feed.bozo and not feed.entries:
            raise FetchError(f"flux RSS illisible : {feed.get('bozo_exception')}")
        items: list[NewsItem] = []
        for entry in feed.entries:
            link, title = entry.get("link"), (entry.get("title") or "").strip()
            if not (link and title):
                continue
            parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            published = datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC) if parsed else None
            if published is not None and published < since:
                continue
            items.append(NewsItem(
                id=item_id(self.source_type, entry.get("id") or link),
                agent_id=agent.agent_id,
                source_name=source.name,
                source_type=self.source_type,
                source_primary=source.primary,
                url=link,
                title=title,
                published_at=published,
                summary=truncate(html_to_text(entry.get("summary") or ""), SUMMARY_CHARS),
            ))
        return items

    def fetch_text(self, item: NewsItem) -> str:
        text = article_text(request(self._client, "GET", str(item.url)).content)
        return text or f"{item.title}\n{item.summary}"
