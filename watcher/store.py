"""Persistance SQLite (cadrage §10) : schéma, migrations simples, accès.

La connexion est en autocommit (`isolation_level=None`) : chaque écriture hors transaction est immédiatement
durable, et `transaction()` ouvre une transaction explicite. Le pipeline traite chaque agent dans une transaction :
`seen_items`, `events`, `source_state`... ne sont commités que si l'agent a réussi.

Horodatages : ISO 8601 en UTC. Dates métier (`event_date`...) : ISO 8601 sans heure.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from watcher.models import Alert, NewsItem

log = logging.getLogger(__name__)

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS seen_items (
    agent_id        TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    published_at    TEXT,
    triage_relevant INTEGER,              -- NULL si non trié (baseline)
    first_seen_at   TEXT NOT NULL,
    PRIMARY KEY (agent_id, item_id)
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    rule_id         TEXT NOT NULL,        -- ID affiché
    source_rule_id  TEXT NOT NULL,        -- ID de la règle en config (unless_fired)
    origin          TEXT NOT NULL,        -- event | price | arm | time | anomaly
    action          TEXT NOT NULL,
    severity        TEXT NOT NULL,
    is_rumor        INTEGER NOT NULL,
    dedup_key       TEXT NOT NULL,
    event_date      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    sent_at         TEXT,                 -- NULL = en attente d'envoi (outbox)
    payload_json    TEXT NOT NULL         -- Alert sérialisée
);
CREATE INDEX IF NOT EXISTS idx_events_dedup ON events (agent_id, dedup_key, created_at);

CREATE TABLE IF NOT EXISTS armed_watches (
    id              INTEGER PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    arm_id          TEXT NOT NULL,
    source_event_id INTEGER NOT NULL REFERENCES events(id),
    metric          TEXT NOT NULL,
    ref_value       REAL NOT NULL,
    op              TEXT NOT NULL,
    threshold       REAL NOT NULL,
    action          TEXT NOT NULL,
    severity        TEXT NOT NULL,
    status          TEXT NOT NULL,        -- active | fired | cancelled
    created_at      TEXT NOT NULL,
    closed_at       TEXT
);

CREATE TABLE IF NOT EXISTS source_state (
    agent_id        TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    state_json      TEXT NOT NULL,        -- instantané ClinicalTrials, dernière clôture traitée...
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (agent_id, source_name)
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY,
    env             TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,        -- running | ok | partial | failed
    agents_ok       INTEGER NOT NULL DEFAULT 0,
    agents_failed   INTEGER NOT NULL DEFAULT 0,
    alerts_created  INTEGER NOT NULL DEFAULT 0,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    errors_json     TEXT
);

CREATE TABLE IF NOT EXISTS config_errors (
    agent_id        TEXT NOT NULL,
    error_hash      TEXT NOT NULL,
    message         TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    notified_at     TEXT,
    resolved_at     TEXT,
    PRIMARY KEY (agent_id, error_hash)
);
"""

# version → script. Ajouter une migration = ajouter une entrée (jamais modifier une entrée existante).
MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1}
SCHEMA_VERSION = max(MIGRATIONS)


class StoreError(Exception):
    """Base incompatible ou corrompue."""


def ts(moment: datetime) -> str:
    """Horodatage ISO en UTC ; refuse les datetimes naïfs (fuseau ambigu)."""
    if moment.tzinfo is None:
        raise ValueError("datetime sans fuseau horaire")
    return moment.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ConfigErrorRow:
    agent_id: str
    error_hash: str
    message: str
    first_seen_at: str
    notified_at: str | None


@dataclass(frozen=True)
class EventRow:
    id: int
    agent_id: str
    dedup_key: str
    created_at: str
    sent_at: str | None
    alert: Alert


