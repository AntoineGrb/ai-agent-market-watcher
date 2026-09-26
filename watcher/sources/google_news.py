"""Flux de recherche Google News (`google_news_rss`, source non primaire). Voir docs/sources.md §4.

- Le flux renvoie au plus 100 résultats triés par pertinence : ` when:Nd` est toujours ajouté à la requête,
  sinon les articles récents sont perdus.
- `summary` du flux ne contient qu'un lien reprenant le titre : le tri travaille sur le titre et l'éditeur.
- `fetch_text` est en « meilleur effort » : le lien du flux mène à une page de consentement, l'URL de l'éditeur
  s'obtient par une API interne non documentée. En cas d'échec, repli sur le titre et l'éditeur. La source n'étant
  jamais primaire, cela n'affecte jamais une recommandation actionnable.
- Débit : Google répond HTTP 429 après quelques dizaines d'appels rapprochés, et les agents tournent l'un après
  l'autre. Pour que les décodages d'un agent ne fassent jamais bloquer les flux RSS des agents suivants, les
  décodages sont espacés (`DECODE_PAUSE_S`), plafonnés par run (`MAX_DECODES_PER_RUN`), faits sans retry, et
  coupés pour le reste du run au premier 429 (disjoncteur). Les flux RSS, eux, ne sont jamais coupés.
  Le fetcher est instancié une fois par processus, donc une fois par run : ses compteurs valent pour le run.
"""

from __future__ import annotations

import calendar
import json
import logging
import math
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser
import httpx

from watcher.config import AgentConfig, Source
from watcher.models import NewsItem
from watcher.sources.base import SourceState, item_id, reject_unknown, require_str
from watcher.sources.http import FetchError, request
from watcher.sources.text import article_text

log = logging.getLogger(__name__)

FEED_URL = "https://news.google.com/rss/search"
ARTICLE_URL = "https://news.google.com/articles/{article_id}"
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
# Cookie de consentement (UE). Il doit vivre dans le jar du client, domaine .google.com : Google répond d'abord
# par un 302 qui pose d'autres cookies, et un simple en-tête Cookie ne survit pas à cette redirection.
CONSENT_COOKIE = ("SOCS", "CAI", ".google.com")
PARAMS = {"query", "hl", "gl", "ceid"}
MIN_ARTICLE_CHARS = 300             # en dessous : page vide, paywall ou anti-bot → repli
MAX_DECODES_PER_RUN = 20            # tous agents confondus ; au-delà : repli titre + éditeur
DECODE_PAUSE_S = 1.0                # entre deux décodages (2 appels Google chacun)
RATE_LIMITED = 429

_SIGNATURE = re.compile(r'data-n-a-sg="([^"]+)"')
_TIMESTAMP = re.compile(r'data-n-a-ts="([^"]+)"')


