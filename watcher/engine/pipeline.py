"""Étapes 7 à 9 du run d'un agent (cadrage §3.1) : résolution, règles déterministes, dédoublonnage, outbox.

Appelé dans la transaction de l'agent : alertes, surveillances armées et changements de statut ne sont commités
que si l'agent termine son run avec succès. Aucune horloge en dur : `now` est injecté.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from watcher.config import Action, AgentConfig, Defaults
from watcher.engine.dedup import dedup_key, should_emit, uses_window
from watcher.engine.describe import rule_text
from watcher.engine.price_rules import evaluate_anomaly, evaluate_armed_watches, evaluate_price_rules
from watcher.engine.priority import sort_key
from watcher.engine.resolve import PendingArm, resolve_matches
from watcher.engine.time_rules import evaluate_time_rules
from watcher.models import Alert, NewsItem, PriceSnapshot, RuleMatch
from watcher.store import Store

log = logging.getLogger(__name__)

ANOMALY_EXPLAINING_ORIGINS = ("event",)   # seule une actualité « explique » un mouvement de cours


@dataclass
class AgentEvaluation:
    emitted: list[Alert] = field(default_factory=list)      # écrites dans l'outbox (sent_at = NULL)
    suppressed: list[Alert] = field(default_factory=list)   # doublons : journalisées seulement
    ignored: list[Alert] = field(default_factory=list)      # matches IGNORE : journalisés seulement
    armed: list[str] = field(default_factory=list)          # surveillances créées ce run


class _AgentPipeline:
    def __init__(self, store: Store, cfg: AgentConfig, defaults: Defaults, now: datetime,
                 price: PriceSnapshot | None) -> None:
        self.store = store
        self.cfg = cfg
        self.defaults = defaults
        self.now = now
        self.price = price
        self.result = AgentEvaluation()

    @property
    def agent_id(self) -> str:
        return self.cfg.agent_id

    def with_context(self, alert: Alert) -> Alert:
        """Fige dans l'alerte ce dont le mail a besoin : texte de la règle, devise, prix d'entrée, cours."""
        position = self.cfg.position
        return alert.model_copy(update={
            "rule_text": alert.rule_text or rule_text(alert, self.cfg, self.defaults),
            "currency": position.currency,
            "entry_price": position.entry_price if position.status == "OWNED" else None,
            "price": alert.price or self.price,
        })

    def emit(self, alert: Alert) -> int | None:
        """Écrit l'alerte dans l'outbox sauf doublon. Retourne l'ID de l'événement créé."""
        alert = self.with_context(alert)
        key = dedup_key(alert, self.cfg.position.entry_date)
        since = self.now - timedelta(days=self.defaults.dedup.event_window_days) if uses_window(alert) else None
        previous = [row.alert for row in self.store.events_by_key(self.agent_id, key, since)]
        ok, why = should_emit(alert, previous, self.defaults.priority)
        if not ok:
            log.info("%s : alerte %s (%s) supprimée : %s", self.agent_id, alert.rule_id, alert.origin, why)
            self.result.suppressed.append(alert)
            return None
        event_id = self.store.insert_event(alert, key, self.now)
        self.result.emitted.append(alert)
        log.info("%s : alerte %s %s %s écrite dans l'outbox (%s)", self.agent_id, alert.rule_id,
                 alert.action.value, alert.severity.value, why)
        return event_id

    def arm(self, pending: PendingArm, event_id: int) -> None:
        arm = pending.arm
        if any(w.arm_id == arm.id for w in self.store.active_armed_watches(self.agent_id)):
            log.info("%s : surveillance %s déjà active, pas de nouvel armement", self.agent_id, arm.id)
            return
        self.store.create_armed_watch(self.agent_id, arm, pending.ref_value, event_id, self.now)
        self.result.armed.append(arm.id)
        log.info("%s : surveillance %s armée (référence %g, seuil %s %g)", self.agent_id, arm.id,
                 pending.ref_value, arm.when.op, arm.when.value)


def evaluate_agent(
    store: Store,
    cfg: AgentConfig,
    defaults: Defaults,
    *,
    matches: Sequence[RuleMatch],
    items: Mapping[str, NewsItem],
    price: PriceSnapshot | None,
    now: datetime,
    fired: set[str],
) -> AgentEvaluation:
    """Construit, dédoublonne et écrit dans l'outbox toutes les alertes d'un agent pour ce run.

    `fired` : règles déjà déclenchées (historique complet de `events`), pour `unless_fired`.
    """
    pipe = _AgentPipeline(store, cfg, defaults, now, price)
    today = now.astimezone(defaults.schedule.tz).date()
    active = cfg.active_rules(fired)

    # Étape 7 : matches du LLM, du plus prioritaire au moins prioritaire (le meilleur passe le dédoublonnage).
    resolved = resolve_matches(matches, cfg=cfg, defaults=defaults, items=items, price=price, today=today,
                               fired=fired)
    for r in sorted(resolved, key=lambda r: sort_key(r.alert, defaults.priority)):
        if r.alert.action is Action.IGNORE:
            log.info("%s : match %s → IGNORE, journalisé sans envoi", cfg.agent_id, r.alert.rule_id)
            pipe.result.ignored.append(r.alert)
            continue
        event_id = pipe.emit(r.alert)
        if event_id is not None and r.arm is not None:
            pipe.arm(r.arm, event_id)

    # Étape 8 : règles déterministes. Les surveillances créées à l'instant sont évaluées dans ce même run.
    watches = store.active_armed_watches(cfg.agent_id)
    for alert in evaluate_price_rules(cfg, active, price, watch_active=bool(watches)):
        pipe.emit(alert)
    for watch, alert in evaluate_armed_watches(cfg, watches, price):
        pipe.emit(alert)
        store.mark_watch_fired(watch.id, now)   # une surveillance ne se déclenche qu'une fois, même en doublon
    for alert in evaluate_time_rules(cfg, active, today):
        pipe.emit(alert)

    quiet_since = now - timedelta(days=defaults.price_anomaly.quiet_days)
    explained = bool(resolved) or store.has_events_since(cfg.agent_id, quiet_since, ANOMALY_EXPLAINING_ORIGINS)
    if (anomaly := evaluate_anomaly(cfg, defaults, price, explained=explained)) is not None:
        pipe.emit(anomaly)

    r = pipe.result
    log.info("%s : %d match(es) résolu(s), %d alerte(s) écrite(s), %d supprimée(s), %d ignorée(s)",
             cfg.agent_id, len(resolved), len(r.emitted), len(r.suppressed), len(r.ignored))
    return r
