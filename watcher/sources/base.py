"""Interface des fetchers et registre par type de source (cadrage §8.1).

Ajouter un type de source = ajouter un module qui appelle `register(...)`, sans toucher au reste.

Écart assumé avec la signature du cadrage : `fetch` reçoit aussi un `SourceState`. Certaines sources détectent
des changements d'un run à l'autre (instantané ClinicalTrials, §8.2) ; leur état vit dans `source_state` et doit
être écrit dans la transaction de l'agent, donc par l'appelant et non par le fetcher. Les autres l'ignorent.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from watcher.config import AgentConfig, Source
from watcher.models import NewsItem

log = logging.getLogger(__name__)


@dataclass
class SourceState:
    """État persistant d'une source : `previous` vient de `source_state`, `updated` y sera écrit si renseigné."""

    previous: dict[str, Any] | None = None
    updated: dict[str, Any] | None = None


class Fetcher(Protocol):
    source_type: str

    def validate_params(self, params: dict[str, Any]) -> None:
        """Lève ValueError si les paramètres de la source sont invalides (appelé au chargement de la config)."""

    def fetch(self, source: Source, agent: AgentConfig, since: datetime, state: SourceState) -> list[NewsItem]:
        """Retourne les documents publiés depuis `since` (sans le texte complet). Lève une exception en cas d'échec."""

    def fetch_text(self, item: NewsItem) -> str:
        """Récupère le texte complet d'un document retenu au tri."""


def item_id(*parts: str) -> str:
    """ID stable d'un document : sha256 de sa clé naturelle (guid RSS, accession EDGAR, identifiant AMF...)."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"paramètre {key!r} requis (chaîne non vide)")
    return value.strip()


def reject_unknown(params: dict[str, Any], allowed: set[str]) -> None:
    if unknown := set(params) - allowed:
        raise ValueError(f"paramètres inconnus : {sorted(unknown)} (attendus : {sorted(allowed)})")


_REGISTRY: dict[str, Fetcher] = {}


def register(fetcher: Fetcher) -> Fetcher:
    if fetcher.source_type in _REGISTRY:
        raise ValueError(f"fetcher déjà enregistré pour le type {fetcher.source_type!r}")
    _REGISTRY[fetcher.source_type] = fetcher
    return fetcher


def get_fetcher(source_type: str) -> Fetcher | None:
    return _REGISTRY.get(source_type)


def validate_source_params(source: Source) -> None:
    """Valide les paramètres d'une source active auprès de son fetcher.

    Un type sans fetcher enregistré n'est pas une erreur de config : la source sera sautée au fetch.
    """
    fetcher = get_fetcher(source.type)
    if fetcher is None:
        log.debug("aucun fetcher enregistré pour %s (%s)", source.name, source.type)
        return
    fetcher.validate_params(source.params)
