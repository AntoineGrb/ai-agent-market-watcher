"""Evals avec appels LLM réels (cadrage §12.2) : `pytest -m eval`.

Chaque fixture passe par la couche LLM complète (tri puis analyse), avec les modèles de `_defaults.yaml`
ou ceux de `WATCHER_MODEL_TRIAGE` / `WATCHER_MODEL_ANALYSIS` pour comparer des modèles.
Coût indicatif : quelques centimes de dollar pour le jeu complet avec les modèles par défaut.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from watcher.config import load_agent, load_defaults
from watcher.fixtures import Fixture, discover_fixtures
from watcher.llm import LlmError, LlmLayer, TokenBudget
from watcher.llm.evals import CaseResult, eval_config, score_case
from watcher.run import _load_dotenv
from watcher.settings import PROJECT_ROOT, Settings

pytestmark = pytest.mark.eval

FIXTURES = discover_fixtures(PROJECT_ROOT / "fixtures")
EVAL_BUDGET_TOKENS = 200_000   # par cas : largement au-dessus d'un document isolé


@pytest.fixture(scope="module")
def layer() -> LlmLayer:
    _load_dotenv()
    settings = Settings.from_env()
    if settings.anthropic_api_key is None:
        pytest.skip("ANTHROPIC_API_KEY absente : evals non exécutables")
    return LlmLayer(settings)


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
def test_fixture(fixture: Fixture, layer: LlmLayer, eval_results: list[CaseResult]) -> None:
    agents_dir = PROJECT_ROOT / "agents"
    defaults = load_defaults(agents_dir)
    cfg = eval_config(load_agent(agents_dir / fixture.header.agent.lower()), fixture)
    names = layer.model_names(defaults)
    model = f"{names['triage']} + {names['analysis']}"
    now = datetime.now(UTC)
    item = fixture.to_item(now=now)
    try:
        out = layer.process(cfg, defaults, [item], None, today=now.astimezone(defaults.schedule.tz).date(),
                            fired=set(), budget=TokenBudget(EVAL_BUDGET_TOKENS))
    except LlmError as exc:
        result = score_case(fixture, cfg, [], model=model)
        result.error = str(exc)
    else:
        result = score_case(fixture, cfg, out.matches, model=model)
    eval_results.append(result)
    assert result.passed, (f"{fixture.name} : attendu {result.expected}, trouvé {result.found}, "
                           f"faux positifs de vente {result.sale_false_positives}, chiffres {result.figure_errors}, "
                           f"erreur {result.error}")
