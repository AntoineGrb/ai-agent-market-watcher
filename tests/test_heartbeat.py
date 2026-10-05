from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from pydantic_ai.usage import RunUsage

from watcher import run as run_module
from watcher.config import Action, Arm, Condition, Defaults, Severity
from watcher.llm import LlmUsage, TokenBudget
from watcher.models import Alert
from watcher.notify import templates
from watcher.notify.heartbeat import build_heartbeat
from watcher.run import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Runner, main, run_scope
from watcher.settings import Settings
from watcher.store import MIGRATIONS, RunTotals, Store
from tests.conftest import NOW, FakeHealthchecks, FakeMailer, repo_agent

MONDAY = NOW + timedelta(days=3)          # lundi 28/09/2026 07:00 Europe/Paris
MONDAY_DATE = date(2026, 9, 28)


def _run(store: Store, started: datetime, status: str = "ok", scope: str = "all", **totals) -> int:
    run_id = store.start_run("test", started, scope)
    store.finish_run(run_id, started + timedelta(seconds=75), RunTotals(status=status, **totals))
    return run_id


def _alert(rule_id: str, severity: Severity = Severity.HIGH, agent_id: str = "UBI") -> Alert:
    return Alert(agent_id=agent_id, rule_id=rule_id, source_rule_id=rule_id, origin="event",
                 action=Action.RECO_UNCLEAR, severity=severity, headline="h", rationale="r",
                 event_date=date(2026, 9, 24))


# --------------------------------------------------------------------------- collecte


def test_window_runs_and_tokens(store: Store, defaults: Defaults) -> None:
    _run(store, NOW - timedelta(days=4))                                          # lundi 21/09 : hors fenêtre
    _run(store, NOW - timedelta(days=3), input_tokens=10, output_tokens=1)          # mardi 22/09
    _run(store, NOW - timedelta(days=2), scope="agent NANO")                        # run manuel : non compté
    _run(store, NOW - timedelta(days=1), status="partial",
         errors={"NANO": "LlmError : API indisponible", "_warnings": {"UBI": ["source X : 503"]}})
    _run(store, NOW, input_tokens=1_100_000, output_tokens=40_000,
         errors={"_warnings": {"UBI": ["source X : 503"]}},
         usage_by_model={"anthropic:claude-haiku-4-5": [1_000_000, 20_000],
                         "anthropic:claude-sonnet-5": [100_000, 20_000], "google-gla:gemini": [5, 5]})

    hb = build_heartbeat(store, defaults, configs={}, invalid={}, now=MONDAY)
    assert (hb.start, hb.today) == (date(2026, 9, 22), MONDAY_DATE)
    assert len(hb.runs) == 4
    assert hb.days_ok == 2
    assert hb.days_without_ok == [date(2026, 9, d) for d in (23, 24, 26, 27, 28)]
    assert (hb.input_tokens, hb.output_tokens) == (1_100_010, 40_001)
    # Haiku : 1 M × 1 $ + 20 k × 5 $/M ; Sonnet : 100 k × 2 $/M + 20 k × 10 $/M.
    assert hb.cost_usd == pytest.approx(1.0 + 0.1 + 0.2 + 0.2)
    assert hb.unpriced_models == ["google-gla:gemini"]
    assert [(o.key, o.message, o.count) for o in hb.warnings] == [("UBI", "source X : 503", 2)]
    assert [(o.key, o.count) for o in hb.errors] == [("NANO", 1)]


