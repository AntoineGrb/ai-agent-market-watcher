"""Consignes des agents LLM et assemblage des messages (cadrage §7.1 à §7.4).

Les consignes (stables pour un agent donné) sont séparées des messages (documents et cours du jour, volatils).
Le LLM ne voit que les règles `event` actives, jamais les actions ni les sévérités : elles sont décidées par le code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from watcher.config import AgentConfig, EventRule
from watcher.models import NewsItem, PriceSnapshot
from watcher.sources.text import truncate

TRIAGE_SUMMARY_CHARS = 500
PRICE_HISTORY_SESSIONS = 10

# Cadrage §7.3, à intégrer telles quelles.
GLOBAL_INSTRUCTIONS = """\
Tu es un agent de veille boursière 100 % consultatif. Tu n'as aucun accès au portefeuille ni au courtier.
Ta seule tâche : lire les documents fournis et identifier ceux qui décrivent un événement correspondant
à l'une des règles listées.

Règles de sortie :
- Pour chaque événement correspondant à une règle, renvoie un match avec l'ID exact de la règle
  et les IDs des documents qui le prouvent.
- Si aucun document ne correspond à une règle, renvoie une liste vide. C'est le cas le plus fréquent
  et c'est une réponse correcte.
- Ne décide jamais d'une action ni d'une sévérité : elles sont déterminées ailleurs.
- Ne fais aucun calcul (pourcentage, dilution, prime, conversion de devise). Extrais uniquement
  les chiffres bruts demandés par la règle, tels qu'écrits dans le document, convertis en nombre
  (ex. « 40 millions d'actions » → 40000000).
- Si un chiffre demandé n'apparaît pas explicitement dans le document, omets la clé.
  N'estime jamais un chiffre absent.
- event_date est la date de l'événement annoncé (date du communiqué), pas la date de l'article qui le relaie.
- Un article de presse peut correspondre à une règle : signale-le normalement.
- Si la formulation est ambiguë ou si un document semble correspondre à deux règles incompatibles,
  choisis la règle la plus prudente indiquée dans les consignes de l'agent et explique l'ambiguïté.
- confidence mesure ta certitude que l'événement décrit correspond exactement au déclencheur de la règle,
  pas la probabilité que l'information soit vraie.
- rationale : en français, 3 phrases maximum, en citant le passage clé du document."""

REFERENCES_NOTE = """\
Les documents sont identifiés par une référence courte (D1, D2...) : c'est elle que tu indiques dans item_ids.
Dans extracted_figures, n'utilise que les noms de chiffres listés pour la règle."""

TRIAGE_INSTRUCTIONS = """\
Tu fais le premier tri des documents d'une veille boursière sur {company}. Un second agent lira ensuite
en détail les documents que tu gardes.

Garde tout document qui pourrait, même indirectement, annoncer ou commenter l'un des événements surveillés
ci-dessous, quel que soit son sujet apparent (un article de presse jeu vidéo ou santé peut annoncer un événement
surveillé). En cas de doute, garde le document : un document écarté à tort ne sera jamais relu, alors qu'un
document gardé à tort ne coûte qu'une lecture. N'écarte que le bruit évident : homonyme, autre société, ou
document qui ne touche à aucun des événements surveillés (critique ou test d'un jeu, publicité, etc.).

Tous les événements listés comptent, y compris ceux qui paraissent neutres, rassurants ou de routine : ils sont
suivis volontairement. Ne juge jamais de l'importance ou de l'impact d'un événement, seulement de sa
correspondance avec la liste.

Renvoie uniquement les références (D1, D2...) des documents à garder. Liste vide si aucun ne mérite d'être lu."""

DIRECTION_LABELS = {
    "entry": "entrée en position",
    "bullish": "haussier",
    "bearish": "baissier",
    "neutral": "neutre",
}


# --------------------------------------------------------------------------- tri (Haiku)


def triage_instructions(cfg: AgentConfig, rules: Sequence[EventRule]) -> str:
    """Société, thèse, mots-clés et déclencheurs des règles actives (texte seul, sans ID)."""
    triggers = "\n".join(f"- {_one_line(r.trigger)}" for r in rules)
    return "\n\n".join([
        TRIAGE_INSTRUCTIONS.format(company=cfg.company),
        f"Thèse suivie : {_one_line(cfg.thesis)}",
        f"Mots-clés : {', '.join(cfg.keywords)}",
        f"Événements surveillés :\n{triggers}",
    ])


def triage_prompt(refs: Mapping[str, NewsItem]) -> str:
    blocks = []
    for ref, item in refs.items():
        lines = [f"[{ref}] {item.title}", f"Source : {item.source_name} ; date : {_published(item)}"]
        if summary := _one_line(item.summary):
            lines.append(f"Résumé : {truncate(summary, TRIAGE_SUMMARY_CHARS)}")
        blocks.append("\n".join(lines))
    return f"Documents à trier ({len(refs)}) :\n\n" + "\n\n".join(blocks)


# --------------------------------------------------------------------------- analyse (Sonnet)


def format_rule(rule: EventRule) -> str:
    lines = [f"### {rule.id} ({DIRECTION_LABELS[rule.direction]})", f"Déclencheur : {_one_line(rule.trigger)}"]
    if rule.figures:
        lines.append("Chiffres à extraire (omets la clé si le chiffre n'est pas écrit dans le document) :")
        lines += [f"- {name} : {_one_line(desc)}" for name, desc in rule.figures.items()]
    else:
        lines.append("Aucun chiffre à extraire.")
    return "\n".join(lines)


def analysis_instructions(cfg: AgentConfig, rules: Sequence[EventRule]) -> str:
    """Consignes globales + prompt.md de l'agent + règles `event` actives (ID, déclencheur, chiffres)."""
    return "\n\n".join([
        GLOBAL_INSTRUCTIONS,
        REFERENCES_NOTE,
        "# Consignes propres à l'agent\n\n" + cfg.prompt,
        "# Règles surveillées\n\n" + "\n\n".join(format_rule(r) for r in rules),
    ])


def price_context(cfg: AgentConfig, price: PriceSnapshot | None) -> str:
    pos = cfg.position
    header = f"Ligne suivie : {pos.ticker} ({pos.listing}, {pos.currency}), position {pos.status}."
    if price is None:
        return f"{header}\nCours indisponible pour ce run."
    move = "n/d" if price.daily_move_pct is None else f"{price.daily_move_pct:+.2f} %"
    history = " ; ".join(f"{d.isoformat()} : {close:g}" for d, close in price.history[-PRICE_HISTORY_SESSIONS:])
    return (f"{header}\nDernière clôture : {price.last_close:g} {pos.currency} le "
            f"{price.last_close_date.isoformat()} (variation J-1 : {move}).\n"
            f"Clôtures des dernières séances : {history}")


def analysis_prompt(
    cfg: AgentConfig, refs: Mapping[str, NewsItem], price: PriceSnapshot | None, *, today: date, max_doc_chars: int
) -> str:
    docs = []
    for ref, item in refs.items():
        text = truncate(item.text or f"{item.title}\n{item.summary}", max_doc_chars)
        docs.append(f"## [{ref}] {item.title}\nSource : {item.source_name} ; date de publication : "
                    f"{_published(item)}\n\n{text}")
    return "\n\n".join([
        f"Date du jour : {today.isoformat()}.",
        "# Contexte de cours\n\n" + price_context(cfg, price),
        f"# Documents ({len(refs)})\n\n" + "\n\n".join(docs),
    ])


# --------------------------------------------------------------------------- utilitaires


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _published(item: NewsItem) -> str:
    return item.published_at.date().isoformat() if item.published_at else "inconnue"
