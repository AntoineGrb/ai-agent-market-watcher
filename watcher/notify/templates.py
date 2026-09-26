"""Gabarits des mails (cadrage §9), en texte brut.

Étape 1 : mail de test et erreur de configuration. Étape 2 : version minimale de l'alerte et du digest (pour
l'outbox) ; leur mise en forme définitive (texte du déclencheur, seuils...) et le heartbeat arrivent à l'étape 5.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime

from watcher.config import Action
from watcher.models import Alert, Evidence
from watcher.notify.mailer import Mail

ACTION_LABELS: dict[Action, str] = {
    Action.RECO_SELL_ALL: "Je te recommande de vendre toute la ligne",
    Action.RECO_SELL_HALF: "Je te recommande de vendre la moitié (récupérer la mise)",
    Action.RECO_HOLD: "Rien à faire, information pour le suivi",
    Action.RECO_BUY: "Les conditions d'entrée sont réunies, l'achat est envisageable",
    Action.RECO_NO_ENTRY: "Les conditions d'entrée ne sont pas réunies, ne pas acheter",
    Action.RECO_UNCLEAR: "Situation ambiguë : pas de recommandation, lis la source toi-même",
    Action.IGNORE: "Ignoré",
}

SUBJECT_LABELS: dict[Action, str] = {
    Action.RECO_SELL_ALL: "Vendre toute la ligne",
    Action.RECO_SELL_HALF: "Vendre la moitié",
    Action.RECO_HOLD: "Information de suivi",
    Action.RECO_BUY: "Achat envisageable",
    Action.RECO_NO_ENTRY: "Ne pas acheter",
    Action.RECO_UNCLEAR: "Situation ambiguë",
    Action.IGNORE: "Ignoré",
}

ALERT_FOOTER = "Recommandation automatique issue de tes règles. Aucune opération n'a été exécutée."


def smtp_check_mail(env: str, now: datetime) -> Mail:
    return Mail(
        subject="Watcher · mail de test",
        body=(
            "Ce mail confirme que la configuration SMTP du watcher fonctionne.\n\n"
            f"Environnement : {env}\n"
            f"Envoyé le : {now.strftime('%d/%m/%Y à %H:%M %Z')}\n"
        ),
    )


def config_error_mail(agent_id: str, message: str) -> Mail:
    return Mail(
        subject=f"[CONFIG] {agent_id} · agent désactivé : configuration invalide",
        body=(
            f"L'agent {agent_id} est désactivé tant que sa configuration est invalide.\n"
            "Les autres agents continuent de tourner normalement.\n\n"
            "Erreur :\n"
            f"{message}\n\n"
            f"Corrige agents/{agent_id.lower()}/config.yaml : l'agent reprendra au run suivant.\n"
            "Ce mail n'est envoyé qu'une fois par erreur ; un rappel figure dans chaque heartbeat tant qu'elle persiste.\n"
        ),
    )


# --------------------------------------------------------------------------- alertes


def _one_line(text: str) -> str:
    """Un objet de mail ne doit contenir aucun saut de ligne (le headline vient du LLM)."""
    return " ".join(text.split())


def _num(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _evidence_tag(e: Evidence) -> str:
    return "[source primaire]" if e.primary else "[RUMEUR — presse]"


def _alert_block(alert: Alert) -> str:
    rule = alert.rule_id if alert.rule_id == alert.source_rule_id else f"{alert.rule_id} (règle {alert.source_rule_id})"
    lines = [
        f"{ACTION_LABELS[alert.action]} [{alert.severity.value}]",
        f"Règle : {rule}",
        alert.headline,
        "",
        alert.rationale,
    ]
    if alert.figures:
        lines.append("Chiffres extraits : " + ", ".join(f"{k} = {v:g}" for k, v in alert.figures.items()))
    if alert.metrics:
        lines.append("Métriques calculées : " + ", ".join(f"{k} = {_num(v, 3)}" for k, v in alert.metrics.items()))
    if alert.price is not None:
        p = alert.price
        move = f", variation J-1 : {_num(p.daily_move_pct, 1)} %" if p.daily_move_pct is not None else ""
        lines.append(f"Cours : clôture du {p.last_close_date:%d/%m/%Y} à {_num(p.last_close)}{move}")
    if alert.evidence:
        lines.append("Preuves :")
        lines += [f"- {e.url} {_evidence_tag(e)} ({e.source_name})" for e in alert.evidence]
    if alert.downgrade_reason:
        lines.append(f"Attention : {alert.downgrade_reason}")
    if alert.confidence is not None:
        lines.append(f"Confiance du LLM : {_num(alert.confidence)}")
    return "\n".join(lines)


def _with_footer(body: str) -> str:
    return f"{body}\n\n-- \n{ALERT_FOOTER}\n"


def alert_mail(agent_id: str, alerts: Sequence[Alert]) -> Mail:
    """Mail d'alerte d'un agent. `alerts` est déjà trié (§6.6) : la première donne l'objet."""
    if not alerts:
        raise ValueError("alert_mail : aucune alerte")
    lead = alerts[0]
    subject = f"[{lead.severity.value}] {agent_id} · {SUBJECT_LABELS[lead.action]} · {_one_line(lead.headline)}"
    separator = "\n\n" + "-" * 60 + "\n\n"
    return Mail(subject=subject, body=_with_footer(separator.join(_alert_block(a) for a in alerts)))


def _main_link(alert: Alert) -> str:
    primary = [e for e in alert.evidence if e.primary]
    chosen = (primary or alert.evidence or [None])[0]
    return str(chosen.url) if chosen is not None else "(pas de lien)"


def digest_mail(day: date, alerts_by_agent: Mapping[str, Sequence[Alert]]) -> Mail:
    """Digest INFO global : une section par agent, une ligne par alerte."""
    total = sum(len(alerts) for alerts in alerts_by_agent.values())
    if total == 0:
        raise ValueError("digest_mail : aucune alerte")
    sections = []
    for agent_id, alerts in alerts_by_agent.items():
        lines = [agent_id]
        lines += [
            f"- {a.rule_id} · {_one_line(a.headline)} · {_main_link(a)}{' [RUMEUR]' if a.is_rumor else ''}"
            for a in alerts
        ]
        sections.append("\n".join(lines))
    plural = "s" if total > 1 else ""
    return Mail(
        subject=f"[INFO] Veille du {day:%d/%m} — {total} élément{plural}",
        body=_with_footer("\n\n".join(sections)),
    )
