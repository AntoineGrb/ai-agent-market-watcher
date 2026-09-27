"""Données du heartbeat hebdomadaire (cadrage §9.3), rassemblées depuis la base et les configs chargées.

Le rendu est dans `templates.heartbeat_mail`. Fenêtre : les 7 jours locaux se terminant aujourd'hui (le run du jour
inclus). « X/7 runs OK » compte les jours ayant au moins un run quotidien (`--all`) terminé en `ok`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from watcher.config import AgentConfig, Defaults, TimeRule
from watcher.engine.describe import num
from watcher.engine.time_rules import due_date
from watcher.store import ArmedWatch, ConfigErrorRow, EventRow, RunRow, Store

WINDOW_DAYS = 7
DAILY_SCOPE = "all"
WARNINGS_KEY = "_warnings"   # même clé que `run.WARNINGS_KEY` (runs.errors_json)


@dataclass(frozen=True)
class AgentState:
    agent_id: str
    state: str              # WATCH | OWNED | CLOSED | désactivé (config invalide)
    detail: str | None = None


@dataclass(frozen=True)
class Occurrence:
    key: str                # agent ou clé d'erreur du run (`_mail`, `_budget`...)
    message: str
    count: int


@dataclass
class HeartbeatData:
    today: date
    start: date
    runs: list[RunRow]
    days_ok: int
    days_without_ok: list[date]
    sent: list[EventRow]
    agents: list[AgentState]
    config_errors: list[ConfigErrorRow]
    errors: list[Occurrence]
    warnings: list[Occurrence]
    watches: list[ArmedWatch]
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_by_model: dict[str, list[int]] = field(default_factory=dict)
    cost_usd: float = 0.0
    unpriced_models: list[str] = field(default_factory=list)
    attention: list[str] = field(default_factory=list)


def _occurrences(pairs: list[tuple[str, str]]) -> list[Occurrence]:
    counts = Counter(pairs)
    return [Occurrence(key, message, n) for (key, message), n in sorted(counts.items(), key=lambda kv: kv[0])]


def _agent_state(cfg: AgentConfig) -> AgentState:
    p = cfg.position
    if p.status == "OWNED" and p.entry_price is not None and p.entry_date is not None:
        detail = f"entrée à {num(p.entry_price, 2)} {p.currency} le {p.entry_date:%d/%m/%Y}"
    else:
        detail = None
    return AgentState(cfg.agent_id, p.status, detail)


def _attention(cfg: AgentConfig, defaults: Defaults, store: Store, today: date) -> list[str]:
    """Points à vérifier à la main : contexte ancien, nombre d'actions, sources coupées, time stops proches."""
    hb = defaults.heartbeat
    agent_id, p = cfg.agent_id, cfg.position
    items: list[str] = []
    context_age = (today - cfg.context_updated_at).days
    if context_age > hb.context_max_age_days:
        items.append(f"{agent_id} : contexte daté du {cfg.context_updated_at:%d/%m/%Y} ({context_age} jours), "
                     "relis la thèse et prompt.md puis mets à jour context_updated_at")
    if p.shares_outstanding is None:
        items.append(f"{agent_id} : shares_outstanding non renseigné (dilution non calculable sans chiffre extrait)")
    elif p.shares_outstanding_as_of is None:
        items.append(f"{agent_id} : shares_outstanding sans date (shares_outstanding_as_of absent)")
    elif (shares_age := (today - p.shares_outstanding_as_of).days) > hb.shares_outstanding_max_age_days:
        items.append(f"{agent_id} : shares_outstanding au {p.shares_outstanding_as_of:%d/%m/%Y} ({shares_age} jours), "
                     "à mettre à jour depuis la dernière publication des droits de vote")
    items += [f"{agent_id} : source « {s.name} » désactivée (enabled: false)" for s in cfg.sources if not s.enabled]
    for rule in cfg.active_rules(store.fired_rule_ids(agent_id)):
        if not isinstance(rule, TimeRule) or (due := due_date(rule, cfg)) is None:
            continue
        days_left = (due - today).days
        if 0 <= days_left <= hb.time_stop_warning_days:
            note = f" : {rule.note}" if rule.note else ""
            items.append(f"{agent_id} : time stop {rule.id} le {due:%d/%m/%Y} (dans {days_left} jours){note}")
    return items


def build_heartbeat(
    store: Store,
    defaults: Defaults,
    *,
    configs: Mapping[str, AgentConfig],
    invalid: Mapping[str, str],
    now: datetime,
) -> HeartbeatData:
    """`configs` : agents chargés ce run ; `invalid` : agents désactivés pour config invalide (ID → message)."""
    tz = defaults.schedule.tz
    today = now.astimezone(tz).date()
    start = today - timedelta(days=WINDOW_DAYS - 1)
    since = datetime.combine(start, time(0), tzinfo=tz)

    runs = store.runs_since(since)
    ok_days = {r.started_at.astimezone(tz).date() for r in runs if r.scope == DAILY_SCOPE and r.status == "ok"}
    window = [start + timedelta(days=i) for i in range(WINDOW_DAYS)]

    errors: list[tuple[str, str]] = []
    warnings: list[tuple[str, str]] = []
    tokens_by_model: dict[str, list[int]] = {}
    for run in runs:
        for key, value in run.errors.items():
            if key == WARNINGS_KEY and isinstance(value, dict):
                warnings += [(agent, str(w)) for agent, ws in value.items() for w in ws]
            else:
                errors.append((key, str(value)))
        for model, (tokens_in, tokens_out) in run.usage_by_model.items():
            totals = tokens_by_model.setdefault(model, [0, 0])
            totals[0] += tokens_in
            totals[1] += tokens_out

    cost = 0.0
    unpriced: list[str] = []
    for model, (tokens_in, tokens_out) in sorted(tokens_by_model.items()):
        price = defaults.llm_pricing.get(model)
        if price is None:
            unpriced.append(model)
        else:
            cost += price.cost_usd(tokens_in, tokens_out)

    agents = [_agent_state(configs[a]) for a in sorted(configs)]
    agents += [AgentState(a, "désactivé (config invalide)") for a in sorted(invalid)]
    agents.sort(key=lambda s: s.agent_id)
    attention = [item for a in sorted(configs) if configs[a].position.status != "CLOSED"
                 for item in _attention(configs[a], defaults, store, today)]

    return HeartbeatData(
        today=today,
        start=start,
        runs=runs,
        days_ok=len(ok_days),
        days_without_ok=[d for d in window if d not in ok_days],
        sent=store.sent_events_since(since),
        agents=agents,
        config_errors=store.open_config_errors(),
        errors=_occurrences(errors),
        warnings=_occurrences(warnings),
        watches=store.active_armed_watches(),
        input_tokens=sum(r.input_tokens for r in runs),
        output_tokens=sum(r.output_tokens for r in runs),
        tokens_by_model=tokens_by_model,
        cost_usd=cost,
        unpriced_models=unpriced,
        attention=attention,
    )
