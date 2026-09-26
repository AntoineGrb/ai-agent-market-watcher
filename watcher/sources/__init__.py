"""Fetchers de sources. `register_builtin_fetchers` enregistre ceux du dépôt dans le registre de `base`."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from watcher.settings import Settings
from watcher.sources.base import get_fetcher, register
from watcher.sources.clinicaltrials import ClinicalTrialsFetcher
from watcher.sources.dila_amf import DilaAmfFetcher
from watcher.sources.edgar import EdgarFetcher
from watcher.sources.google_news import GoogleNewsFetcher
from watcher.sources.rss import RssFetcher

# `html_list` (scraping) n'est pas implémenté : aucune source n'en a besoin (docs/sources.md, synthèse).


def register_builtin_fetchers(
    settings: Settings,
    client: httpx.Client,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Idempotent : un type déjà enregistré (par un test, par exemple) est conservé."""
    for fetcher in (
        GoogleNewsFetcher(client, clock),
        RssFetcher(client),
        ClinicalTrialsFetcher(client, clock),
        EdgarFetcher(client, settings.sec_user_agent),
        DilaAmfFetcher(client, clock),
    ):
        if get_fetcher(fetcher.source_type) is None:
            register(fetcher)