@dataclass(frozen=True)
class RunTotals:
    status: str                 # ok | partial | failed
    agents_ok: int = 0
    agents_failed: int = 0
    alerts_created: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    errors: dict[str, str] | None = None


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: Path | str) -> Store:
        """Ouvre (et crée si besoin) la base, puis applique les migrations en attente."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        store = cls(conn)
        try:
            store.migrate()
        except Exception:
            conn.close()
            raise
        return store

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ schéma

    def schema_version(self) -> int:
        self._conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return int(row[0] or 0)

    def migrate(self) -> None:
        current = self.schema_version()
        if current > SCHEMA_VERSION:
            raise StoreError(f"base en version {current}, plus récente que le code ({SCHEMA_VERSION})")
        for version in sorted(v for v in MIGRATIONS if v > current):
            # executescript valide toute transaction en cours : BEGIN / COMMIT sont donc dans le script.
            script = f"BEGIN;\n{MIGRATIONS[version]}\nINSERT INTO schema_version (version) VALUES ({version});\nCOMMIT;"
            try:
                self._conn.executescript(script)
            except sqlite3.Error:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            log.info("schéma SQLite migré en version %d", version)

    # ------------------------------------------------------------------ transactions

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Transaction explicite : tout est commité à la sortie normale, tout est annulé sur exception."""
        if self._conn.in_transaction:
            raise StoreError("transaction déjà ouverte (les transactions imbriquées ne sont pas supportées)")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ runs

    def start_run(self, env: str, started_at: datetime) -> int:
        cur = self._conn.execute(
            "INSERT INTO runs (env, started_at, status) VALUES (?, ?, 'running')",
            (env, ts(started_at)),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, finished_at: datetime, totals: RunTotals) -> None:
        self._conn.execute(
            """UPDATE runs SET finished_at = ?, status = ?, agents_ok = ?, agents_failed = ?,
                   alerts_created = ?, input_tokens = ?, output_tokens = ?, errors_json = ?
               WHERE id = ?""",
            (
                ts(finished_at), totals.status, totals.agents_ok, totals.agents_failed, totals.alerts_created,
                totals.input_tokens, totals.output_tokens,
                json.dumps(totals.errors, ensure_ascii=False) if totals.errors else None,
                run_id,
            ),
        )

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    # ------------------------------------------------------------------ erreurs de configuration

    def record_config_error(self, agent_id: str, error_hash: str, message: str, now: datetime) -> bool:
        """Enregistre une erreur de config. Retourne True si elle doit encore être notifiée par mail.

        Une erreur résolue qui réapparaît est traitée comme une nouvelle détection.
        """
        row = self._conn.execute(
            "SELECT notified_at, resolved_at FROM config_errors WHERE agent_id = ? AND error_hash = ?",
            (agent_id, error_hash),
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO config_errors (agent_id, error_hash, message, first_seen_at) VALUES (?, ?, ?, ?)",
                (agent_id, error_hash, message, ts(now)),
            )
            return True
        if row["resolved_at"] is not None:
            self._conn.execute(
                """UPDATE config_errors SET first_seen_at = ?, notified_at = NULL, resolved_at = NULL
                   WHERE agent_id = ? AND error_hash = ?""",
                (ts(now), agent_id, error_hash),
            )
            return True
        return row["notified_at"] is None

    def mark_config_error_notified(self, agent_id: str, error_hash: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE config_errors SET notified_at = ? WHERE agent_id = ? AND error_hash = ?",
            (ts(now), agent_id, error_hash),
        )

    def resolve_config_errors(self, agent_id: str, now: datetime, keep: Iterable[str] = ()) -> int:
        """Marque résolues les erreurs ouvertes de l'agent, sauf celles dont l'empreinte est dans `keep`."""
        keep = list(keep)
        placeholders = ",".join("?" * len(keep))
        exclusion = f"AND error_hash NOT IN ({placeholders})" if keep else ""
        cur = self._conn.execute(
            f"UPDATE config_errors SET resolved_at = ? WHERE agent_id = ? AND resolved_at IS NULL {exclusion}",
            (ts(now), agent_id, *keep),
        )
        return cur.rowcount

    def open_config_errors(self) -> list[ConfigErrorRow]:
        rows = self._conn.execute(
            """SELECT agent_id, error_hash, message, first_seen_at, notified_at FROM config_errors
               WHERE resolved_at IS NULL ORDER BY agent_id, first_seen_at"""
        ).fetchall()
        return [ConfigErrorRow(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ documents vus

    def seen_item_ids(self, agent_id: str, item_ids: Iterable[str]) -> set[str]:
        ids = list(item_ids)
        seen: set[str] = set()
        for start in range(0, len(ids), 500):   # limite de variables SQLite
            chunk = ids[start:start + 500]
            rows = self._conn.execute(
                f"SELECT item_id FROM seen_items WHERE agent_id = ? AND item_id IN ({','.join('?' * len(chunk))})",
                (agent_id, *chunk),
            ).fetchall()
            seen.update(r["item_id"] for r in rows)
        return seen

    def mark_seen(self, items: Iterable[NewsItem], now: datetime, relevant: set[str] | None = None) -> None:
        """Marque des documents comme vus. `relevant=None` : non triés (baseline), `triage_relevant` reste NULL."""
        self._conn.executemany(
            """INSERT OR IGNORE INTO seen_items
                   (agent_id, item_id, source_name, url, title, published_at, triage_relevant, first_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    it.agent_id, it.id, it.source_name, str(it.url), it.title,
                    ts(it.published_at) if it.published_at else None,
                    None if relevant is None else int(it.id in relevant),
                    ts(now),
                )
                for it in items
            ],
        )

    # ------------------------------------------------------------------ état des sources

    def get_source_state(self, agent_id: str, source_name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT state_json FROM source_state WHERE agent_id = ? AND source_name = ?",
            (agent_id, source_name),
        ).fetchone()
        return None if row is None else json.loads(row["state_json"])

    def set_source_state(self, agent_id: str, source_name: str, state: dict[str, Any], now: datetime) -> None:
        self._conn.execute(
            """INSERT INTO source_state (agent_id, source_name, state_json, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (agent_id, source_name) DO UPDATE SET state_json = excluded.state_json,
                                                                 updated_at = excluded.updated_at""",
            (agent_id, source_name, json.dumps(state, ensure_ascii=False, sort_keys=True), ts(now)),
        )

    # ------------------------------------------------------------------ événements (outbox)

    def fired_rule_ids(self, agent_id: str) -> set[str]:
        """Historique complet des règles déclenchées (sert à `unless_fired`)."""
        rows = self._conn.execute(
            "SELECT DISTINCT source_rule_id FROM events WHERE agent_id = ?", (agent_id,)
        ).fetchall()
        return {r["source_rule_id"] for r in rows}

    def insert_event(self, alert: Alert, dedup_key: str, now: datetime) -> int:
        """Écrit une alerte dans l'outbox (`sent_at = NULL`)."""
        cur = self._conn.execute(
            """INSERT INTO events (agent_id, rule_id, source_rule_id, origin, action, severity, is_rumor,
                                   dedup_key, event_date, created_at, sent_at, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (
                alert.agent_id, alert.rule_id, alert.source_rule_id, alert.origin, alert.action.value,
                alert.severity.value, int(alert.is_rumor), dedup_key, alert.event_date.isoformat(), ts(now),
                alert.model_dump_json(),
            ),
        )
        return int(cur.lastrowid)

    def pending_events(self) -> list[EventRow]:
        """Toutes les alertes non envoyées, y compris celles des runs précédents."""
        rows = self._conn.execute(
            """SELECT id, agent_id, dedup_key, created_at, sent_at, payload_json FROM events
               WHERE sent_at IS NULL ORDER BY created_at, id"""
        ).fetchall()
        return [
            EventRow(
                id=r["id"], agent_id=r["agent_id"], dedup_key=r["dedup_key"], created_at=r["created_at"],
                sent_at=r["sent_at"], alert=Alert.model_validate_json(r["payload_json"]),
            )
            for r in rows
        ]

    def mark_sent(self, event_ids: Iterable[int], now: datetime) -> None:
        self._conn.executemany(
            "UPDATE events SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
            [(ts(now), event_id) for event_id in event_ids],
        )
