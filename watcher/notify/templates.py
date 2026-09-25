"""Gabarits des mails (cadrage §9), en texte brut.

Étape 1 : mail de test et erreur de configuration. Alerte, digest et heartbeat arrivent à l'étape 5.
"""

from __future__ import annotations

from datetime import datetime

from watcher.notify.mailer import Mail


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
