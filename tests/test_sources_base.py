from __future__ import annotations

from typing import Any

import pytest

from watcher.config import Source
from watcher.sources import base


class DummyFetcher:
    source_type = "rss"

    def validate_params(self, params: dict[str, Any]) -> None:
        if "url" not in params:
            raise ValueError("paramètre 'url' requis")

    def fetch(self, source, agent, since):
        return []

    def fetch_text(self, item):
        return ""


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_REGISTRY", {})


def test_register_and_validate() -> None:
    base.register(DummyFetcher())
    assert base.get_fetcher("rss") is not None
    base.validate_source_params(Source(name="IR", type="rss", primary=True, params={"url": "https://x"}))
    with pytest.raises(ValueError, match="'url' requis"):
        base.validate_source_params(Source(name="IR", type="rss", primary=True))


def test_duplicate_registration_refused() -> None:
    base.register(DummyFetcher())
    with pytest.raises(ValueError, match="déjà enregistré"):
        base.register(DummyFetcher())


def test_unregistered_type_is_not_a_config_error() -> None:
    base.validate_source_params(Source(name="EDGAR", type="edgar", primary=True))
