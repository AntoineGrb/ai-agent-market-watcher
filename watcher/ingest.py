"""Étapes 3 et 4 du run d'un agent (cadrage §3.1) : cours, puis fetch des sources.

Appelé dans la transaction de l'agent : la « dernière clôture traitée » et l'état des sources (instantané
ClinicalTrials...) ne sont commités que si l'agent termine son run avec succès.

Aucune erreur de cours ou de source ne fait échouer l'agent : elle est loggée et remontée comme avertissement
(heartbeat). Sans cours, les règles de prix et l'anomalie sont sautées ; l'analyse des documents continue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from watcher.config import AgentConfig, Defaults
from watcher.models import NewsItem, PriceSnapshot
from watcher.prices import PRICE_STATE_KEY, PriceError, PriceService, build_snapshot
from watcher.sources.base import SourceState, get_fetcher
from watcher.store import Store

log = logging.getLogger(__name__)


@dataclass
class AgentInputs:
    price: PriceSnapshot | None = None
    items: list[NewsItem] = field(default_factory=list)       # nouveaux documents : non vus et assez récents
    warnings: list[str] = field(default_factory=list)         # sources ou cours en erreur, replis utilisés


def load_price(
    store: Store, cfg: AgentConfig, prices: PriceService | None, *, today: date, now: datetime, inputs: AgentInputs
) -> None:
    """Construit le `PriceSnapshot` et enregistre la dernière clôture comme traitée."""
    if prices is None:
        log.info("%s : aucun provider de cours configuré, règles de prix sautées", cfg.agent_id)
        return
    state = store.get_source_state(cfg.agent_id, PRICE_STATE_KEY) or {}
    last_processed = date.fromisoformat(state["last_close_date"]) if state.get("last_close_date") else None
    try:
        fetched = prices.closes(cfg.position, today=today)
        snapshot = build_snapshot(cfg.position, fetched.closes, last_processed)
    except PriceError as exc:
        log.error("%s : cours indisponible, règles de prix et anomalie sautées : %s", cfg.agent_id, exc)
        inputs.warnings.append(f"cours indisponible : {exc}")
        return
    for warning in fetched.warnings:
        log.warning("%s : cours : %s", cfg.agent_id, warning)
        inputs.warnings.append(f"cours : {warning}")
    inputs.price = snapshot
    move = "n/d" if snapshot.daily_move_pct is None else f"{snapshot.daily_move_pct:+.2f} %"
    log.info("%s : clôture %s au %s (J-1 %s, nouvelle clôture : %s, provider %s)", cfg.agent_id,
             snapshot.last_close, snapshot.last_close_date.isoformat(), move,
             "oui" if snapshot.is_new_close else "non", fetched.provider)
    if snapshot.is_new_close:
        store.set_source_state(cfg.agent_id, PRICE_STATE_KEY, {
            "last_close_date": snapshot.last_close_date.isoformat(),
            "last_close": snapshot.last_close,
            "provider": fetched.provider,
        }, now)


def fetch_sources(store: Store, cfg: AgentConfig, defaults: Defaults, *, now: datetime, inputs: AgentInputs) -> None:
    """Fetch de chaque source active, puis filtrage : trop anciens, doublons entre sources, déjà vus."""
    since = now - timedelta(days=defaults.ingestion.max_item_age_days)
    fetched: dict[str, NewsItem] = {}
    for source in cfg.enabled_sources():
        fetcher = get_fetcher(source.type)
        if fetcher is None:
            log.warning("%s : source %r sautée, aucun fetcher pour le type %s", cfg.agent_id, source.name, source.type)
            inputs.warnings.append(f"source {source.name} : type {source.type} non implémenté")
            continue
        state = SourceState(previous=store.get_source_state(cfg.agent_id, source.name))
        try:
            items = fetcher.fetch(source, cfg, since, state)
        except Exception as exc:   # une source en erreur ne fait jamais échouer l'agent (cadrage §8.1)
            log.error("%s : source %r en erreur : %s : %s", cfg.agent_id, source.name, type(exc).__name__, exc)
            inputs.warnings.append(f"source {source.name} en erreur : {type(exc).__name__} : {exc}")
            continue
        if state.updated is not None:
            store.set_source_state(cfg.agent_id, source.name, state.updated, now)
        recent = [it for it in items if it.published_at is None or it.published_at >= since]
        log.info("%s : source %r : %d document(s), %d dans la fenêtre de %d jour(s)", cfg.agent_id, source.name,
                 len(items), len(recent), defaults.ingestion.max_item_age_days)
        for item in recent:
            fetched.setdefault(item.id, item)

    seen = store.seen_item_ids(cfg.agent_id, fetched)
    inputs.items = [it for it_id, it in fetched.items() if it_id not in seen]
    log.info("%s : %d document(s) nouveau(x), %d déjà vu(s)", cfg.agent_id, len(inputs.items), len(seen))


def gather(
    store: Store, cfg: AgentConfig, defaults: Defaults, prices: PriceService | None, *, now: datetime
) -> AgentInputs:
    inputs = AgentInputs()
    today = now.astimezone(defaults.schedule.tz).date()
    load_price(store, cfg, prices, today=today, now=now, inputs=inputs)
    fetch_sources(store, cfg, defaults, now=now, inputs=inputs)
    return inputs
