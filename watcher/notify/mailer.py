"""Envoi SMTP (cadrage §9) : texte brut UTF-8, Gmail en STARTTLS sur le port 587.

En environnement de test, l'objet est préfixé `[TEST]`. Pas de dry-run : les mails partent réellement.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from watcher.settings import Settings

log = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 30.0


class MailError(Exception):
    """Mail non envoyé (SMTP non configuré, refusé ou injoignable)."""


@dataclass(frozen=True)
class Mail:
    subject: str
    body: str


SmtpFactory = Callable[[str, int, float], smtplib.SMTP]


def _default_smtp(host: str, port: int, timeout: float) -> smtplib.SMTP:
    return smtplib.SMTP(host, port, timeout=timeout)


class Mailer:
    def __init__(self, settings: Settings, smtp_factory: SmtpFactory = _default_smtp) -> None:
        self._settings = settings
        self._smtp_factory = smtp_factory

    def _recipients(self) -> list[str]:
        return [addr.strip() for addr in (self._settings.mail_to or "").split(",") if addr.strip()]

    def check_configured(self) -> tuple[str, str, list[str]]:
        """Retourne (utilisateur, mot de passe, destinataires) ou lève MailError si un paramètre manque."""
        s = self._settings
        user = s.smtp_user or ""
        password = s.smtp_app_password.get_secret_value() if s.smtp_app_password else ""
        recipients = self._recipients()
        missing = [name for name, value in (("SMTP_USER", user), ("SMTP_APP_PASSWORD", password),
                                            ("MAIL_TO", recipients)) if not value]
        if missing:
            raise MailError(f"SMTP non configuré : {', '.join(missing)} manquant(s)")
        return user, password, recipients

    def build(self, mail: Mail) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"{self._settings.subject_prefix}{mail.subject}"
        msg["From"] = self._settings.smtp_user or ""
        msg["To"] = ", ".join(self._recipients())
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="watcher.local")
        msg.set_content(mail.body, charset="utf-8")
        return msg

    def send(self, mail: Mail) -> None:
        user, password, _ = self.check_configured()
        msg = self.build(mail)
        try:
            with self._smtp_factory(self._settings.smtp_host, self._settings.smtp_port, SMTP_TIMEOUT_SECONDS) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(user, password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"envoi SMTP en échec : {exc}") from exc
        log.info("mail envoyé : %s", msg["Subject"])
