from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from watcher.notify.mailer import Mail, MailError
from watcher.settings import PROJECT_ROOT, Settings
from watcher.store import Store

REPO_AGENTS_DIR = PROJECT_ROOT / "agents"
NOW = datetime(2026, 9, 25, 5, 0, tzinfo=UTC)   # 07:00 Europe/Paris


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    """Copie des configs réelles du dépôt dans un dossier temporaire, modifiable par chaque test."""
    shutil.copytree(REPO_AGENTS_DIR, tmp_path / "agents")
    return tmp_path


@pytest.fixture
def settings(base_dir: Path) -> Settings:
    return Settings(env="test", base_dir=base_dir, smtp_user="bot@example.com",
                    smtp_app_password="secret", mail_to="me@example.com")


@pytest.fixture
def store() -> Store:
    s = Store.open(":memory:")
    yield s
    s.close()


EditFn = Callable[[dict[str, Any]], None]


@pytest.fixture
def edit_agent(base_dir: Path) -> Callable[[str, EditFn], None]:
    """Modifie `agents/<id>/config.yaml` via une fonction qui mute le dict chargé."""

    def _edit(agent_dir: str, mutate: EditFn) -> None:
        path = base_dir / "agents" / agent_dir / "config.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    return _edit


class FakeMailer:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[Mail] = []
        self.fail = fail

    def send(self, mail: Mail) -> None:
        if self.fail:
            raise MailError("SMTP injoignable")
        self.sent.append(mail)


class FakeHealthchecks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def start(self) -> None:
        self.calls.append(("start", ""))

    def success(self, body: str = "") -> None:
        self.calls.append(("success", body))

    def fail(self, body: str) -> None:
        self.calls.append(("fail", body))
