from __future__ import annotations

from datetime import date, timedelta

import pytest

from watcher.config import Action, Defaults, Severity
from watcher.models import Alert, Evidence
from watcher.notify import templates
from watcher.notify.dispatch import send_outbox
from watcher.notify.mailer import Mail, MailError
from watcher.store import Store
from tests.conftest import NOW, TODAY, FakeMailer, snapshot

A, S = Action, Severity


def _alert(agent_id: str, rule_id: str, action: Action, severity: Severity, *, rumor: bool = False,
           headline: str = "Titre") -> Alert:
    evidence = [Evidence(item_id="x", url="https://example.com/press", source_name="Google News FR", primary=False)]
    if not rumor:
        evidence.append(Evidence(item_id="y", url="https://example.com/amf", source_name="AMF", primary=True))
    return Alert(agent_id=agent_id, rule_id=rule_id, source_rule_id=rule_id, origin="event", action=action,
                 severity=severity, headline=headline, rationale="Passage clé.", evidence=evidence,
                 is_rumor=rumor, confidence=0.9, event_date=date(2026, 9, 24))


def _seed(store: Store, *alerts: Alert) -> list[int]:
    return [store.insert_event(a, f"event:{a.rule_id}", NOW) for a in alerts]


class SelectiveMailer(FakeMailer):
    """Refuse les mails dont l'objet commence par `refused`."""

    def __init__(self, refused: str) -> None:
        super().__init__()
        self.refused = refused

    def send(self, mail: Mail) -> None:
        if mail.subject.startswith(self.refused):
            raise MailError("SMTP refusé")
        super().send(mail)


def _send(store: Store, mailer: FakeMailer, defaults: Defaults, now=NOW):
    return send_outbox(store, mailer, priority=defaults.priority, now=now, digest_day=TODAY)


def test_empty_outbox_sends_nothing(store: Store, defaults: Defaults) -> None:
    mailer = FakeMailer()
    report = _send(store, mailer, defaults)
    assert mailer.sent == [] and report.mails_sent == 0


def test_one_mail_per_agent_and_one_digest(store: Store, defaults: Defaults) -> None:
    _seed(store,
          _alert("UBI", "U-S5", A.RECO_UNCLEAR, S.HIGH, headline="Notation dégradée"),
          _alert("UBI", "U-S2", A.RECO_SELL_ALL, S.CRITICAL, headline="Bris de covenant\nsans waiver"),
          _alert("NANO", "N-N1", A.RECO_HOLD, S.INFO, headline="Lecture décalée"),
          _alert("UBI", "U-N1", A.RECO_HOLD, S.INFO, rumor=True, headline="Jeu reporté"))
    mailer = FakeMailer()
    report = _send(store, mailer, defaults)
    assert (report.mails_sent, report.alerts_sent, report.errors) == (2, 4, [])
    assert store.pending_events() == []

    alert_mail, digest = mailer.sent
    # Objet : alerte de tête (CRITICAL avant HIGH), sans saut de ligne.
    assert alert_mail.subject == "[CRITICAL] UBI · Vendre toute la ligne · Bris de covenant sans waiver"
    body = alert_mail.body
    assert body.index("Règle : U-S2") < body.index("Règle : U-S5")
    assert "Je te recommande de vendre toute la ligne [CRITICAL]" in body
    assert "https://example.com/amf [source primaire] (AMF)" in body
    assert "https://example.com/press [RUMEUR — presse] (Google News FR)" in body
    assert "Confiance du LLM : 0,90" in body
    assert body.rstrip().endswith(templates.ALERT_FOOTER)

    assert digest.subject == "[INFO] Veille du 25/09 — 2 éléments"
    assert digest.body.index("NANO") < digest.body.index("UBI")
    assert "- N-N1 · Lecture décalée · https://example.com/amf" in digest.body
    assert "- U-N1 · Jeu reporté · https://example.com/press [RUMEUR]" in digest.body


def test_failed_mail_stays_in_outbox_for_next_run(store: Store, defaults: Defaults) -> None:
    _seed(store, _alert("UBI", "U-S2", A.RECO_SELL_ALL, S.CRITICAL), _alert("UBI", "U-N1", A.RECO_HOLD, S.INFO))
    report = _send(store, SelectiveMailer(refused="[CRITICAL]"), defaults)
    assert report.mails_sent == 1 and len(report.errors) == 1
    assert "alerte UBI" in report.errors[0]
    assert [e.alert.rule_id for e in store.pending_events()] == ["U-S2"]   # le digest, lui, est parti

    mailer = FakeMailer()
    report = _send(store, mailer, defaults, now=NOW + timedelta(days=1))
    assert [m.subject.split(" · ")[0] for m in mailer.sent] == ["[CRITICAL] UBI"]
    assert store.pending_events() == []


def test_alert_block_details() -> None:
    alert = _alert("UBI", "U-B1", A.RECO_HOLD, S.CRITICAL).model_copy(update={
        "source_rule_id": "U-B1", "rule_id": "U-B2", "figures": {"offer_price": 11.0},
        "metrics": {"figure_vs_prev_close(offer_price)": 1.157895}, "price": snapshot(10.0, 9.5),
        "downgrade_reason": "surveillance U-B1-EXIT non armée : confiance 0,60 < 0,80",
    })
    body = templates.alert_mail("UBI", [alert]).body
    assert "Règle : U-B2 (règle U-B1)" in body
    assert "Chiffres extraits : offer_price = 11" in body
    assert "Métriques calculées : figure_vs_prev_close(offer_price) = 1,158" in body
    assert "Cours : clôture du 24/09/2026 à 10,00, variation J-1 : 5,3 %" in body
    assert "Attention : surveillance U-B1-EXIT non armée" in body


def test_templates_refuse_empty_input() -> None:
    with pytest.raises(ValueError):
        templates.alert_mail("UBI", [])
    with pytest.raises(ValueError):
        templates.digest_mail(TODAY, {"UBI": []})


def test_digest_singular_and_missing_link() -> None:
    alert = _alert("UBI", "U-N1", A.RECO_HOLD, S.INFO).model_copy(update={"evidence": []})
    mail = templates.digest_mail(TODAY, {"UBI": [alert]})
    assert mail.subject == "[INFO] Veille du 25/09 — 1 élément"
    assert "(pas de lien)" in mail.body
