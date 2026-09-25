"""Logs et monitoring externe (cadrage §11.5).

- Logs : `data/<env>/logs/watcher.log`, rotation quotidienne, 30 jours conservés, plus la sortie d'erreur
  (capturée par le cron de l'hôte).
- Healthchecks.io : `/start` en début de run, URL simple si tout est OK, `/fail` sinon. Un ping en échec
  est loggé mais ne fait jamais échouer le run : l'absence de ping suffit à déclencher l'alerte côté Healthchecks.
"""

from __future__ import annotations

import logging
import sys
import uuid
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s : %(message)s"
LOG_RETENTION_DAYS = 30
USER_AGENT = "ai-agent-market-watcher/0.1 (+healthchecks)"
PING_BODY_MAX_CHARS = 10_000     # Healthchecks tronque les corps de ping trop longs

_HANDLER_MARK = "_watcher_handler"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    """Configure le logger racine. Idempotent : les handlers posés par un appel précédent sont remplacés."""
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = TimedRotatingFileHandler(
        log_dir / "watcher.log", when="midnight", backupCount=LOG_RETENTION_DAYS, encoding="utf-8"
    )
    stream_handler = logging.StreamHandler(sys.stderr)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(level)

    # Bibliothèques trop bavardes au niveau INFO (une ligne par requête HTTP).
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Healthchecks:
    """Client de ping Healthchecks.io. Ne lève jamais d'exception."""

    def __init__(
        self,
        url: str | None,
        enabled: bool = True,
        *,
        timeout: float = 10.0,
        retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        self._url = (url or "").rstrip("/")
        self.enabled = enabled and bool(self._url)
        self._timeout = timeout
        self._retries = retries
        self._client = client
        self.run_id = str(uuid.uuid4())   # relie /start et le ping de fin (calcul de durée côté Healthchecks)

    def start(self) -> None:
        self._ping("/start")

    def success(self, body: str = "") -> None:
        self._ping("", body)

    def fail(self, body: str) -> None:
        self._ping("/fail", body)

    def _ping(self, suffix: str, body: str = "") -> None:
        if not self.enabled:
            return
        url = f"{self._url}{suffix}"
        payload = body[:PING_BODY_MAX_CHARS].encode("utf-8")
        client = self._client or httpx.Client(timeout=self._timeout, headers={"User-Agent": USER_AGENT})
        try:
            for attempt in range(1, self._retries + 2):
                try:
                    response = client.post(url, params={"rid": self.run_id}, content=payload)
                    response.raise_for_status()
                    return
                except httpx.HTTPError as exc:
                    log.warning("ping Healthchecks %s en échec (tentative %d) : %s", suffix or "/", attempt, exc)
            log.error("ping Healthchecks %s abandonné après %d tentatives", suffix or "/", self._retries + 1)
        finally:
            if self._client is None:
                client.close()
