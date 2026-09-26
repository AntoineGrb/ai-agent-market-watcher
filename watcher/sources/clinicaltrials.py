"""Registre ClinicalTrials.gov, API v2 (`clinicaltrials`, source primaire). Voir docs/sources.md §1.

Détection de changement (cadrage §8.2) : un instantané des champs clés est conservé dans `source_state`.
Toute différence avec l'instantané précédent produit un `NewsItem` synthétique dont le texte est le diff.
Premier run (aucun instantané) : l'instantané sert de référence, sans document.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from watcher.config import AgentConfig, Source
from watcher.models import NewsItem
from watcher.sources.base import SourceState, item_id, reject_unknown, require_str
from watcher.sources.http import FetchError, get_json

log = logging.getLogger(__name__)

API_URL = "https://clinicaltrials.gov/api/v2/studies/{nct_id}"
STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"
_NCT_ID = re.compile(r"^NCT\d{8}$")

# Libellés affichés dans le diff (lu par le LLM), dans l'ordre de l'instantané.
FIELD_LABELS = {
    "overall_status": "Statut global",
    "primary_completion_date": "Date de fin principale (critère principal)",
    "completion_date": "Date de fin de l'étude",
    "has_results": "Résultats publiés dans le registre",
    "last_update_posted": "Date de dernière mise à jour publiée",
    "primary_outcomes": "Critères d'évaluation principaux",
}


def _date_struct(struct: dict[str, Any] | None) -> str | None:
    if not struct or not struct.get("date"):
        return None
    kind = struct.get("type")
    return f"{struct['date']} ({kind})" if kind else str(struct["date"])


def snapshot(study: dict[str, Any]) -> dict[str, Any]:
    """Champs clés de l'étude. Lève FetchError si la réponse n'a pas la structure attendue."""
    try:
        protocol = study["protocolSection"]
        status = protocol["statusModule"]
    except (KeyError, TypeError) as exc:
        raise FetchError(f"réponse ClinicalTrials inattendue : clé absente {exc}") from exc
    outcomes = (protocol.get("outcomesModule") or {}).get("primaryOutcomes") or []
    return {
        "overall_status": status.get("overallStatus"),
        "primary_completion_date": _date_struct(status.get("primaryCompletionDateStruct")),
        "completion_date": _date_struct(status.get("completionDateStruct")),
        "has_results": bool(study.get("hasResults", False)),
        "last_update_posted": (status.get("lastUpdatePostDateStruct") or {}).get("date"),
        "primary_outcomes": [o.get("measure", "") for o in outcomes],
    }


def diff(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    lines = []
    for key, label in FIELD_LABELS.items():
        before, after = previous.get(key), current.get(key)
        if before != after:
            lines.append(f"- {label} : {_fmt(before)} → {_fmt(after)}")
    return lines


def _fmt(value: Any) -> str:
    if value is None:
        return "(non renseigné)"
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, list):
        return " ; ".join(value) if value else "(aucun)"
    return str(value)


class ClinicalTrialsFetcher:
    source_type = "clinicaltrials"

    def __init__(self, client: httpx.Client, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._client = client
        self._clock = clock

    def validate_params(self, params: dict[str, Any]) -> None:
        reject_unknown(params, {"nct_id"})
        if not _NCT_ID.match(require_str(params, "nct_id")):
            raise ValueError(f"nct_id invalide : {params['nct_id']!r} (attendu NCT suivi de 8 chiffres)")

    def fetch(self, source: Source, agent: AgentConfig, since: datetime, state: SourceState) -> list[NewsItem]:
        nct_id = source.params["nct_id"]
        study = get_json(self._client, API_URL.format(nct_id=nct_id), params={"format": "json"})
        current = snapshot(study)
        state.updated = {"snapshot": current}

        previous = (state.previous or {}).get("snapshot")
        if previous is None:
            log.info("%s : instantané de référence ClinicalTrials %s enregistré", agent.agent_id, nct_id)
            return []
        changes = diff(previous, current)
        if not changes:
            return []

        title_study = (study["protocolSection"].get("identificationModule") or {}).get("briefTitle", "")
        text = "\n".join([
            f"Modification du registre ClinicalTrials.gov pour l'étude {nct_id}.",
            f"Titre de l'étude : {title_study}" if title_study else "",
            "Champs modifiés (avant → après) :",
            *changes,
        ]).replace("\n\n", "\n")
        changed = ", ".join(FIELD_LABELS[k].lower() for k in FIELD_LABELS if previous.get(k) != current.get(k))
        return [NewsItem(
            id=item_id(self.source_type, nct_id, json.dumps(current, sort_keys=True)),
            agent_id=agent.agent_id,
            source_name=source.name,
            source_type=self.source_type,
            source_primary=source.primary,
            url=STUDY_URL.format(nct_id=nct_id),
            title=f"ClinicalTrials.gov {nct_id} : modification du registre ({changed})",
            published_at=self._clock(),     # date de détection : le registre ne date pas chaque champ
            summary=text,
            text=text,
        )]

    def fetch_text(self, item: NewsItem) -> str:
        return item.text or item.summary   # le diff est déjà complet : rien à télécharger
