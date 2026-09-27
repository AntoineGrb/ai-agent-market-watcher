"""Briques communes aux agents LLM : modèles, budget de tokens, références de documents (cadrage §7).

- Le modèle est résolu au moment du run (config, surchargée par `WATCHER_MODEL_TRIAGE` / `WATCHER_MODEL_ANALYSIS`),
  jamais à la construction des agents : c'est ce qui permet de changer de modèle par config ou pour les evals.
- Chaque appel est plafonné (`UsageLimits`) par le budget restant du run (`llm_budget.max_total_tokens_per_run`).
  Les tokens consommés sont comptés même quand l'appel échoue.
- Les documents sont présentés au LLM sous des références courtes (`D1`, `D2`...) plutôt que sous leur ID sha256
  de 64 caractères : moins d'erreurs de recopie, donc moins de retries. Le code retraduit ces références en IDs
  réels après validation.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

import pydantic_ai
from pydantic_ai.exceptions import AgentRunError, UsageLimitExceeded
from pydantic_ai.models import Model, infer_model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

from watcher.models import NewsItem
from watcher.settings import Settings

log = logging.getLogger(__name__)

# Bannière de premier lancement de PydanticAI : sans intérêt dans les logs d'un job planifié.
pydantic_ai.BANNER_ENABLED = False

ANTHROPIC_PREFIX = "anthropic:"
REF_PREFIX = "D"
# Requêtes par appel : 1 + `retries` tentatives de sortie ; une de marge.
MAX_REQUESTS_PER_CALL = 4


class LlmError(Exception):
    """Échec de la couche LLM pour un agent : l'agent est en échec pour ce run (documents non marqués vus)."""


class LlmConfigError(LlmError):
    """Modèle inutilisable (clé API absente, nom de modèle inconnu)."""


class LlmBudgetExceeded(LlmError):
    """Budget de tokens du run épuisé : le run s'arrête proprement et passe en échec (cadrage §7.5)."""


# --------------------------------------------------------------------------- modèles


def resolve_model(name: str, settings: Settings, *, max_tokens: int) -> Model:
    """Instancie le modèle `provider:nom`. La clé Anthropic vient des settings, jamais d'une valeur en dur.

    Les autres providers (comparaisons en eval, ex. `google-gla:...`) lisent leur clé dans l'environnement.
    """
    model_settings = ModelSettings(max_tokens=max_tokens)
    if name.startswith(ANTHROPIC_PREFIX):
        if settings.anthropic_api_key is None:
            raise LlmConfigError(f"ANTHROPIC_API_KEY absente : modèle {name} inutilisable")
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key=settings.anthropic_api_key.get_secret_value())
        return AnthropicModel(name.removeprefix(ANTHROPIC_PREFIX), provider=provider, settings=model_settings)
    try:
        model = infer_model(name)
    except Exception as exc:   # provider inconnu, dépendance absente, clé manquante...
        raise LlmConfigError(f"modèle {name!r} inutilisable : {exc}") from exc
    return model


# --------------------------------------------------------------------------- budget de tokens


def model_key(model: Model) -> str:
    """Nom `provider:modèle`, tel qu'écrit dans `_defaults.yaml` (clé des tarifs du heartbeat)."""
    return f"{model.system}:{model.model_name}"


@dataclass
class LlmUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    # Tokens par modèle (`provider:modèle` → [entrée, sortie]) : sert à estimer le coût dans le heartbeat.
    by_model: dict[str, list[int]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, usage: RunUsage | LlmUsage, model: str | None = None) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.requests += usage.requests
        if isinstance(usage, LlmUsage):
            for name, (tokens_in, tokens_out) in usage.by_model.items():
                self._add_model(name, tokens_in, tokens_out)
        elif model is not None:
            self._add_model(model, usage.input_tokens, usage.output_tokens)

    def _add_model(self, name: str, tokens_in: int, tokens_out: int) -> None:
        totals = self.by_model.setdefault(name, [0, 0])
        totals[0] += tokens_in
        totals[1] += tokens_out


@dataclass
class TokenBudget:
    """Budget de tokens partagé par tous les agents d'un run."""

    max_total_tokens: int
    used: LlmUsage = field(default_factory=LlmUsage)

    @property
    def remaining(self) -> int:
        return max(0, self.max_total_tokens - self.used.total_tokens)

    @contextmanager
    def call(self, label: str, model: str) -> Iterator[tuple[RunUsage, UsageLimits]]:
        """Encadre un appel LLM : plafonds de l'appel, puis comptage des tokens (par modèle), même en cas d'échec."""
        if self.remaining == 0:
            raise LlmBudgetExceeded(f"budget de {self.max_total_tokens} tokens du run épuisé avant : {label}")
        usage = RunUsage()
        limits = UsageLimits(request_limit=MAX_REQUESTS_PER_CALL, total_tokens_limit=self.remaining)
        try:
            yield usage, limits
        except UsageLimitExceeded as exc:
            if self.used.total_tokens + usage.total_tokens >= self.max_total_tokens:
                raise LlmBudgetExceeded(f"budget de {self.max_total_tokens} tokens du run épuisé pendant : "
                                        f"{label}") from exc
            raise LlmError(f"{label} : {exc}") from exc
        except AgentRunError as exc:   # retries épuisés (validation), erreur de l'API...
            raise LlmError(f"{label} : {type(exc).__name__} : {exc}") from exc
        finally:
            self.used.add(usage, model)
            log.info("%s : %d requête(s), %d tokens en entrée, %d en sortie (run : %d / %d)", label,
                     usage.requests, usage.input_tokens, usage.output_tokens, self.used.total_tokens,
                     self.max_total_tokens)


# --------------------------------------------------------------------------- références de documents


def assign_refs(items: Sequence[NewsItem]) -> dict[str, NewsItem]:
    """Références courtes, stables dans l'ordre fourni : `D1`, `D2`..."""
    return {f"{REF_PREFIX}{i}": item for i, item in enumerate(items, start=1)}


def unknown_refs(refs: Sequence[str], known: Mapping[str, NewsItem]) -> list[str]:
    return [r for r in refs if r not in known]


def batched(items: Sequence[NewsItem], size: int) -> Iterator[list[NewsItem]]:
    for start in range(0, len(items), size):
        yield list(items[start:start + size])
