from __future__ import annotations

from pathlib import Path

import pytest

from watcher.settings import PROJECT_ROOT, Settings, SettingsError


def test_from_env_defaults() -> None:
    s = Settings.from_env({})
    assert s.env == "prod"
    assert s.smtp_host == "smtp.gmail.com" and s.smtp_port == 587
    assert s.base_dir == PROJECT_ROOT
    assert s.subject_prefix == ""


def test_empty_variables_are_ignored() -> None:
    s = Settings.from_env({"WATCHER_ENV": "test", "SMTP_PORT": " ", "WATCHER_MODEL_TRIAGE": "", "MAIL_TO": "a@b.c"})
    assert s.env == "test" and s.smtp_port == 587 and s.model_triage is None and s.mail_to == "a@b.c"


def test_secrets_are_masked() -> None:
    s = Settings.from_env({"SMTP_APP_PASSWORD": "abcd efgh", "ANTHROPIC_API_KEY": "sk-ant-xxx"})
    assert "abcd" not in repr(s) and "sk-ant" not in repr(s)
    assert s.smtp_app_password.get_secret_value() == "abcd efgh"


@pytest.mark.parametrize("environ", [{"WATCHER_ENV": "staging"}, {"SMTP_PORT": "abc"}, {"SMTP_PORT": "70000"}])
def test_invalid_values(environ: dict[str, str]) -> None:
    with pytest.raises(SettingsError, match="variables d'environnement invalides"):
        Settings.from_env(environ)


def test_overrides_win_over_environment(tmp_path: Path) -> None:
    s = Settings.from_env({"WATCHER_ENV": "prod"}, env="test", base_dir=tmp_path)
    assert s.env == "test"
    assert s.db_path == tmp_path / "data" / "test" / "watcher.sqlite"
    assert s.log_dir == tmp_path / "data" / "test" / "logs"
    assert s.agents_dir == tmp_path / "agents"


def test_environment_behaviours() -> None:
    prod = Settings.from_env({"HEALTHCHECKS_URL": "https://hc-ping.com/uuid"})
    test = Settings.from_env({"WATCHER_ENV": "test", "HEALTHCHECKS_URL": "https://hc-ping.com/uuid"})
    assert prod.healthchecks_enabled and not test.healthchecks_enabled
    assert test.subject_prefix == "[TEST] "
    assert prod.db_path != test.db_path
    assert not Settings.from_env({}).healthchecks_enabled