def test_agents_watches_and_attention(store: Store, defaults: Defaults) -> None:
    ubi = repo_agent("ubi", entry_date=MONDAY_DATE - timedelta(days=350),       # U-T2 dans 15 jours
                     shares_outstanding_as_of=date(2026, 6, 30))
    ubi = ubi.model_copy(update={"context_updated_at": date(2026, 5, 1)})
    nano = repo_agent("nano", shares_outstanding=None)
    closed = repo_agent("ubi", status="CLOSED").model_copy(
        update={"agent_id": "OLD", "context_updated_at": date(2020, 1, 1)})
    event_id = store.insert_event(_alert("U-B1"), "event:U-B1", NOW)
    arm = Arm(id="U-B1-EXIT", when=Condition(metric="price_vs_figure", figure="offer_price", op=">=", value=0.98),
              action=Action.RECO_SELL_ALL, severity=Severity.CRITICAL)
    store.create_armed_watch("UBI", arm, 11.0, event_id, NOW)
    store.record_config_error("TTWO", "h", "position : status OWNED sans entry_price", NOW)

    hb = build_heartbeat(store, defaults, configs={"UBI": ubi, "NANO": nano, "OLD": closed},
                         invalid={"TTWO": "position : status OWNED sans entry_price"}, now=MONDAY)
    states = {a.agent_id: (a.state, a.detail) for a in hb.agents}
    assert states["UBI"][0] == "OWNED" and "entrée à 5,33 EUR" in states["UBI"][1]
    assert states["TTWO"] == ("désactivé (config invalide)", None)
    assert states["OLD"] == ("CLOSED", None)
    assert [w.arm_id for w in hb.watches] == ["U-B1-EXIT"]
    assert [e.agent_id for e in hb.config_errors] == ["TTWO"]

    attention = "\n".join(hb.attention)
    assert "UBI : contexte daté du 01/05/2026 (150 jours)" in attention
    assert "UBI : shares_outstanding au 30/06/2026 (90 jours)" in attention
    assert "UBI : source « Ubisoft Investor Center » désactivée" in attention
    assert "UBI : time stop U-T2 le 13/10/2026 (dans 15 jours)" in attention
    assert "NANO : shares_outstanding non renseigné" in attention
    assert "OLD" not in attention                                   # agent CLOSED : rien à surveiller
    assert "U-T1" not in attention                                  # règle WATCH, inactive en OWNED


def test_past_time_stop_and_undated_shares(store: Store, defaults: Defaults) -> None:
    """Un time stop échu a déjà produit son alerte : il ne figure plus dans les points d'attention."""
    ubi = repo_agent("ubi", entry_date=MONDAY_DATE - timedelta(days=400), shares_outstanding_as_of=None)
    attention = build_heartbeat(store, defaults, configs={"UBI": ubi}, invalid={}, now=MONDAY).attention
    assert not any("U-T2" in item for item in attention)
    assert any("shares_outstanding sans date" in item for item in attention)


# --------------------------------------------------------------------------- rendu


def test_heartbeat_mail(store: Store, defaults: Defaults) -> None:
    _run(store, NOW, alerts_created=2, input_tokens=1000, output_tokens=200,
         usage_by_model={"anthropic:claude-sonnet-5": [1000, 200]})
    ids = [store.insert_event(_alert("U-S2", Severity.CRITICAL), "a", NOW),
           store.insert_event(_alert("N-N1", Severity.INFO, agent_id="NANO"), "b", NOW)]
    store.mark_sent(ids, NOW)
    store.record_config_error("NANO", "h", "position : status OWNED sans entry_price", NOW)
    hb = build_heartbeat(store, defaults, configs={"UBI": repo_agent("ubi")},
                         invalid={"NANO": "position : status OWNED sans entry_price"}, now=MONDAY)
    mail = templates.heartbeat_mail(hb, tz=defaults.schedule.tz)

    assert mail.subject == "[HEARTBEAT] Semaine du 22/09 — 1/7 runs OK, 2 alertes"
    body = mail.body
    assert "- ven. 25/09 07:00 · all · ok · 1 min 15 s · 2 alertes créées · 1 200 tokens" in body
    assert "Jours sans run quotidien réussi : mar. 22/09" in body
    assert "- NANO : 1 (1 INFO) : N-N1 (24/09)" in body
    assert "- UBI : 1 (1 CRITICAL) : U-S2 (24/09)" in body
    assert "- NANO : désactivé (config invalide)" in body
    assert "Erreurs de configuration en cours (agents désactivés) :\n- NANO (depuis le 2026-09-25)" in body
    assert "Sources et cours : avertissements :\n- aucune" in body
    assert "Surveillances armées actives :\n- aucune" in body
    assert "Coût estimé : 0,00 $" in body
    assert "source « Ubisoft Investor Center » désactivée" in body
    assert body.rstrip().endswith(templates.HEARTBEAT_FOOTER)


