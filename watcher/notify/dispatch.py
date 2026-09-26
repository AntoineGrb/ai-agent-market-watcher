"""Envoi de l'outbox (cadrage §3.1 étape 11, §6.8).

Toutes les alertes `sent_at IS NULL` partent, y compris celles des runs précédents : 1 mail par agent pour ses
alertes CRITICAL / HIGH, 1 digest global pour les INFO. `sent_at` n'est renseigné qu'après succès SMTP ; un mail
en échec laisse ses alertes dans l'outbox pour le run suivant, sans bloquer les autres mails.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from watcher.config import Action, Severity
from watcher.engine.priority import sort_key
from watcher.notify import templates
from watcher.notify.mailer import Mail, Mailer, MailError
from watcher.store import EventRow, Store

log = logging.getLogger(__name__)


@dataclass
class DispatchReport:
    mails_sent: int = 0
    alerts_sent: int = 0
    errors: list[str] = field(default_factory=list)


def _sorted(rows: Sequence[EventRow], priority: Sequence[Action]) -> list[EventRow]:
    return sorted(rows, key=lambda r: (sort_key(r.alert, priority), r.id))


def send_outbox(
    store: Store,
    mailer: Mailer,
    *,
    priority: Sequence[Action],
    now: datetime,
    digest_day: date,
) -> DispatchReport:
    """Envoie les alertes en attente. `digest_day` : date (locale) affichée dans l'objet du digest."""
    report = DispatchReport()
    pending = store.pending_events()
    if not pending:
        log.info("outbox vide, aucun mail d'alerte")
        return report

    by_agent: dict[str, list[EventRow]] = {}
    digest: dict[str, list[EventRow]] = {}
    for row in pending:
        target = digest if row.alert.severity is Severity.INFO else by_agent
        target.setdefault(row.agent_id, []).append(row)

    batches: list[tuple[str, list[EventRow], Mail]] = []
    for agent_id in sorted(by_agent):
        rows = _sorted(by_agent[agent_id], priority)
        batches.append((f"alerte {agent_id}", rows, templates.alert_mail(agent_id, [r.alert for r in rows])))
    if digest:
        grouped = {agent_id: _sorted(digest[agent_id], priority) for agent_id in sorted(digest)}
        rows = [r for agent_rows in grouped.values() for r in agent_rows]
        mail = templates.digest_mail(digest_day,
                                     {a: [r.alert for r in agent_rows] for a, agent_rows in grouped.items()})
        batches.append(("digest", rows, mail))

    for label, rows, mail in batches:
        try:
            mailer.send(mail)
        except MailError as exc:
            log.error("mail %s non envoyé, %d alerte(s) conservée(s) dans l'outbox : %s", label, len(rows), exc)
            report.errors.append(f"{label} : {exc}")
            continue
        store.mark_sent([r.id for r in rows], now)
        report.mails_sent += 1
        report.alerts_sent += len(rows)
    return report
