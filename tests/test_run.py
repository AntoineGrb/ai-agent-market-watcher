from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from watcher import run as run_module
from watcher.config import load_agent, load_defaults
from watcher.engine.pipeline import evaluate_agent
from watcher.models import NewsItem
from watcher.run import EXIT_FAILED, EXIT_OK, EXIT_USAGE, Runner, main
from watcher.settings import Settings
from watcher.store import Store
from tests.conftest import NOW, FakeHealthchecks, FakeMailer, news, rule_match, snapshot


def _runner(settings: Settings, store: Store, mailer: FakeMailer | None = None) -> tuple[Runner, FakeMailer, FakeHealthchecks]:
    mailer = mailer or FakeMailer()
    hc = FakeHealthchecks()
    return Runner(settings, store, mailer, hc, clock=lambda: NOW + timedelta(seconds=30)), mailer, hc


def _unset_nano_entry(edit_agent) -> None:
    edit_agent("nano", lambda d: d["position"].update(entry_price=None))


def test_all_agents_ok(settings: Settings, store: Store) -> None:
    runner, mailer, hc = _runner(settings, store)
    report = runner.run(now=NOW)
    assert report.status == "ok"
    assert sorted(report.agents_ok) == ["NANO", "UBI"]
    assert [c[0] for c in hc.calls] == ["start", "success"]
    assert mailer.sent == []
    row = store.get_run(report.run_id)
    assert (row["status"], row["agents_ok"], row["finished_at"]) == ("ok", 2, "2026-09-25T05:00:30+00:00")


def test_nano_without_entry_price_is_disabled_cleanly(settings: Settings, store: Store, edit_agent) -> None:
    _unset_nano_entry(edit_agent)
    runner, mailer, hc = _runner(settings, store)

    report = runner.run(now=NOW)
    assert report.status == "partial"
    assert report.agents_ok == ["UBI"] and report.agents_failed == ["NANO"]
    assert "entry_price" in report.errors["NANO"]
    assert [c[0] for c in hc.calls] == ["start", "fail"]
    assert "NANO" in hc.calls[-1][1]
    assert len(mailer.sent) == 1
    assert mailer.sent[0].subject.startswith("[CONFIG] NANO")
    assert "Renseigne le prix et la date" in mailer.sent[0].body

    # Même erreur au run suivant : pas de second mail, mais toujours en échec.
    report = runner.run(now=NOW + timedelta(days=1))
    assert report.status == "partial" and len(mailer.sent) == 1
    assert len(store.open_config_errors()) == 1

    # Config corrigée : l'erreur est résolue et l'agent repart.
    edit_agent("nano", lambda d: d["position"].update(entry_price=22.55))
    report = runner.run(now=NOW + timedelta(days=2))
    assert report.status == "ok" and store.open_config_errors() == []


def test_config_error_mail_retried_after_smtp_failure(settings: Settings, store: Store, edit_agent) -> None:
    _unset_nano_entry(edit_agent)
    runner, _, _ = _runner(settings, store, FakeMailer(fail=True))
    report = runner.run(now=NOW)
    assert "_mail" in report.errors
    assert store.open_config_errors()[0].notified_at is None

    runner, mailer, _ = _runner(settings, store)
    runner.run(now=NOW + timedelta(days=1))
    assert len(mailer.sent) == 1
    assert store.open_config_errors()[0].notified_at is not None


def test_baseline_sends_no_mail(settings: Settings, store: Store, edit_agent) -> None:
    _unset_nano_entry(edit_agent)
    runner, mailer, _ = _runner(settings, store)
    runner.run(now=NOW, baseline=True)
    assert mailer.sent == []
    assert store.open_config_errors()[0].notified_at is None   # sera notifiée au premier run normal