def test_heartbeat_mail_truncates_long_lists(store: Store, defaults: Defaults) -> None:
    warnings = {"UBI": [f"source {i} : erreur" for i in range(templates.MAX_OCCURRENCES + 3)]}
    _run(store, NOW, status="partial", errors={"_warnings": warnings, "_mail": "SMTP"})
    hb = build_heartbeat(store, defaults, configs={}, invalid={}, now=MONDAY)
    body = templates.heartbeat_mail(hb, tz=defaults.schedule.tz).body
    assert "… et 3 autre(s)" in body
    assert "Erreurs des runs :\n- _mail : SMTP" in body
    assert "Points d'attention :\n- aucun" in body


def test_unfinished_run_and_empty_week(store: Store, defaults: Defaults) -> None:
    store.start_run("test", NOW)                                     # run interrompu : jamais terminé
    hb = build_heartbeat(store, defaults, configs={}, invalid={}, now=MONDAY)
    body = templates.heartbeat_mail(hb, tz=defaults.schedule.tz).body
    assert "running · durée inconnue" in body
    hb = build_heartbeat(Store.open(":memory:"), defaults, configs={}, invalid={}, now=MONDAY)
    mail = templates.heartbeat_mail(hb, tz=defaults.schedule.tz)
    assert mail.subject.endswith("0/7 runs OK, 0 alerte")
    assert "- aucun run enregistré" in mail.body


# --------------------------------------------------------------------------- déclenchement


def _runner(settings: Settings, store: Store, mailer: FakeMailer | None = None, when: datetime = MONDAY):
    mailer = mailer or FakeMailer()
    hc = FakeHealthchecks()
    return Runner(settings, store, mailer, hc, clock=lambda: when + timedelta(seconds=30)), mailer, hc


def _heartbeats(mailer: FakeMailer) -> list:
    return [m for m in mailer.sent if m.subject.startswith("[HEARTBEAT]")]


def test_heartbeat_sent_once_on_monday_after_the_run(settings: Settings, store: Store) -> None:
    runner, mailer, hc = _runner(settings, store)
    report = runner.run(now=MONDAY)
    assert report.status == "ok"
    [mail] = _heartbeats(mailer)
    assert "1/7 runs OK" in mail.subject                          # le run du jour y figure déjà
    assert "- lun. 28/09 07:00 · all · ok · 30 s" in mail.body
    assert store.heartbeat_sent_on(MONDAY_DATE)
    assert hc.calls[-1][0] == "success"

    runner.run(now=MONDAY + timedelta(hours=2))                    # relance manuelle le même jour
    assert len(_heartbeats(mailer)) == 1


@pytest.mark.parametrize("kwargs", [{"only": "UBI"}, {"baseline": True}])
def test_no_heartbeat_for_manual_or_baseline_runs(settings: Settings, store: Store, kwargs) -> None:
    runner, mailer, _ = _runner(settings, store)
    runner.run(now=MONDAY, **kwargs)
    assert _heartbeats(mailer) == []


def test_no_heartbeat_on_other_days(settings: Settings, store: Store) -> None:
    runner, mailer, _ = _runner(settings, store, when=NOW)
    runner.run(now=NOW)
    assert _heartbeats(mailer) == [] and not store.heartbeat_sent_on(NOW.date())


def test_heartbeat_smtp_failure_degrades_the_run(settings: Settings, store: Store) -> None:
    runner, _, hc = _runner(settings, store, FakeMailer(fail=True))
    report = runner.run(now=MONDAY)
    assert report.status == "partial" and "heartbeat" in report.errors["_mail"]
    assert store.get_run(report.run_id)["status"] == "partial"
    assert hc.calls[-1][0] == "fail"
    assert not store.heartbeat_sent_on(MONDAY_DATE)

    runner, mailer, _ = _runner(settings, store)                   # nouvelle tentative le même jour
    runner.run(now=MONDAY + timedelta(hours=1))
    assert len(_heartbeats(mailer)) == 1


