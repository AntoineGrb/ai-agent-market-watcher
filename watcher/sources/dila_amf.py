"""Informations réglementées AMF via l'API info-financiere.gouv.fr (`dila_amf`, source primaire).

Voir docs/sources.md §3.2 : dataset Opendatasoft `flux-amf-new-prod`, filtré par ISIN et par date d'émission.
- Le dataset contient des dates aberrantes (ex. 5015-02-04) : toujours filtrer par ISIN et par plage de dates,
  et écarter côté client toute date dans le futur.
- Les communiqués existent en FR et en EN avec deux IDs : le LLM verra deux documents primaires, c'est accepté.
- Les franchissements de seuil (diffuseur `307`) sont gardés : ils servent à certaines règles (U-B3, U-B5).
- Texte : PDF extrait avec pypdf, seulement pour les documents retenus au tri.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from watcher.config import AgentConfig, Source
from watcher.models import NewsItem
from watcher.sources.base import SourceState, item_id, reject_unknown, require_str
from watcher.sources.http import FetchError, get_json, request
from watcher.sources.text import pdf_to_text

log = logging.getLogger(__name__)

API_URL = "https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records"
PAGE_SIZE = 100          # maximum de l'API
MAX_PAGES = 10           # garde-fou : quelques documents par jour attendus
FUTURE_TOLERANCE = timedelta(days=1)
AMF_PUBLISHER = "307"    # publication de l'AMF elle-même (franchissements de seuil, DEU)
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


class DilaAmfFetcher:
    source_type = "dila_amf"

    def __init__(self, client: httpx.Client, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._client = client
        self._clock = clock

    def validate_params(self, params: dict[str, Any]) -> None:
        reject_unknown(params, {"isin"})
        if not _ISIN.match(require_str(params, "isin")):
            raise ValueError(f"isin invalide : {params['isin']!r}")

    def _records(self, isin: str, since: datetime) -> list[dict[str, Any]]:
        where = (f'identificationsociete_iso_cd_isi="{isin}" '
                 f"and informationdeposee_inf_dat_emt>=date'{since.astimezone(UTC).date().isoformat()}'")
        records: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            data = get_json(self._client, API_URL, params={
                "where": where,
                "order_by": "informationdeposee_inf_dat_emt desc",
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
            })
            try:
                results = list(data["results"])
            except (KeyError, TypeError) as exc:
                raise FetchError(f"réponse AMF inattendue : clé absente {exc}") from exc
            records += results
            if len(results) < PAGE_SIZE:
                return records
        log.warning("AMF %s : plus de %d documents depuis %s, seuls les plus récents sont gardés",
                    isin, MAX_PAGES * PAGE_SIZE, since.date())
        return records

    def fetch(self, source: Source, agent: AgentConfig, since: datetime, state: SourceState) -> list[NewsItem]:
        isin = source.params["isin"]
        horizon = self._clock() + FUTURE_TOLERANCE
        items: list[NewsItem] = []
        for record in self._records(isin, since):
            if record.get("identificationsociete_iso_cd_isi") != isin:
                continue
            published = _parse_datetime(record.get("informationdeposee_inf_dat_emt"))
            uid, url = record.get("uin_idt_uin"), record.get("url_de_recuperation")
            title = (record.get("informationdeposee_inf_tit_inf") or "").strip()
            if published is None or not (since <= published <= horizon):
                continue
            if not (uid and url and title):
                log.warning("AMF %s : enregistrement incomplet ignoré (%s)", isin, uid)
                continue
            items.append(NewsItem(
                id=item_id(self.source_type, str(uid)),
                agent_id=agent.agent_id,
                source_name=source.name,
                source_type=self.source_type,
                source_primary=source.primary,
                url=url,
                title=title,
                published_at=published,
                summary=_summary(record),
            ))
        return items

    def fetch_text(self, item: NewsItem) -> str:
        text = pdf_to_text(request(self._client, "GET", str(item.url)).content)
        if not text:
            log.warning("PDF AMF sans texte extractible : %s", item.url)
            return f"{item.title}\n{item.summary}\n(texte du PDF non extractible)"
        return text


def _summary(record: dict[str, Any]) -> str:
    publisher = record.get("identificationdiffuseur_idi_cod_dif")
    origin = "publication de l'AMF" if publisher == AMF_PUBLISHER else "document déposé par l'émetteur"
    kind = " / ".join(filter(None, (record.get("type_d_information"), record.get("sous_type_d_information"))))
    lang = record.get("informationdeposee_inf_lng_inf")
    parts = [f"Catégorie : {kind}" if kind else "", origin, f"langue : {lang}" if lang else ""]
    return " ; ".join(p for p in parts if p)


def _parse_datetime(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
