"""Point d'entrée : `python -m watcher.run --all | --agent ID [--baseline | --inject FILE [--primary]] | --test-mail
| --heartbeat`.

Orchestration d'un run (cadrage §3.1). Invariants :
- isolation par agent : une config invalide ou une exception n'arrête jamais les autres agents ;
- l'état d'un agent (seen_items, events, source_state...) n'est commité que si l'agent a réussi ;
- Healthchecks reçoit /start puis succès ou /fail (runs `--all` uniquement : le check surveille le cron quotidien).

Chargement des configs, erreurs de config, store, monitoring, cours et fetch des sources (`ingest`), tri et
analyse LLM (`llm`), moteur déterministe (résolution, règles price / time / surveillances / anomalie,
dédoublonnage, outbox) et envoi de l'outbox. Les documents d'un agent ne sont marqués vus que s'il a réussi.
Le budget de tokens est commun à tout le run ; son dépassement fait échouer le run (cadrage §7.5).
Heartbeat hebdomadaire (cadrage §9.3) : le jour `schedule.heartbeat_weekday`, après le run quotidien (`--all`),
une seule fois par jour ; `--heartbeat` l'envoie immédiatement, sans exécuter les agents.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from watcher.config import (
    AgentConfig,
    AgentLoadError,
    AgentsLoadResult,
    ConfigError,
    Defaults,
    discover_agents,
    load_agents,
    load_defaults,
)
from watcher.engine.pipeline import evaluate_agent
from watcher.fixtures import load_fixture
from watcher.ingest import gather
from watcher.llm import LlmBudgetExceeded, LlmConfigError, LlmLayer, LlmOutcome, LlmUsage, TokenBudget
from watcher.monitoring import Healthchecks, setup_logging
from watcher.notify import templates
from watcher.notify.dispatch import send_outbox
from watcher.notify.heartbeat import build_heartbeat
from watcher.notify.mailer import Mailer, MailError
from watcher.prices import EuronextProvider, PriceService, YFinanceProvider
from watcher.settings import PROJECT_ROOT, Settings, SettingsError
from watcher.sources import register_builtin_fetchers
from watcher.sources.base import validate_source_params
from watcher.sources.http import build_client
from watcher.models import NewsItem
from watcher.store import RunTotals, Store

log = logging.getLogger("watcher.run")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

GLOBAL_ERROR_KEY = "_global"
MAIL_ERROR_KEY = "_mail"
BUDGET_ERROR_KEY = "_budget"   # budget de tokens du run épuisé : le run passe en échec
WARNINGS_KEY = "_warnings"     # dans runs.errors_json : sources / cours en erreur, sans effet sur le statut du run


@dataclass(frozen=True)
class LoadedRun:
    """Paramètres globaux et agents chargés pendant le run (réutilisés par le heartbeat)."""

    defaults: Defaults
    agents: AgentsLoadResult


def run_scope(*, only: str | None, baseline: bool, injected: bool) -> str:
    """Portée enregistrée dans `runs.scope` : seul le run quotidien (`all`) compte dans « X/7 runs OK »."""
    if only is None:
        return "baseline" if baseline else "all"
    agent_id = only.upper()
    if injected:
        return f"inject {agent_id}"
    return f"baseline {agent_id}" if baseline else f"agent {agent_id}"


@dataclass
class AgentOutcome:
    alerts_created: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    run_id: int
    status: str = "running"
    agents_ok: list[str] = field(default_factory=list)
    agents_failed: list[str] = field(default_factory=list)
    alerts_created: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, list[str]] = field(default_factory=dict)   # par agent ; remontés dans le heartbeat
    usage: LlmUsage = field(default_factory=LlmUsage)             # tokens consommés sur tout le run

    def add_error(self, key: str, message: str) -> None:
        self.errors[key] = f"{self.errors[key]} ; {message}" if key in self.errors else message

    def finalize(self) -> str:
        if GLOBAL_ERROR_KEY in self.errors or BUDGET_ERROR_KEY in self.errors or (
                self.agents_failed and not self.agents_ok):
            self.status = "failed"
        elif self.errors:
            self.status = "partial"
        else:
            self.status = "ok"
        return self.status

    def summary(self) -> str:
        lines = [f"run {self.run_id} : {self.status}, agents OK : {len(self.agents_ok)}, "
                 f"en échec : {len(self.agents_failed)}, tokens : {self.usage.input_tokens} en entrée, "
                 f"{self.usage.output_tokens} en sortie"]
        lines += [f"- {key} : {message}" for key, message in self.errors.items()]
        lines += [f"- avertissement {agent} : {w}" for agent, ws in self.warnings.items() for w in ws]
        return "\n".join(lines)

    def persisted_errors(self) -> dict[str, object] | None:
        payload: dict[str, object] = dict(self.errors)
        if self.warnings:
            payload[WARNINGS_KEY] = self.warnings
        return payload or None


class Runner:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        mailer: Mailer,
        healthchecks: Healthchecks,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        prices: PriceService | None = None,
        llm: LlmLayer | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.mailer = mailer
        self.hc = healthchecks
        self.clock = clock
        self.prices = prices
        self.llm = llm

    def run(
        self,
        *,
        now: datetime,
        only: str | None = None,
        baseline: bool = False,
        injected: list[NewsItem] | None = None,
    ) -> RunReport:
        """Exécute un run complet. `now` est injecté (UTC, avec fuseau) : jamais d'horloge en dur plus bas.

        `injected` (`--inject`, avec `only`) : documents traités à la place du fetch des sources.
        """
        monitored = only is None
        if monitored:
            self.hc.start()
        scope = run_scope(only=only, baseline=baseline, injected=injected is not None)
        report = RunReport(run_id=self.store.start_run(self.settings.env, now, scope))
        log.info("run %d démarré (env=%s, portée=%s)", report.run_id, self.settings.env, scope)
        loaded: LoadedRun | None = None
        try:
            loaded = self._run_agents(report, now=now, only=only, baseline=baseline, injected=injected)
        except Exception as exc:   # erreur hors agent (defaults, dossier agents...) : le run entier échoue
            log.exception("run %d en échec global", report.run_id)
            report.errors[GLOBAL_ERROR_KEY] = str(exc)
        self._record(report)

        # Étape 12 : heartbeat, après l'enregistrement du run pour qu'il y figure. Un échec d'envoi dégrade le run.
        if loaded is not None and monitored and not baseline and self._heartbeat_due(loaded.defaults, now):
            if not self.send_heartbeat(loaded, now=now, report=report):
                self._record(report)
        log.info(report.summary())
        if monitored:
            if report.status == "ok":
                self.hc.success(report.summary())
            else:
                self.hc.fail(report.summary())
        return report

    def _record(self, report: RunReport) -> None:
        report.finalize()
        self.store.finish_run(report.run_id, self.clock(), RunTotals(
            status=report.status,
            agents_ok=len(report.agents_ok),
            agents_failed=len(report.agents_failed),
            alerts_created=report.alerts_created,
            input_tokens=report.usage.input_tokens,
            output_tokens=report.usage.output_tokens,
            errors=report.persisted_errors(),
            usage_by_model=report.usage.by_model or None,
        ))

    def _run_agents(
        self, report: RunReport, *, now: datetime, only: str | None, baseline: bool, injected: list[NewsItem] | None
    ) -> LoadedRun:
        defaults = load_defaults(self.settings.agents_dir)
        loaded = load_agents(self.settings.agents_dir, only=only, validate_source=validate_source_params)
        budget = TokenBudget(defaults.llm_budget.max_total_tokens_per_run, used=report.usage)

        for agent_id, error in loaded.errors.items():
            report.agents_failed.append(agent_id)
            report.errors[agent_id] = f"config invalide : {error.message}"
            self._handle_config_error(report, error, now=now, notify=not baseline)

        for agent_id, cfg in loaded.configs.items():
            self.store.resolve_config_errors(agent_id, now)
            try:
                with self.store.transaction():
                    outcome = self._process_agent(cfg, defaults, now=now, baseline=baseline, budget=budget,
                                                  injected=injected)
            except Exception as exc:
                log.exception("agent %s en échec, état non commité", agent_id)
                report.agents_failed.append(agent_id)
                report.errors[agent_id] = f"{type(exc).__name__} : {exc}"
                if isinstance(exc, LlmBudgetExceeded):
                    report.add_error(BUDGET_ERROR_KEY, str(exc))
            else:
                report.agents_ok.append(agent_id)
                report.alerts_created += outcome.alerts_created
                if outcome.warnings:
                    report.warnings[agent_id] = outcome.warnings

        if not baseline:
            self._send_outbox(report, defaults, now=now)
        return LoadedRun(defaults, loaded)

    def _send_outbox(self, report: RunReport, defaults: Defaults, *, now: datetime) -> None:
        """Étape 11 : toutes les alertes non envoyées partent, y compris celles des runs précédents."""
        dispatch = send_outbox(self.store, self.mailer, priority=defaults.priority, now=now,
                               digest_day=now.astimezone(defaults.schedule.tz).date(),
                               send_empty_digest=defaults.digest.send_if_empty)
        log.info("outbox : %d mail(s) envoyé(s), %d alerte(s)", dispatch.mails_sent, dispatch.alerts_sent)
        for error in dispatch.errors:
            report.add_error(MAIL_ERROR_KEY, error)

    # ------------------------------------------------------------------ heartbeat

    def _heartbeat_due(self, defaults: Defaults, now: datetime) -> bool:
        today = now.astimezone(defaults.schedule.tz).date()
        return today.weekday() == defaults.schedule.heartbeat_weekday and not self.store.heartbeat_sent_on(today)

    def send_heartbeat(self, loaded: LoadedRun, *, now: datetime, report: RunReport | None = None) -> bool:
        """Envoie le heartbeat et l'enregistre. Retourne False si le mail n'est pas parti (retenté au run suivant
        du même jour, sinon la semaine suivante : Healthchecks reste le filet de sécurité)."""
        defaults = loaded.defaults
        data = build_heartbeat(self.store, defaults, configs=loaded.agents.configs,
                               invalid={a: e.message for a, e in loaded.agents.errors.items()}, now=now)
        mail = templates.heartbeat_mail(data, tz=defaults.schedule.tz)
        try:
            self.mailer.send(mail)
        except MailError as exc:
            log.error("heartbeat non envoyé : %s", exc)
            if report is not None:
                report.add_error(MAIL_ERROR_KEY, f"heartbeat : {exc}")
            return False
        self.store.record_heartbeat(data.today, now)
        log.info("heartbeat envoyé : %s", mail.subject)
        return True

    def heartbeat_now(self, *, now: datetime) -> bool:
        """`--heartbeat` : envoi immédiat, sans exécuter les agents ni toucher à leur état."""
        defaults = load_defaults(self.settings.agents_dir)
        agents = load_agents(self.settings.agents_dir, validate_source=validate_source_params)
        return self.send_heartbeat(LoadedRun(defaults, agents), now=now)

    def _handle_config_error(self, report: RunReport, error: AgentLoadError, *, now: datetime, notify: bool) -> None:
        """Mail immédiat à la première détection d'une erreur (dédoublonnée sur son empreinte)."""
        must_notify = self.store.record_config_error(error.agent_id, error.fingerprint, error.message, now)
        if not (must_notify and notify):
            return
        try:
            self.mailer.send(templates.config_error_mail(error.agent_id, error.message))
        except MailError as exc:
            # notified_at reste NULL : nouvelle tentative au prochain run.
            log.error("mail d'erreur de config %s non envoyé : %s", error.agent_id, exc)
            report.add_error(MAIL_ERROR_KEY, str(exc))
        else:
            self.store.mark_config_error_notified(error.agent_id, error.fingerprint, now)

    def _process_agent(
        self,
        cfg: AgentConfig,
        defaults: Defaults,
        *,
        now: datetime,
        baseline: bool,
        budget: TokenBudget,
        injected: list[NewsItem] | None = None,
    ) -> AgentOutcome:
        """Traite un agent dans sa transaction : alertes écrites dans l'outbox et avertissements (sources, cours)."""
        agent_id = cfg.agent_id
        if cfg.position.status == "CLOSED":
            if cancelled := self.store.cancel_armed_watches(agent_id, now):
                log.info("%s : %d surveillance(s) armée(s) annulée(s) (position CLOSED)", agent_id, cancelled)
            log.info("%s : statut CLOSED, agent ignoré", agent_id)
            return AgentOutcome()
        today = now.astimezone(defaults.schedule.tz).date()
        fired = self.store.fired_rule_ids(agent_id)
        active = cfg.active_rules(fired)
        sources = cfg.enabled_sources()
        log.info("%s : statut %s, %d règle(s) active(s) sur %d, %d source(s) active(s) sur %d, jour %s",
                 agent_id, cfg.position.status, len(active), len(cfg.rules), len(sources), len(cfg.sources),
                 today.isoformat())
        # Étapes 3 et 4 du pipeline : cours (et dernière clôture traitée), fetch des sources.
        inputs = gather(self.store, cfg, defaults, self.prices, now=now, injected=injected)
        if baseline:
            # Premier démarrage : tout ce qui existe est marqué vu, sans tri, sans analyse, sans alerte (§11.7).
            self.store.mark_seen(inputs.items, now)
            log.info("%s : baseline, %d document(s) marqué(s) vu(s)", agent_id, len(inputs.items))
            return AgentOutcome(warnings=inputs.warnings)
        # Étapes 5 et 6 : tri puis analyse. Une erreur LLM fait échouer l'agent (documents non marqués vus).
        llm = LlmOutcome()
        if inputs.items:
            if self.llm is None:
                raise LlmConfigError("couche LLM non configurée")
            llm = self.llm.process(cfg, defaults, inputs.items, inputs.price, today=today, fired=fired,
                                   budget=budget)
        # Étapes 7 à 9 : résolution, règles déterministes, dédoublonnage, outbox.
        evaluation = evaluate_agent(self.store, cfg, defaults, matches=llm.matches, items=llm.items,
                                    price=inputs.price, now=now, fired=fired)
        # Étape 10 : documents marqués vus dans la transaction de l'agent, commitée seulement s'il réussit.
        self.store.mark_seen(inputs.items, now, relevant=llm.relevant)
        return AgentOutcome(alerts_created=len(evaluation.emitted), warnings=inputs.warnings + llm.warnings)


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m watcher.run", description="Veille boursière par agents.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="exécute tous les agents (run quotidien)")
    target.add_argument("--agent", metavar="ID", help="exécute un seul agent (ex. NANO)")
    target.add_argument("--test-mail", action="store_true",
                        help="envoie un mail pour valider la config SMTP (force WATCHER_ENV=test)")
    target.add_argument("--heartbeat", action="store_true",
                        help="envoie immédiatement le heartbeat hebdomadaire, sans exécuter les agents")
    parser.add_argument("--baseline", action="store_true",
                        help="premier démarrage : marque tous les documents comme vus, sans LLM ni mail")
    parser.add_argument("--inject", metavar="FILE", type=Path,
                        help="avec --agent : fait passer une fixture par tout le pipeline (force WATCHER_ENV=test)")
    parser.add_argument("--primary", action="store_true",
                        help="avec --inject : traite le document comme issu d'une source primaire")
    return parser