def test_heartbeat_now_without_running_agents(settings: Settings, store: Store) -> None:
    runner, mailer, hc = _runner(settings, store, when=NOW)
    assert runner.heartbeat_now(now=NOW) is True
    [mail] = mailer.sent
    assert mail.subject.startswith("[HEARTBEAT] Semaine du 19/09")
    assert hc.calls == [] and store.runs_since(NOW - timedelta(days=7)) == []


def test_cli_heartbeat(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_module, "_load_dotenv", lambda: None)
    monkeypatch.setattr(run_module.Settings, "from_env",
                        classmethod(lambda cls, environ=None, **kw: settings.model_copy(update=kw)))
    monkeypatch.setattr(run_module, "setup_logging", lambda log_dir: None)
    monkeypatch.setattr(run_module, "register_builtin_fetchers", lambda settings, client: None)
    monkeypatch.setattr(run_module, "build_price_service", lambda client: None)
    sent = []
    monkeypatch.setattr(run_module.Mailer, "send", lambda self, mail: sent.append(mail))
    assert main(["--heartbeat"]) == EXIT_OK
    assert sent[0].subject.startswith("[HEARTBEAT]")

    def smtp_down(self, mail):
        raise run_module.MailError("SMTP injoignable")

    monkeypatch.setattr(run_module.Mailer, "send", smtp_down)
    assert main(["--heartbeat"]) == EXIT_FAILED
    (settings.agents_dir / "_defaults.yaml").write_text("models: {}", encoding="utf-8")
    assert main(["--heartbeat"]) == EXIT_FAILED


def test_cli_heartbeat_incompatible_with_baseline() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--heartbeat", "--baseline"])
    assert exc.value.code == EXIT_USAGE


# --------------------------------------------------------------------------- portée des runs, tokens, migration


def test_run_scope() -> None:
    assert run_scope(only=None, baseline=False, injected=False) == "all"
    assert run_scope(only=None, baseline=True, injected=False) == "baseline"
    assert run_scope(only="nano", baseline=False, injected=False) == "agent NANO"
    assert run_scope(only="nano", baseline=True, injected=False) == "baseline NANO"
    assert run_scope(only="NANO", baseline=False, injected=True) == "inject NANO"


def test_scope_is_stored(settings: Settings, store: Store) -> None:
    runner, _, _ = _runner(settings, store, when=NOW)
    report = runner.run(now=NOW, only="ubi")
    assert store.get_run(report.run_id)["scope"] == "agent UBI"


def test_usage_by_model() -> None:
    budget = TokenBudget(1_000)
    with budget.call("tri", "anthropic:claude-haiku-4-5") as (usage, _):
        usage.incr(RunUsage(input_tokens=100, output_tokens=10, requests=1))
    with budget.call("analyse", "anthropic:claude-sonnet-5") as (usage, _):
        usage.incr(RunUsage(input_tokens=50, output_tokens=5, requests=1))
    assert budget.used.by_model == {"anthropic:claude-haiku-4-5": [100, 10], "anthropic:claude-sonnet-5": [50, 5]}

    total = LlmUsage()
    total.add(budget.used)
    total.add(budget.used)
    assert total.total_tokens == 330 and total.by_model["anthropic:claude-sonnet-5"] == [100, 10]
    total.add(RunUsage(input_tokens=1))                              # sans modèle : totaux seulement
    assert total.input_tokens == 301 and total.by_model["anthropic:claude-haiku-4-5"] == [200, 20]


def test_migration_from_v1_keeps_runs(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(MIGRATIONS[1] + """
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1);
        INSERT INTO runs (env, started_at, status) VALUES ('prod', '2026-09-24T05:00:00+00:00', 'ok');
    """)
    conn.close()
    with Store.open(path) as store:
        assert store.schema_version() == 2
        [run] = store.runs_since(NOW - timedelta(days=7))
        assert (run.scope, run.usage_by_model, run.duration_s) == ("all", {}, None)
        store.record_heartbeat(MONDAY_DATE, MONDAY)
        store.record_heartbeat(MONDAY_DATE, MONDAY + timedelta(hours=1))   # idempotent
        assert store.heartbeat_sent_on(MONDAY_DATE)