class GoogleNewsFetcher:
    source_type = "google_news_rss"

    def __init__(
        self,
        client: httpx.Client,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._decodes = 0                       # décodages tentés pendant ce run
        self._tripped: str | None = None        # raison de l'arrêt des décodages (429), None tant que tout va bien
        name, value, domain = CONSENT_COOKIE
        client.cookies.set(name, value, domain=domain)

    def validate_params(self, params: dict[str, Any]) -> None:
        reject_unknown(params, PARAMS)
        for key in sorted(PARAMS):
            require_str(params, key)
        if re.search(r"\bwhen:\d+[hdmy]\b", params["query"]):
            raise ValueError("ne pas mettre 'when:' dans la requête : le fetcher l'ajoute selon max_item_age_days")

    def fetch(self, source: Source, agent: AgentConfig, since: datetime, state: SourceState) -> list[NewsItem]:
        days = max(1, math.ceil((self._clock() - since).total_seconds() / 86400))
        params = {
            "q": f"{source.params['query']} when:{days}d",
            "hl": source.params["hl"],
            "gl": source.params["gl"],
            "ceid": source.params["ceid"],
        }
        response = request(self._client, "GET", FEED_URL, params=params)
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise FetchError(f"flux Google News illisible : {feed.get('bozo_exception')}")

        items: list[NewsItem] = []
        for entry in feed.entries:
            guid = entry.get("id") or entry.get("link")
            link = entry.get("link")
            title = (entry.get("title") or "").strip()
            if not (guid and link and title):
                continue
            published = _entry_datetime(entry)
            if published is not None and published < since:
                continue
            publisher = (entry.get("source") or {}).get("title", "")
            items.append(NewsItem(
                id=item_id(self.source_type, guid),
                agent_id=agent.agent_id,
                source_name=source.name,
                source_type=self.source_type,
                source_primary=source.primary,
                url=link,
                title=title,
                published_at=published,
                summary=f"Éditeur : {publisher}" if publisher else "",
            ))
        return items

    def fetch_text(self, item: NewsItem) -> str:
        fallback = f"{item.title}\n{item.summary}\n(texte de l'article indisponible : seuls le titre et l'éditeur sont connus)"
        if self._tripped is not None:
            log.debug("décodage Google News coupé pour ce run (%s), repli pour %s", self._tripped, item.url)
            return fallback
        if self._decodes >= MAX_DECODES_PER_RUN:
            log.info("plafond de %d décodages Google News atteint, repli pour %s", MAX_DECODES_PER_RUN, item.url)
            return fallback
        if self._decodes:
            self._sleep(DECODE_PAUSE_S)
        self._decodes += 1
        try:
            url = self.decode_url(str(item.url))
        except FetchError as exc:
            if exc.status_code == RATE_LIMITED:
                self._tripped = str(exc)
                log.warning("Google News limite le débit (429) : décodages coupés pour le reste du run")
            log.info("URL Google News non décodée pour %s : %s", item.url, exc)
            return fallback
        except (httpx.HTTPError, ValueError) as exc:
            log.info("URL Google News non décodée pour %s : %s", item.url, exc)
            return fallback
        try:
            text = article_text(request(self._client, "GET", url).content)
        except (FetchError, httpx.HTTPError, ValueError) as exc:   # site de l'éditeur : sans effet sur le disjoncteur
            log.info("article de l'éditeur indisponible (%s) : %s", url, exc)
            return fallback
        if len(text) < MIN_ARTICLE_CHARS:
            log.info("texte Google News trop court pour %s (%d caractères), repli sur le titre", url, len(text))
            return fallback
        return f"{item.title}\nSource : {url}\n\n{text}"

    def decode_url(self, link: str) -> str:
        """URL de l'éditeur derrière un lien `news.google.com/rss/articles/<id>` (API interne, RPC `Fbv4je`).

        Sans retry : réessayer un 429 ne ferait qu'aggraver la limitation pour les flux des agents suivants.
        """
        article = _article_id(link)
        page = request(self._client, "GET", ARTICLE_URL.format(article_id=article), retries=0).text
        signature, timestamp = _SIGNATURE.search(page), _TIMESTAMP.search(page)
        if not (signature and timestamp):
            raise ValueError("attributs data-n-a-sg / data-n-a-ts absents de la page Google News")
        inner = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
            f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{article}",{timestamp.group(1)},"{signature.group(1)}"]'
        )
        payload = {"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}
        response = request(
            self._client, "POST", BATCH_URL, data=payload, retries=0,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        return parse_batchexecute(response.text)


def _article_id(link: str) -> str:
    parts = urlsplit(link).path.rstrip("/").split("/")
    if len(parts) < 2 or parts[-2] != "articles" or not parts[-1]:
        raise ValueError(f"lien Google News inattendu : {link}")
    return parts[-1]


def parse_batchexecute(body: str) -> str:
    """Extrait l'URL décodée de la réponse `batchexecute` (préfixe anti-XSSI `)]}'` puis blocs JSON)."""
    for chunk in body.split("\n"):
        chunk = chunk.strip()
        if not chunk.startswith("["):
            continue
        try:
            envelope = json.loads(chunk)
        except ValueError:
            continue
        for entry in envelope:
            if isinstance(entry, list) and len(entry) > 2 and entry[:2] == ["wrb.fr", "Fbv4je"] and entry[2]:
                decoded = json.loads(entry[2])
                if isinstance(decoded, list) and len(decoded) > 1 and str(decoded[1]).startswith("http"):
                    return str(decoded[1])
    raise ValueError("URL absente de la réponse batchexecute")


def _entry_datetime(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