def build_price_service(client: httpx.Client) -> PriceService:
    """yfinance pour toutes les lignes, repli CSV Euronext pour Euronext Paris (docs/sources.md §6.2 bis)."""
    return PriceService(YFinanceProvider(), euronext_fallback=EuronextProvider(client))


def _utcnow() -> datetime:
    """Seule horloge réelle de la CLI (remplacée dans les tests)."""
    return datetime.now(UTC)


def _load_dotenv() -> None:
    """Charge `.env` en local si python-dotenv est installé (dépendance de dev). En conteneur : env_file."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.test_mail or args.heartbeat) and args.baseline:
        parser.error("--baseline est incompatible avec --test-mail et --heartbeat")
    if args.inject is not None and (args.agent is None or args.baseline):
        parser.error("--inject exige --agent et est incompatible avec --baseline")
    if args.primary and args.inject is None:
        parser.error("--primary n'a de sens qu'avec --inject")

    _load_dotenv()
    # --test-mail et --inject forcent l'environnement de test : jamais de pollution de l'état de prod.
    force_test = args.test_mail or args.inject is not None
    try:
        settings = Settings.from_env(**({"env": "test"} if force_test else {}))
    except SettingsError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return EXIT_USAGE
    setup_logging(settings.log_dir)
    mailer = Mailer(settings)
    now = _utcnow()

    if args.test_mail:
        try:
            mailer.send(templates.smtp_check_mail(settings.env, now.astimezone()))
        except MailError as exc:
            log.error("mail de test non envoyé : %s", exc)
            return EXIT_FAILED
        return EXIT_OK

    if args.agent:
        try:
            known = discover_agents(settings.agents_dir)
        except ConfigError as exc:
            log.error("%s", exc)
            return EXIT_USAGE
        if args.agent.upper() not in known:
            log.error("agent inconnu : %s (disponibles : %s)", args.agent, ", ".join(known) or "aucun")
            return EXIT_USAGE

    injected: list[NewsItem] | None = None
    if args.inject is not None:
        try:
            fixture = load_fixture(args.inject)
        except ConfigError as exc:
            log.error("%s", exc)
            return EXIT_USAGE
        if fixture.header.agent != args.agent.upper():
            log.error("la fixture %s concerne l'agent %s, pas %s", args.inject, fixture.header.agent,
                      args.agent.upper())
            return EXIT_USAGE
        injected = [fixture.to_item(now=now, primary=True if args.primary else None)]
        log.info("injection de %s (source %s, primaire : %s)", fixture.name, injected[0].source_name,
                 injected[0].source_primary)

    healthchecks = Healthchecks(settings.healthchecks_ping_url, enabled=settings.healthchecks_enabled)
    with build_client() as client, Store.open(settings.db_path) as store:
        register_builtin_fetchers(settings, client)
        runner = Runner(settings, store, mailer, healthchecks, prices=build_price_service(client),
                        llm=LlmLayer(settings))
        if args.heartbeat:
            try:
                return EXIT_OK if runner.heartbeat_now(now=now) else EXIT_FAILED
            except ConfigError as exc:
                log.error("heartbeat impossible : %s", exc)
                return EXIT_FAILED
        report = runner.run(now=now, only=args.agent, baseline=args.baseline, injected=injected)
    return EXIT_OK if report.status == "ok" else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
