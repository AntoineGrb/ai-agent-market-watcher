"""Gabarits des mails (cadrage §9), en texte brut UTF-8.

- mail de test SMTP ;
- erreur de configuration (§9.4) ;
- alerte d'un agent (§9.1), une par agent et par run pour ses alertes CRITICAL / HIGH ;
- digest INFO global (§9.2) ;
- heartbeat hebdomadaire (§9.3).

Les gabarits ne font aucun calcul métier : ils affichent ce que le moteur a figé dans l'`Alert` (règle, seuils
évalués, cours, preuves). Le préfixe `[TEST]` est ajouté par le mailer.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, tzinfo

from watcher.config import Action, Severity
from watcher.engine.describe import OP_TEXT, metric_text, num
from watcher.models import Alert, ConditionCheck, Evidence
from watcher.notify.heartbeat import HeartbeatData, Occurrence
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

ORIGIN_LABELS = {
    "event": "actualité",
    "price": "règle de cours",
    "arm": "surveillance armée",
    "time": "échéance",
    "anomaly": "anomalie de cours",
}

ALERT_FOOTER = "Recommandation automatique issue de tes règles. Aucune opération n'a été exécutée."
HEARTBEAT_FOOTER = "Heartbeat hebdomadaire du watcher. Aucune opération n'a été exécutée."
SEPARATOR = "\n\n" + "-" * 60 + "\n\n"
WEEKDAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
MAX_OCCURRENCES = 15     # lignes d'erreurs / avertissements affichées par section du heartbeat
RATIO_METRICS = ("price_vs_entry", "figure_vs_prev_close", "price_vs_figure")
PERCENT_METRICS = ("dilution_pct", "daily_move_pct")


# --------------------------------------------------------------------------- utilitaires


def _one_line(text: str) -> str:
    """Un objet de mail ne doit contenir aucun saut de ligne (le headline vient du LLM)."""
    return " ".join(text.split())


def _signed_pct(value: float) -> str:
    return f"{'+' if value > 0 else ''}{num(value, 1)} %"


def _figure(value: float) -> str:
    """Chiffre brut : entiers avec séparateur de milliers (`40 000 000`), sinon décimales à la française."""
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", " ")
    return num(value)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n > 1 else ''}"


def _with_footer(body: str, footer: str = ALERT_FOOTER) -> str:
    return f"{body}\n\n-- \n{footer}\n"


# --------------------------------------------------------------------------- mails simples


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
            f"Corrige agents/{agent_id.lower()}/config.yaml (ou prompt.md) : l'agent reprendra au run suivant.\n"
            "Ce mail n'est envoyé qu'une fois par erreur ; un rappel figure dans chaque heartbeat tant qu'elle persiste.\n"
        ),
    )


# --------------------------------------------------------------------------- alertes


def _evidence_tag(e: Evidence) -> str:
    return "[source primaire]" if e.primary else "[RUMEUR — presse]"


def _metric_value(label: str, value: float) -> str:
    base = label.split("(", 1)[0]
    if base in PERCENT_METRICS:
        return f"{num(value, 1)} %"
    if base in RATIO_METRICS:
        return f"{num(value, 3)} (soit {_signed_pct(100 * (value - 1))})"
    return _figure(value)


def _threshold(label: str, threshold: float) -> str:
    return f"{num(threshold)}{' %' if label.split('(', 1)[0] in PERCENT_METRICS else ''}"


def _check_line(c: ConditionCheck) -> str:
    verdict = "condition remplie" if c.met else "condition non remplie"
    return (f"- {metric_text(c.metric)} : {_metric_value(c.metric, c.value)} ; "
            f"seuil : {OP_TEXT[c.op]} {_threshold(c.metric, c.threshold)} → {verdict}")


def _price_line(alert: Alert) -> str | None:
    p = alert.price
    if p is None:
        return None
    currency = f" {alert.currency}" if alert.currency else ""
    parts = [f"clôture du {p.last_close_date:%d/%m/%Y} à {num(p.last_close, 2)}{currency}"]
    if p.daily_move_pct is not None:
        parts.append(f"variation J-1 : {_signed_pct(p.daily_move_pct)}")
    if alert.entry_price:
        parts.append(f"écart au prix d'entrée : {_signed_pct(100 * (p.last_close / alert.entry_price - 1))} "
                     f"(entrée à {num(alert.entry_price, 2)}{currency})")
    return "Cours : " + " ; ".join(parts)


def _alert_block(alert: Alert) -> str:
    rule = alert.rule_id if alert.rule_id == alert.source_rule_id else f"{alert.rule_id} (règle {alert.source_rule_id})"
    rule_line = f"Règle : {rule}" + (f" · {_one_line(alert.rule_text)}" if alert.rule_text else "")
    header = f"{ACTION_LABELS[alert.action]} [{alert.severity.value}]"
    if alert.is_rumor:
        header += " [RUMEUR — aucune source primaire]"
    lines = [
        header,
        _one_line(alert.headline),
        rule_line,
        f"Origine : {ORIGIN_LABELS[alert.origin]} · date : {alert.event_date:%d/%m/%Y}",
        "",
        ("Analyse : " if alert.origin == "event" else "Explication : ") + alert.rationale.strip(),
    ]
    if alert.downgrade_reason:
        lines += ["", f"Attention : {alert.downgrade_reason}"]
    details: list[str] = []
    if alert.figures:
        # Seuls les chiffres d'un match viennent du LLM ; ceux d'une surveillance sont sa valeur de référence.
        label = "Chiffres extraits" if alert.origin == "event" else "Valeurs de référence"
        details.append(f"{label} : " + ", ".join(f"{k} = {_figure(v)}" for k, v in alert.figures.items()))
    if alert.checks:
        details.append("Seuils évalués :")
        details += [_check_line(c) for c in alert.checks]
    checked = {c.metric for c in alert.checks}
    extra = {k: v for k, v in alert.metrics.items() if k not in checked and k != "daily_move_pct"}
    if extra:
        details.append("Métriques calculées : " + ", ".join(f"{metric_text(k)} = {_metric_value(k, v)}"
                                                            for k, v in extra.items()))
    if (price := _price_line(alert)) is not None:
        details.append(price)
    if alert.evidence:
        details.append("Preuves :")
        details += [f"- {e.url} {_evidence_tag(e)} ({e.source_name})" for e in alert.evidence]
    if alert.confidence is not None:
        details.append(f"Confiance du LLM : {num(alert.confidence, 2)}")
    if details:
        lines += [""] + details
    return "\n".join(lines)


def alert_mail(agent_id: str, alerts: Sequence[Alert]) -> Mail:
    """Mail d'alerte d'un agent. `alerts` est déjà trié (§6.6) : la première donne l'objet."""
    if not alerts:
        raise ValueError("alert_mail : aucune alerte")
    lead = alerts[0]
    subject = f"[{lead.severity.value}] {agent_id} · {SUBJECT_LABELS[lead.action]} · {_one_line(lead.headline)}"
    intro = f"{agent_id} : {_plural(len(alerts), 'alerte')}, de la plus prioritaire à la moins prioritaire."
    body = SEPARATOR.join([intro] + [_alert_block(a) for a in alerts])
    return Mail(subject=subject, body=_with_footer(body))


