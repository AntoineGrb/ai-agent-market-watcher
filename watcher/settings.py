"""Paramètres d'exécution issus des variables d'environnement (cadrage §11.3 et §11.4).

Aucune valeur secrète n'a de défaut ; les secrets sont des `SecretStr` pour ne jamais apparaître dans les logs.
Les champs requis par une brique (SMTP, Healthchecks...) sont vérifiés par cette brique, au moment de l'utiliser :
un run sans SMTP configuré peut encore charger les configs et écrire en base.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

Env = Literal["prod", "test"]


class SettingsError(Exception):
    """Variable d'environnement invalide."""


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    env: Env = "prod"
    anthropic_api_key: SecretStr | None = None
    model_triage: str | None = None       # surcharge de _defaults.yaml
    model_analysis: str | None = None
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, gt=0, lt=65536)
    smtp_user: str | None = None
    smtp_app_password: SecretStr | None = None
    mail_to: str | None = None
    healthchecks_url: str | None = None   # URL de ping du check, sans suffixe
    sec_user_agent: str | None = None
    base_dir: Path = PROJECT_ROOT

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: object) -> Settings:
        """Construit les paramètres depuis l'environnement. Une variable vide équivaut à une variable absente."""
        environ = os.environ if environ is None else environ
        mapping = {
            "env": "WATCHER_ENV",
            "anthropic_api_key": "ANTHROPIC_API_KEY",
            "model_triage": "WATCHER_MODEL_TRIAGE",
            "model_analysis": "WATCHER_MODEL_ANALYSIS",
            "smtp_host": "SMTP_HOST",
            "smtp_port": "SMTP_PORT",
            "smtp_user": "SMTP_USER",
            "smtp_app_password": "SMTP_APP_PASSWORD",
            "mail_to": "MAIL_TO",
            "healthchecks_url": "HEALTHCHECKS_URL",
            "sec_user_agent": "SEC_USER_AGENT",
        }
        values: dict[str, object] = {}
        for field_name, var in mapping.items():
            raw = environ.get(var, "").strip()
            if raw:
                values[field_name] = raw
        values.update(overrides)
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            details = "; ".join(f"{'.'.join(map(str, e['loc']))} : {e['msg']}" for e in exc.errors(include_url=False))
            raise SettingsError(f"variables d'environnement invalides : {details}") from exc

    # ------------------------------------------------------------------ chemins

    @property
    def agents_dir(self) -> Path:
        return self.base_dir / "agents"

    @property
    def data_dir(self) -> Path:
        """`data/<env>/` : la base et les logs de test ne touchent jamais ceux de prod."""
        return self.base_dir / "data" / self.env

    @property
    def db_path(self) -> Path:
        return self.data_dir / "watcher.sqlite"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    # ------------------------------------------------------------------ comportements liés à l'environnement

    @property
    def is_test(self) -> bool:
        return self.env == "test"

    @property
    def subject_prefix(self) -> str:
        return "[TEST] " if self.is_test else ""

    @property
    def healthchecks_enabled(self) -> bool:
        """Désactivé en test (§11.4) pour ne jamais fausser le suivi du job de prod."""
        return not self.is_test and bool(self.healthchecks_url)
