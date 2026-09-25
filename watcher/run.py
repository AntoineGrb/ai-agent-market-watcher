"""Point d'entrée : `python -m watcher.run --all | --agent ID [--baseline] | --test-mail`.

Orchestration d'un run (cadrage §3.1). Invariants :
- isolation par agent : une config invalide ou une exception n'arrête jamais les autres agents ;
- l'état d'un agent (seen_items, events, source_state...) n'est commité que si l'agent a réussi ;
- Healthchecks reçoit /start puis succès ou /fail (runs `--all` uniquement : le check surveille le cron quotidien).

Étape 1 du plan : chargement des configs, erreurs de config, store, monitoring. Les étapes 3 à 11 du pipeline
(cours, sources, LLM, moteur, envoi) sont branchées aux étapes 2 à 5 du plan.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from watcher.config import (
    AgentConfig,
    AgentLoadError,
    ConfigError,
    Defaults,
    discover_agents,
    load_agents,
    load_defaults,
)
from watcher.monitoring import Healthchecks, setup_logging
from watcher.notify import templates
from watcher.notify.mailer import Mailer, MailError
from watcher.settings import PROJECT_ROOT, Settings, SettingsError
from watcher.sources.base import validate_source_params
from watcher.store import RunTotals, Store

log = logging.getLogger("watcher.run")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

GLOBAL_ERROR_KEY = "_global"
MAIL_ERROR_KEY = "_mail"


@dataclass
class RunReport:
    run_id: int
    status: str = "running"
    agents_ok: list[str] = field(default_factory=list)
    agents_failed: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def finalize(self) -> str:
        if GLOBAL_ERROR_KEY in self.errors or (self.agents_failed and not self.agents_ok):
            self.status = "failed"
        elif self.errors:
            self.status = "partial"
        else:
            self.status = "ok"
        return self.status

    def summary(self) -> str:
        lines = [f"run {self.run_id} : {self.status}, agents OK : {len(self.agents_ok)}, "
                 f"en échec : {len(self.agents_failed)}"]
        lines += [f"- {key} : {message}" for key, message in self.errors.items()]
        return "\n".join(lines)


class Runner:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        mailer: Mailer,
        healthchecks: Healthchecks,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.store = store
        self.mailer = mailer
        self.hc = healthchecks
        self.clock = clock

    def run(self, *, now: datetime, only: str | None = None, baseline: bool = False) -> RunReport:
        """Exécute un run complet. `now` est injecté (UTC, avec fuseau) : jamais d'horloge en dur plus bas."""
        monitored = only is None
        if monitored:
            self.hc.start()
        report = RunReport(run_id=self.store.start_run(self.settings.env, now))
        log.info("run %d démarré (env=%s, agents=%s, baseline=%s)",
                 report.run_id, self.settings.env, only or "tous", baseline)
        try:
            self._run_agents(report, now=now, only=only, baseline=baseline)
        except Exception as exc:   # erreur hors agent (defaults, dossier agents...) : le run entier échoue
            log.exception("run %d en échec global", report.run_id)
            report.errors[GLOBAL_ERROR_KEY] = str(exc)

        report.finalize()
        self.store.finish_run(report.run_id, self.clock(), RunTotals(
            status=report.status,
            agents_ok=len(report.agents_ok),
            agents_failed=len(report.agents_failed),
            errors=report.errors or None,
        ))
        log.info(report.summary())
        if monitored:
            if report.status == "ok":
                self.hc.success(report.summary())
            else:
                self.hc.fail(report.summary())
        return report

    def _run_agents(self, report: RunReport, *, now: datetime, only: str | None, baseline: bool) -> None:
        defaults = load_defaults(self.settings.agents_dir)
        loaded = load_agents(self.settings.agents_dir, only=only, validate_source=validate_source_params)

        for agent_id, error in loaded.errors.items():
            report.agents_failed.append(agent_id)
            report.errors[agent_id] = f"config invalide : {error.message}"
            self._handle_config_error(report, error, now=now, notify=not baseline)

        for agent_id, cfg in loaded.configs.items():
            self.store.resolve_config_errors(agent_id, now)
            try:
                with self.store.transaction():
                    self._process_agent(cfg, defaults, now=now, baseline=baseline)
            except Exception as exc:
                log.exception("agent %s en échec, état non commité", agent_id)
                report.agents_failed.append(agent_id)
                report.errors[agent_id] = f"{type(exc).__name__} : {exc}"
            else:
                report.agents_ok.append(agent_id)

        # Étape 11 (envoi de l'outbox) et 12 (heartbeat) : étapes 2 et 5 du plan.

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
            report.errors[MAIL_ERROR_KEY] = str(exc)
        else:
            self.store.mark_config_error_notified(error.agent_id, error.fingerprint, now)

    def _process_agent(self, cfg: AgentConfig, defaults: Defaults, *, now: datetime, baseline: bool) -> None:
        agent_id = cfg.agent_id
        if cfg.position.status == "CLOSED":
            log.info("%s : statut CLOSED, agent ignoré", agent_id)
            return
        today = now.astimezone(defaults.schedule.tz).date()
        fired = self.store.fired_rule_ids(agent_id)
        active = cfg.active_rules(fired)
        sources = cfg.enabled_sources()
        log.info("%s : statut %s, %d règle(s) active(s) sur %d, %d source(s) active(s) sur %d, jour %s",
                 agent_id, cfg.position.status, len(active), len(cfg.rules), len(sources), len(cfg.sources),
                 today.isoformat())
        if baseline:
            log.info("%s : baseline demandée, aucun fetcher disponible avant l'étape 3", agent_id)
        # Étapes 3 à 10 du pipeline (cours, sources, tri, analyse, résolution, règles déterministes,
        # dédoublonnage, outbox) : étapes 2 à 4 du plan.


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m watcher.run", description="Veille boursière par agents.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="exécute tous les agents (run quotidien)")
    target.add_argument("--agent", metavar="ID", help="exécute un seul agent (ex. NANO)")
    target.add_argument("--test-mail", action="store_true",
                        help="envoie un mail pour valider la config SMTP (force WATCHER_ENV=test)")
    parser.add_argument("--baseline", action="store_true",
                        help="premier démarrage : marque tous les documents comme vus, sans LLM ni mail")
    return parser


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
    if args.test_mail and args.baseline:
        parser.error("--baseline est incompatible avec --test-mail")

    _load_dotenv()
    try:
        settings = Settings.from_env(**({"env": "test"} if args.test_mail else {}))
    except SettingsError as exc:
        print(f"erreur : {exc}", file=sys.stderr)
        return EXIT_USAGE
    setup_logging(settings.log_dir)
    mailer = Mailer(settings)
    now = datetime.now(UTC)

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

    healthchecks = Healthchecks(settings.healthchecks_url, enabled=settings.healthchecks_enabled)
    with Store.open(settings.db_path) as store:
        report = Runner(settings, store, mailer, healthchecks).run(
            now=now, only=args.agent, baseline=args.baseline
        )
    return EXIT_OK if report.status == "ok" else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