def _main_link(alert: Alert) -> str:
    primary = [e for e in alert.evidence if e.primary]
    chosen = (primary or alert.evidence or [None])[0]
    return str(chosen.url) if chosen is not None else "(pas de lien)"


def digest_mail(day: date, alerts_by_agent: Mapping[str, Sequence[Alert]], *, allow_empty: bool = False) -> Mail:
    """Digest INFO global : une section par agent, une ligne par alerte (règle, headline, lien, tag rumeur).

    `allow_empty` : digest vide explicitement demandé (`digest.send_if_empty`).
    """
    total = sum(len(alerts) for alerts in alerts_by_agent.values())
    if total == 0 and not allow_empty:
        raise ValueError("digest_mail : aucune alerte")
    sections = []
    for agent_id, alerts in alerts_by_agent.items():
        if not alerts:
            continue
        lines = [agent_id]
        lines += [
            f"- {a.rule_id} · {_one_line(a.headline)} · {_main_link(a)}{' [RUMEUR]' if a.is_rumor else ''}"
            for a in alerts
        ]
        sections.append("\n".join(lines))
    body = "\n\n".join(sections) if sections else "Rien à signaler aujourd'hui."
    plural = "s" if total > 1 else ""
    return Mail(
        subject=f"[INFO] Veille du {day:%d/%m} — {total} élément{plural}",
        body=_with_footer(body),
    )


# --------------------------------------------------------------------------- heartbeat


def _day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d/%m}"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "durée inconnue"
    seconds = round(seconds)
    return f"{seconds // 60} min {seconds % 60:02d} s" if seconds >= 60 else f"{seconds} s"


