from __future__ import annotations

import smtplib
from email.message import EmailMessage

import pytest

from watcher.notify import templates
from watcher.notify.mailer import Mail, Mailer, MailError
from watcher.settings import Settings
from tests.conftest import NOW


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float, fail_on: str | None = None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.fail_on = fail_on
        self.calls: list[str] = []
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self, context=None) -> None:
        assert context is not None
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        if self.fail_on == "login":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeSMTP.instances.clear()


def _settings(**kw) -> Settings:
    base = dict(env="test", smtp_user="bot@gmail.com", smtp_app_password="app pwd", mail_to="me@x.fr, other@x.fr")
    return Settings(**{**base, **kw})


def test_send_plain_text_with_test_prefix() -> None:
    mailer = Mailer(_settings(), smtp_factory=FakeSMTP)
    mailer.send(Mail(subject="NANO · Vendre", body="Dilution calculée : 22,0 %"))
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 587)
    assert smtp.calls == ["starttls", "login:bot@gmail.com:app pwd", "quit"]
    msg = smtp.sent[0]
    assert msg["Subject"] == "[TEST] NANO · Vendre"
    assert msg["To"] == "me@x.fr, other@x.fr"
    assert msg.get_content_type() == "text/plain"
    assert msg.get_content_charset() == "utf-8"
    assert "22,0 %" in msg.get_content()


def test_no_prefix_in_prod() -> None:
    msg = Mailer(_settings(env="prod"), smtp_factory=FakeSMTP).build(Mail("Objet", "corps"))
    assert msg["Subject"] == "Objet"


def test_missing_configuration() -> None:
    mailer = Mailer(_settings(smtp_app_password=None, mail_to=" , "), smtp_factory=FakeSMTP)
    with pytest.raises(MailError, match="SMTP_APP_PASSWORD, MAIL_TO manquant"):
        mailer.send(Mail("a", "b"))
    assert FakeSMTP.instances == []


def test_smtp_failure_is_wrapped() -> None:
    mailer = Mailer(_settings(), smtp_factory=lambda h, p, t: FakeSMTP(h, p, t, fail_on="login"))
    with pytest.raises(MailError, match="envoi SMTP en échec"):
        mailer.send(Mail("a", "b"))


def test_network_failure_is_wrapped() -> None:
    def unreachable(host, port, timeout):
        raise ConnectionRefusedError("refused")

    with pytest.raises(MailError, match="refused"):
        Mailer(_settings(), smtp_factory=unreachable).send(Mail("a", "b"))


def test_templates() -> None:
    check = templates.smtp_check_mail("test", NOW)
    assert "test" in check.body and "25/09/2026" in check.body
    err = templates.config_error_mail("NANO", "position : status OWNED sans entry_price / entry_date")
    assert err.subject.startswith("[CONFIG] NANO")
    assert "agents/nano/config.yaml" in err.body
    assert "entry_price" in err.body
