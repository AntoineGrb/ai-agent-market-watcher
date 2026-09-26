"""SEC EDGAR, API JSON des soumissions (`edgar`, source primaire). Voir docs/sources.md §2.

- Liste : `data.sec.gov/submissions/CIK##########.json` (bloc `filings.recent`, en colonnes).
- Document envoyé au LLM : l'exhibit 99.x du dépôt (communiqué). Son nom varie selon l'agent de dépôt, d'où le
  motif `ex[h]?[-_]?99`. Un dépôt sans exhibit 99 (rapport financier en inline XBRL) est écarté : le même contenu
  arrive par un communiqué distinct.
- `User-Agent` avec contact obligatoire (politique SEC, variable `SEC_USER_AGENT`) : sans lui, la source échoue.
- L'exhibit est téléchargé dès le fetch (son titre n'existe que dans le document) : quelques dépôts par fenêtre de
  `max_item_age_days`, loin de la limite SEC de 10 requêtes par seconde. Le texte est gardé pour `fetch_text`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import httpx

from watcher.config import AgentConfig, Source
from watcher.models import NewsItem
from watcher.sources.base import SourceState, item_id, reject_unknown
from watcher.sources.http import FetchError, get_json, request
from watcher.sources.text import html_to_text, truncate

log = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
SUMMARY_CHARS = 500

_EXHIBIT_NAME = re.compile(r"ex[h]?[-_]?99", re.IGNORECASE)
_EXHIBIT_EXT = (".htm", ".html", ".txt")
_EXHIBIT_HEADER = re.compile(r"^exhibit\s+99(\.\d+)?$", re.IGNORECASE)


def _cik(params: dict[str, Any]) -> int:
    raw = params.get("cik")
    if isinstance(raw, bool) or not isinstance(raw, (str, int)) or not str(raw).strip().isdigit():
        raise ValueError(f"cik invalide : {raw!r} (nombre entier, ex. \"1760854\")")
    return int(str(raw).strip())


def pick_exhibit(names: list[str]) -> str | None:
    """Nom de l'exhibit 99 à envoyer au LLM (99.1 en priorité, par tri alphabétique)."""
    candidates = sorted(n for n in names if _EXHIBIT_NAME.search(n) and n.lower().endswith(_EXHIBIT_EXT))
    return candidates[0] if candidates else None


def split_exhibit(text: str) -> tuple[str | None, str]:
    """(titre, corps) : le titre est la première ligne après l'en-tête `EXHIBIT 99.1` (docs/sources.md §2)."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _EXHIBIT_HEADER.match(line.strip()):
            rest = [ln for ln in lines[index + 1:] if ln.strip()]
            if rest:
                return rest[0].strip(), "\n".join(rest)
    return None, text


class EdgarFetcher:
    source_type = "edgar"

    def __init__(self, client: httpx.Client, user_agent: str | None) -> None:
        self._client = client
        self._user_agent = user_agent
        self._texts: dict[str, str] = {}   # URL de l'exhibit → texte, pour fetch_text dans le même run

    def validate_params(self, params: dict[str, Any]) -> None:
        reject_unknown(params, {"cik", "forms"})
        _cik(params)
        forms = params.get("forms")
        if not isinstance(forms, list) or not forms or not all(isinstance(f, str) and f.strip() for f in forms):
            raise ValueError("paramètre 'forms' requis : liste non vide de types de formulaires (ex. [\"6-K\"])")

    def _get(self, url: str) -> httpx.Response:
        if not self._user_agent:
            raise FetchError("SEC_USER_AGENT non renseigné : la SEC exige un User-Agent avec contact")
        return request(self._client, "GET", url, headers={"User-Agent": self._user_agent})

    def _get_json(self, url: str) -> Any:
        if not self._user_agent:
            raise FetchError("SEC_USER_AGENT non renseigné : la SEC exige un User-Agent avec contact")
        return get_json(self._client, url, headers={"User-Agent": self._user_agent})

    def fetch(self, source: Source, agent: AgentConfig, since: datetime, state: SourceState) -> list[NewsItem]:
        cik = _cik(source.params)
        forms = set(source.params["forms"])
        data = self._get_json(SUBMISSIONS_URL.format(cik=cik))
        try:
            recent = data["filings"]["recent"]
            rows = [dict(zip(recent.keys(), values, strict=True)) for values in zip(*recent.values(), strict=True)]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise FetchError(f"réponse EDGAR inattendue : {exc!r}") from exc

        items: list[NewsItem] = []
        for row in rows:
            if row.get("form") not in forms:
                continue
            accepted = _parse_acceptance(row.get("acceptanceDateTime"))
            if accepted is None or accepted < since:
                continue
            item = self._filing_item(source, agent, cik, row, accepted)
            if item is not None:
                items.append(item)
        return items

    def _filing_item(
        self, source: Source, agent: AgentConfig, cik: int, row: dict[str, Any], accepted: datetime
    ) -> NewsItem | None:
        accession = str(row["accessionNumber"])
        base = ARCHIVE_URL.format(cik=cik, accession=accession.replace("-", ""))
        listing = self._get_json(base + "index.json")
        names = [str(entry.get("name", "")) for entry in listing.get("directory", {}).get("item", [])]
        exhibit = pick_exhibit(names)
        if exhibit is None:
            log.info("%s : dépôt %s %s sans exhibit 99, écarté (%s)", agent.agent_id, row.get("form"), accession,
                     row.get("primaryDocument"))
            return None
        url = base + exhibit
        text = html_to_text(self._get(url).content)
        title, body = split_exhibit(text)
        self._texts[url] = body
        return NewsItem(
            id=item_id(self.source_type, accession),
            agent_id=agent.agent_id,
            source_name=source.name,
            source_type=self.source_type,
            source_primary=source.primary,
            url=url,
            title=title or f"{row.get('form')} du {row.get('filingDate')} ({accession})",
            published_at=accepted,
            summary=truncate(body, SUMMARY_CHARS),
        )

    def fetch_text(self, item: NewsItem) -> str:
        url = str(item.url)
        if url not in self._texts:
            self._texts[url] = split_exhibit(html_to_text(self._get(url).content))[1]
        return self._texts[url]


def _parse_acceptance(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        log.warning("acceptanceDateTime EDGAR illisible : %r", raw)
        return None
