"""Fixtures étiquetées : `fixtures/<agent>/<cas>.md`, en-tête YAML + texte du document (cadrage §12.2).

Servent aux evals (`pytest -m eval`) et à l'injection de bout en bout (`--inject`).

```markdown
---
agent: NANO
source_name: SEC EDGAR
source_primary: true
synthetic: false
expected_rule_ids: [N-S4]
expected_figures: {new_shares: 3000000}
---
Texte intégral du communiqué...
```
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from watcher.config import ConfigError, format_validation_error
from watcher.models import NewsItem

FRONT_MATTER = "---"
FIXTURE_SOURCE_TYPE = "fixture"   # aucun fetcher : le texte est déjà complet


class FixtureHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str
    source_name: str
    source_primary: bool = False
    synthetic: bool
    status: Literal["WATCH", "OWNED"] | None = None   # evals : phase simulée (défaut : statut de la config)
    title: str | None = None                  # défaut : première ligne non vide du texte
    url: HttpUrl | None = None                # défaut : URL fictive (le mail de test l'affiche en preuve)
    published_at: datetime | None = None      # défaut : instant de l'injection
    expected_rule_ids: list[str] = Field(default_factory=list)   # vide = aucun match attendu
    allowed_rule_ids: list[str] = Field(default_factory=list)    # matches tolérés sans être exigés (rappels)
    expected_figures: dict[str, float] = Field(default_factory=dict)
    note: str | None = None


@dataclass(frozen=True)
class Fixture:
    path: Path
    header: FixtureHeader
    text: str

    @property
    def name(self) -> str:
        return f"{self.header.agent}/{self.path.stem}"

    @property
    def title(self) -> str:
        if self.header.title:
            return self.header.title
        first = next((line.strip().lstrip("#").strip() for line in self.text.splitlines() if line.strip()), "")
        return first[:200] or self.path.stem

    def to_item(self, *, now: datetime, primary: bool | None = None) -> NewsItem:
        """Document prêt pour le pipeline. `primary` force le caractère primaire de la source (`--primary`)."""
        h = self.header
        digest = hashlib.sha256(f"fixture\x1f{self.name}\x1f{self.text}".encode()).hexdigest()
        return NewsItem(
            id=digest, agent_id=h.agent, source_name=h.source_name, source_type=FIXTURE_SOURCE_TYPE,
            source_primary=h.source_primary if primary is None else primary,
            url=h.url or f"https://fixtures.invalid/{h.agent.lower()}/{self.path.stem}",
            title=self.title, published_at=h.published_at or now, summary=self.text[:500], text=self.text,
        )


def load_fixture(path: Path) -> Fixture:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"fixture introuvable : {path}") from exc
    lines = raw.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER:
        raise ConfigError(f"{path.name} : en-tête YAML absent (le fichier doit commencer par ---)")
    try:
        end = next(i for i, line in enumerate(lines[1:], start=1) if line.strip() == FRONT_MATTER)
    except StopIteration as exc:
        raise ConfigError(f"{path.name} : en-tête YAML non fermé (--- manquant)") from exc
    try:
        header = FixtureHeader.model_validate(yaml.safe_load("\n".join(lines[1:end])))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} : YAML invalide : {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"{path.name} : en-tête invalide :\n{format_validation_error(exc)}") from exc
    text = "\n".join(lines[end + 1:]).strip()
    if not text:
        raise ConfigError(f"{path.name} : texte du document vide")
    return Fixture(path=path, header=header.model_copy(update={"agent": header.agent.upper()}), text=text)


def discover_fixtures(fixtures_dir: Path) -> list[Fixture]:
    return [load_fixture(p) for p in sorted(fixtures_dir.glob("*/*.md"))]
