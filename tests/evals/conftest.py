from __future__ import annotations

from datetime import UTC, datetime

import pytest

from watcher.llm.evals import CaseResult, format_report
from watcher.settings import PROJECT_ROOT

_RESULTS: list[CaseResult] = []


@pytest.fixture(scope="session")
def eval_results() -> list[CaseResult]:
    return _RESULTS


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Tableau récapitulatif par cas et par modèle, affiché et écrit dans `data/evals/`."""
    if not _RESULTS:
        return
    report = format_report(_RESULTS)
    terminalreporter.write_sep("=", "evals LLM")
    terminalreporter.write_line(report)
    out_dir = PROJECT_ROOT / "data" / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"evals-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.md"
    path.write_text(report + "\n", encoding="utf-8")
    terminalreporter.write_line(f"rapport écrit dans {path}")
