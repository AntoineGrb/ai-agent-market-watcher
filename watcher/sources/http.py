"""Client HTTP commun aux fetchers et aux providers de cours (cadrage §8.1).

Règles communes : timeouts explicites, 2 retries maximum (erreurs réseau, 429 et 5xx), `User-Agent` explicite.
Toute autre erreur (4xx, réponse illisible) lève `FetchError` : le fetcher la laisse remonter, et l'appelant
logge la source en erreur sans faire échouer l'agent.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "ai-agent-market-watcher/0.1 (veille boursiere personnelle)"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_RETRIES = 2
RETRY_BACKOFF_S = 2.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class FetchError(Exception):
    """Échec de récupération d'une source (réseau, statut HTTP, format inattendu).

    `status_code` : dernier statut HTTP reçu, s'il y en a un (permet de reconnaître une limitation de débit, 429).
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Client partagé par tous les fetchers d'un run. `transport` : injection de réponses dans les tests."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
        follow_redirects=True,
        transport=transport,
    )


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    data: Mapping[str, Any] | None = None,
    retries: int = MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """Requête avec au plus `retries` nouvelles tentatives. Lève `FetchError` si elle n'aboutit pas."""
    last_error = ""
    last_status: int | None = None
    for attempt in range(retries + 1):
        if attempt:
            sleep(RETRY_BACKOFF_S * attempt)
        try:
            response = client.request(method, url, params=params, headers=headers, data=data)
        except httpx.HTTPError as exc:
            last_error, last_status = f"{type(exc).__name__} : {exc}", None
            log.warning("%s %s : tentative %d en échec (%s)", method, url, attempt + 1, last_error)
            continue
        if response.status_code in RETRYABLE_STATUS:
            last_error, last_status = f"HTTP {response.status_code}", response.status_code
            log.warning("%s %s : tentative %d en échec (%s)", method, url, attempt + 1, last_error)
            continue
        if response.is_error:
            raise FetchError(f"{method} {url} : HTTP {response.status_code}", response.status_code)
        return response
    raise FetchError(f"{method} {url} : {last_error} après {retries + 1} tentative(s)", last_status)


def get_json(client: httpx.Client, url: str, **kwargs: Any) -> Any:
    response = request(client, "GET", url, **kwargs)
    try:
        return response.json()
    except ValueError as exc:
        raise FetchError(f"GET {url} : réponse JSON illisible ({exc})") from exc
