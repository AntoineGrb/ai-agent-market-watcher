from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from watcher.config import AgentConfig, Defaults, load_agent, load_defaults
from watcher.engine.metrics import daily_move_pct
from watcher.llm import LlmLayer
from watcher.models import NewsItem, PriceSnapshot, RuleMatch
from watcher.notify.mailer import Mail, MailError
from watcher.settings import PROJECT_ROOT, Settings
from watcher.sources.http import build_client
from watcher.store import Store

REPO_AGENTS_DIR = PROJECT_ROOT / "agents"
SOURCES_DATA = Path(__file__).parent / "data" / "sources"   # réponses réelles enregistrées à l'étape 0
NOW = datetime(2026, 9, 25, 5, 0, tzinfo=UTC)   # 07:00 Europe/Paris
TODAY = date(2026, 9, 25)                        # vendredi ; dernière séance : jeudi 24/09
LAST_CLOSE_DATE = date(2026, 9, 24)


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """Les retries HTTP gardent leur nombre, sans les pauses réelles entre tentatives."""
    monkeypatch.setattr("watcher.sources.http.RETRY_BACKOFF_S", 0.0)


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


# --------------------------------------------------------------------------- HTTP simulé (fetchers, cours)


Route = httpx.Response | Callable[[httpx.Request], httpx.Response]


class Router:
    """Transport httpx sans réseau : répond selon `scheme://hôte/chemin` (sans la query) et garde les requêtes."""

    def __init__(self, routes: dict[str, Route] | None = None) -> None:
        self.routes: dict[str, Route] = dict(routes or {})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = str(request.url.copy_with(query=None))
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, text=f"route non simulée : {key}")
        return route(request) if callable(route) else route

    def client(self) -> httpx.Client:
        return build_client(httpx.MockTransport(self))


OFFLINE = Router().client()   # client sans réseau ni contexte TLS : validation de paramètres, textes en cache


def data_file(name: str) -> bytes:
    return (SOURCES_DATA / name).read_bytes()


# --------------------------------------------------------------------------- moteur : objets de test


@pytest.fixture(scope="session")
def defaults() -> Defaults:
    return load_defaults(REPO_AGENTS_DIR)


def repo_agent(name: str, **position: Any) -> AgentConfig:
    """Config réelle d'un agent du dépôt, avec des champs de position éventuellement surchargés."""
    cfg = load_agent(REPO_AGENTS_DIR / name)
    if position:
        cfg = cfg.model_copy(update={"position": cfg.position.model_copy(update=position)})
    return cfg


def snapshot(
    last: float = 10.0,
    prev: float | None = 9.5,
    *,
    last_date: date = LAST_CLOSE_DATE,
    new: bool = True,
    history: list[tuple[date, float]] | None = None,
    symbol: str = "UBI.PA",
) -> PriceSnapshot:
    """Cours de test : par défaut 3 séances (22, 23 et 24/09), la dernière à `last`, la précédente à `prev`."""
    if history is None:
        history = [(date(2026, 9, 22), 9.0), (date(2026, 9, 23), prev if prev is not None else 9.0),
                   (last_date, last)]
    return PriceSnapshot(symbol=symbol, last_close=last, last_close_date=last_date, prev_close=prev,
                         daily_move_pct=daily_move_pct(prev, last), is_new_close=new, history=history)


def news(item_id: str, *, primary: bool = True, agent_id: str = "UBI", source_name: str | None = None) -> NewsItem:
    return NewsItem(
        id=item_id, agent_id=agent_id,
        source_name=source_name or ("AMF informations réglementées" if primary else "Google News FR"),
        source_type="dila_amf" if primary else "google_news_rss", source_primary=primary,
        url=f"https://example.com/{item_id}", title=f"Document {item_id}", published_at=NOW,
    )


def items_by_id(items: Iterable[NewsItem]) -> dict[str, NewsItem]:
    return {it.id: it for it in items}


def rule_match(
    rule_id: str,
    item_ids: Iterable[str] = ("a",),
    *,
    confidence: float = 0.9,
    event_date: date = LAST_CLOSE_DATE,
    **figures: float,
) -> RuleMatch:
    return RuleMatch(rule_id=rule_id, item_ids=list(item_ids), headline=f"Événement {rule_id}",
                     rationale="Passage clé du document.", confidence=confidence, event_date=event_date,
                     extracted_figures=figures)


# --------------------------------------------------------------------------- LLM simulé (PydanticAI)


class ScriptedModel(FunctionModel):
    """Modèle de test : renvoie successivement les sorties structurées données (la dernière est répétée).

    `calls` garde les messages de chaque requête, pour vérifier les consignes et les retries.
    """

    def __init__(self, *outputs: dict[str, Any]) -> None:
        self.outputs = list(outputs) or [{}]
        self.calls: list[list[ModelMessage]] = []
        super().__init__(self._respond)

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls.append(messages)
        args = self.outputs[min(len(self.calls), len(self.outputs)) - 1]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    def prompt(self, call: int = 0) -> str:
        """Texte du message utilisateur de la requête `call`."""
        return next(p.content for p in self.calls[call][0].parts if isinstance(p, UserPromptPart))

    def instructions(self, call: int = 0) -> str:
        return self.calls[call][0].instructions or ""

    def retry_feedback(self, call: int) -> str:
        return next(str(p.content) for p in self.calls[call][-1].parts if isinstance(p, RetryPromptPart))


def triage_output(*refs: str) -> dict[str, Any]:
    return {"relevant_ids": list(refs)}


def match_output(rule_id: str, *refs: str, confidence: float = 0.9, event_date: str = "2026-09-24",
                 **figures: float) -> dict[str, Any]:
    return {"rule_id": rule_id, "item_ids": list(refs) or ["D1"], "headline": f"Événement {rule_id}",
            "rationale": "« Passage clé. »", "confidence": confidence, "event_date": event_date,
            "extracted_figures": figures}


def analysis_output(*matches: dict[str, Any]) -> dict[str, Any]:
    return {"matches": list(matches)}


def llm_layer(settings: Settings, triage: ScriptedModel | None = None, analysis: ScriptedModel | None = None,
              text_fetcher: Callable[[NewsItem], str] | None = None) -> LlmLayer:
    """Couche LLM sans réseau : par défaut, le tri ne retient rien."""
    return LlmLayer(settings, triage_model=triage or ScriptedModel(triage_output()),
                    analysis_model=analysis or ScriptedModel(analysis_output()),
                    text_fetcher=text_fetcher or (lambda item: f"Texte complet de {item.title}"))