def _tokens(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _occurrence_lines(items: Sequence[Occurrence]) -> list[str]:
    lines = [f"- {o.key} : {_one_line(o.message)}{f' (×{o.count})' if o.count > 1 else ''}"
             for o in items[:MAX_OCCURRENCES]]
    if len(items) > MAX_OCCURRENCES:
        lines.append(f"- … et {len(items) - MAX_OCCURRENCES} autre(s), voir data/<env>/logs/watcher.log")
    return lines


def heartbeat_mail(hb: HeartbeatData, *, tz: tzinfo) -> Mail:
    """Heartbeat hebdomadaire. `tz` : fuseau d'affichage des heures de run."""
    sections: list[str] = []

    lines = [f"Runs des 7 derniers jours ({_day(hb.start)} → {_day(hb.today)}) :"]
    if hb.runs:
        for r in hb.runs:
            local = r.started_at.astimezone(tz)
            lines.append(f"- {_day(local.date())} {local:%H:%M} · {r.scope} · {r.status} · "
                         f"{_duration(r.duration_s)} · {_plural(r.alerts_created, 'alerte')} créée"
                         f"{'s' if r.alerts_created > 1 else ''} · {_tokens(r.input_tokens + r.output_tokens)} tokens")
    else:
        lines.append("- aucun run enregistré")
    if hb.days_without_ok:
        lines.append("Jours sans run quotidien réussi : " + ", ".join(_day(d) for d in hb.days_without_ok))
    sections.append("\n".join(lines))

    lines = ["Alertes envoyées :"]
    if hb.sent:
        by_agent: dict[str, list[Alert]] = {}
        for row in hb.sent:
            by_agent.setdefault(row.agent_id, []).append(row.alert)
        for agent_id in sorted(by_agent):
            alerts = by_agent[agent_id]
            severities = Counter(a.severity for a in alerts)
            detail = ", ".join(f"{severities[s]} {s.value}" for s in Severity if severities[s])
            rules = ", ".join(f"{a.rule_id} ({a.event_date:%d/%m})" for a in alerts)
            lines.append(f"- {agent_id} : {len(alerts)} ({detail}) : {rules}")
    else:
        lines.append("- aucune")
    sections.append("\n".join(lines))

    lines = ["Statut des agents :"]
    lines += [f"- {a.agent_id} : {a.state}{f' ({a.detail})' if a.detail else ''}" for a in hb.agents]
    sections.append("\n".join(lines))

    if hb.config_errors:
        lines = ["Erreurs de configuration en cours (agents désactivés) :"]
        lines += [f"- {e.agent_id} (depuis le {e.first_seen_at[:10]}) : {_one_line(e.message)}"
                  for e in hb.config_errors]
        sections.append("\n".join(lines))

    if hb.errors:
        sections.append("\n".join(["Erreurs des runs :"] + _occurrence_lines(hb.errors)))
    lines = ["Sources et cours en erreur :"]
    lines += _occurrence_lines(hb.warnings) if hb.warnings else ["- aucune"]
    sections.append("\n".join(lines))

    lines = ["Surveillances armées actives :"]
    if hb.watches:
        lines += [f"- {w.agent_id} {w.arm_id} : référence {num(w.ref_value, 2)}, seuil cours / référence "
                  f"{OP_TEXT.get(w.op, w.op)} {num(w.threshold)} → {w.action.value} {w.severity.value} "
                  f"(armée le {w.created_at[:10]})" for w in hb.watches]
    else:
        lines.append("- aucune")
    sections.append("\n".join(lines))

    lines = [f"Tokens sur 7 jours : {_tokens(hb.input_tokens)} en entrée, {_tokens(hb.output_tokens)} en sortie"]
    lines += [f"- {model} : {_tokens(t_in)} en entrée, {_tokens(t_out)} en sortie"
              for model, (t_in, t_out) in sorted(hb.tokens_by_model.items())]
    cost = f"Coût estimé : {num(hb.cost_usd, 2)} $"
    if hb.unpriced_models:
        cost += f" (hors {', '.join(hb.unpriced_models)} : tarif absent de llm_pricing)"
    lines.append(cost)
    sections.append("\n".join(lines))

    lines = ["Points d'attention :"]
    lines += [f"- {item}" for item in hb.attention] if hb.attention else ["- aucun"]
    sections.append("\n".join(lines))

    sent = len(hb.sent)
    subject = (f"[HEARTBEAT] Semaine du {hb.start:%d/%m} — {hb.days_ok}/7 runs OK, "
               f"{sent} alerte{'s' if sent > 1 else ''}")
    return Mail(subject=subject, body=_with_footer("\n\n".join(sections), HEARTBEAT_FOOTER))