def test_agent_exception_rolls_back_and_spares_others(settings: Settings, store: Store,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    original = Runner._process_agent

    def flaky(self, cfg, defaults, *, now, baseline):
        item = NewsItem(id=f"{cfg.agent_id}-1", agent_id=cfg.agent_id, source_name="s", source_type="rss",
                        source_primary=False, url="https://example.com/a", title="t", published_at=now)
        self.store.mark_seen([item], now)
        if cfg.agent_id == "NANO":
            raise RuntimeError("API LLM indisponible")
        return original(self, cfg, defaults, now=now, baseline=baseline)

    monkeypatch.setattr(Runner, "_process_agent", flaky)
    runner, _, hc = _runner(settings, store)
    report = runner.run(now=NOW)
    assert report.status == "partial"
    assert report.errors["NANO"] == "RuntimeError : API LLM indisponible"
    assert store.seen_item_ids("NANO", ["NANO-1"]) == set()       # état non commité
    assert store.seen_item_ids("UBI", ["UBI-1"]) == {"UBI-1"}     # l'autre agent a commité
    assert hc.calls[-1][0] == "fail"


def test_all_agents_failed(settings: Settings, store: Store, edit_agent) -> None:
    _unset_nano_entry(edit_agent)
    edit_agent("ubi", lambda d: d.pop("thesis"))
    runner, _, _ = _runner(settings, store)
    assert runner.run(now=NOW).status == "failed"


def test_invalid_defaults_fail_the_whole_run(settings: Settings, store: Store) -> None:
    (settings.agents_dir / "_defaults.yaml").write_text("models: {}\n", encoding="utf-8")
    runner, _, hc = _runner(settings, store)
    report = runner.run(now=NOW)
    assert report.status == "failed"
    assert "_defaults.yaml invalide" in report.errors["_global"]
    assert hc.calls[-1][0] == "fail"
    assert store.get_run(report.run_id)["status"] == "failed"


def test_closed_agent_is_skipped(settings: Settings, store: Store, edit_agent, caplog) -> None:
    edit_agent("ubi", lambda d: d["position"].update(status="CLOSED"))
    runner, _, _ = _runner(settings, store)
    with caplog.at_level("INFO"):
        report = runner.run(now=NOW)
    assert report.status == "ok"
    assert "UBI : statut CLOSED, agent ignoré" in caplog.text


def test_single_agent_run_is_not_monitored(settings: Settings, store: Store) -> None:
    runner, _, hc = _runner(settings, store)
    report = runner.run(now=NOW, only="nano")
    assert report.agents_ok == ["NANO"]
    assert hc.calls == []


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Isole `main` : pas de .env réel, base et logs dans le dossier temporaire."""
    monkeypatch.setattr(run_module, "_load_dotenv", lambda: None)
    monkeypatch.setattr(run_module.Settings, "from_env",
                        classmethod(lambda cls, environ=None, **kw: settings.model_copy(update=kw)))
    monkeypatch.setattr(run_module, "setup_logging", lambda log_dir: None)
    return settings


def test_cli_requires_a_target(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == EXIT_USAGE


def test_cli_baseline_incompatible_with_test_mail() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--test-mail", "--baseline"])
    assert exc.value.code == EXIT_USAGE


def test_cli_unknown_agent(cli_settings: Settings) -> None:
    assert main(["--agent", "TTWO"]) == EXIT_USAGE
    assert not cli_settings.db_path.exists()


def test_cli_run_all(cli_settings: Settings) -> None:
    assert main(["--all"]) == EXIT_OK
    assert cli_settings.db_path.exists()


def test_cli_run_fails_on_invalid_agent(cli_settings: Settings, edit_agent, monkeypatch: pytest.MonkeyPatch) -> None:
    _unset_nano_entry(edit_agent)

    def smtp_down(self, mail):
        raise run_module.MailError("SMTP injoignable")

    monkeypatch.setattr(run_module.Mailer, "send", smtp_down)
    assert main(["--agent", "NANO"]) == EXIT_FAILED
    with Store.open(cli_settings.db_path) as store:
        assert store.open_config_errors()[0].notified_at is None


def test_cli_test_mail_forces_test_env(cli_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []
    monkeypatch.setattr(run_module.Mailer, "send", lambda self, mail: sent.append((self._settings.env, mail)))
    assert main(["--test-mail"]) == EXIT_OK
    assert sent[0][0] == "test"
    assert "SMTP" in sent[0][1].body


# --------------------------------------------------------------------------- moteur et outbox


def _ubi_holding_expired(edit_agent) -> None:
    """Entrée il y a plus d'un an : le time stop U-T2 (365 jours) est échu."""
    edit_agent("ubi", lambda d: d["position"].update(entry_date="2025-09-01"))


def test_time_rule_alert_is_sent_once(settings: Settings, store: Store, edit_agent) -> None:
    _ubi_holding_expired(edit_agent)
    runner, mailer, _ = _runner(settings, store)
    report = runner.run(now=NOW)
    assert report.status == "ok" and report.alerts_created == 1
    assert store.get_run(report.run_id)["alerts_created"] == 1
    [mail] = mailer.sent
    assert mail.subject.startswith("[HIGH] UBI · Vendre toute la ligne · Durée de détention maximale")
    assert store.pending_events() == []

    report = runner.run(now=NOW + timedelta(days=1))
    assert report.alerts_created == 0 and len(mailer.sent) == 1


def test_unsent_alert_is_retried_next_run(settings: Settings, store: Store, edit_agent) -> None:
    _ubi_holding_expired(edit_agent)
    runner, _, hc = _runner(settings, store, FakeMailer(fail=True))
    report = runner.run(now=NOW)
    assert report.status == "partial" and "alerte UBI" in report.errors["_mail"]
    assert hc.calls[-1][0] == "fail"
    assert len(store.pending_events()) == 1

    runner, mailer, _ = _runner(settings, store)
    report = runner.run(now=NOW + timedelta(days=1))
    assert report.status == "ok" and report.alerts_created == 0
    assert [m.subject.split(" · ")[0] for m in mailer.sent] == ["[HIGH] UBI"]
    assert store.pending_events() == []


def test_baseline_leaves_outbox_untouched(settings: Settings, store: Store, edit_agent) -> None:
    _ubi_holding_expired(edit_agent)
    runner, mailer, _ = _runner(settings, store)
    report = runner.run(now=NOW, baseline=True)
    assert report.alerts_created == 0 and mailer.sent == [] and store.pending_events() == []


def test_closed_agent_cancels_armed_watches(settings: Settings, store: Store, edit_agent) -> None:
    cfg = load_agent(settings.agents_dir / "ubi")
    defaults = load_defaults(settings.agents_dir)
    evaluate_agent(store, cfg, defaults, matches=[rule_match("U-B1", offer_price=11.0)],
                   items={"a": news("a")}, price=snapshot(10.0, 9.5), now=NOW, fired=set())
    assert len(store.active_armed_watches("UBI")) == 1

    edit_agent("ubi", lambda d: d["position"].update(status="CLOSED"))
    runner, _, _ = _runner(settings, store)
    runner.run(now=NOW + timedelta(days=1))
    assert store.active_armed_watches("UBI") == []
    status = store._conn.execute("SELECT status FROM armed_watches").fetchone()["status"]
    assert status == "cancelled"
