"""Local SQLite corpus -- every result ever returned, kept forever.

This is the only compounding asset in the design. Two reasons it exists:

1. Longitudinal questions. "What changed since March" is unanswerable against a
   live search API and trivial against an accumulated corpus.
2. Insurance. Third-party providers disappear (SocialData.tools did). Anything
   already pulled stays mine -- a shutdown costs future queries, not history.

Write-through, never a cache: results are always returned fresh from the
provider. The corpus records what was seen and when, it does not serve reads
back to the model. That keeps it honest -- no stale answers dressed up as live.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .providers.base import Result

DEFAULT_PATH = Path(os.environ.get("RESEARCH_CORPUS", Path.home() / ".research-mcp" / "corpus.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    fingerprint   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    source_class  TEXT NOT NULL,
    title         TEXT,
    url           TEXT,
    text          TEXT,
    author        TEXT,
    published_at  TEXT,
    score         INTEGER,
    comments      INTEGER,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_class   ON results(source_class);
CREATE INDEX IF NOT EXISTS idx_results_seen    ON results(first_seen);
CREATE INDEX IF NOT EXISTS idx_results_pub     ON results(published_at);

CREATE TABLE IF NOT EXISTS searches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    tool       TEXT NOT NULL,
    question   TEXT NOT NULL,
    queries    TEXT NOT NULL,   -- json: the fanned-out queries actually issued
    providers  TEXT NOT NULL,   -- json: {provider: n_results | "error: ..."}
    n_results  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_searches_ts ON searches(ts);

-- Which results a given search surfaced. Makes "what did this question look
-- like in March vs now" a single join.
CREATE TABLE IF NOT EXISTS search_results (
    search_id   INTEGER NOT NULL REFERENCES searches(id),
    fingerprint TEXT NOT NULL REFERENCES results(fingerprint),
    PRIMARY KEY (search_id, fingerprint)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Corpus:
    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.executescript(SCHEMA)
            c.commit()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def record(
        self,
        *,
        tool: str,
        question: str,
        queries: list[str],
        providers: dict[str, object],
        results: list[Result],
    ) -> int:
        ts = _now()
        with closing(self._conn()) as c:
            cur = c.execute(
                "INSERT INTO searches (ts, tool, question, queries, providers, n_results)"
                " VALUES (?,?,?,?,?,?)",
                (ts, tool, question, json.dumps(queries), json.dumps(providers, default=str), len(results)),
            )
            sid = cur.lastrowid
            for r in results:
                row = r.to_row()
                # Upsert: keep the earliest first_seen, refresh volatile fields.
                c.execute(
                    """
                    INSERT INTO results (fingerprint, source, source_class, title, url, text,
                                         author, published_at, score, comments, first_seen, last_seen)
                    VALUES (:fingerprint,:source,:source_class,:title,:url,:text,
                            :author,:published_at,:score,:comments,:ts,:ts)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        last_seen = :ts,
                        score     = COALESCE(excluded.score, results.score),
                        comments  = COALESCE(excluded.comments, results.comments)
                    """,
                    {**row, "ts": ts},
                )
                c.execute(
                    "INSERT OR IGNORE INTO search_results (search_id, fingerprint) VALUES (?,?)",
                    (sid, r.fingerprint),
                )
            c.commit()
        return sid

    def stats(self) -> dict[str, object]:
        with closing(self._conn()) as c:
            n_res = c.execute("SELECT COUNT(*) FROM results").fetchone()[0]
            n_src = c.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
            by_class = dict(
                c.execute("SELECT source_class, COUNT(*) FROM results GROUP BY 1 ORDER BY 2 DESC").fetchall()
            )
            span = c.execute("SELECT MIN(ts), MAX(ts) FROM searches").fetchone()
        return {
            "db": str(self.path),
            "results": n_res,
            "searches": n_src,
            "by_class": by_class,
            "first_search": span[0],
            "last_search": span[1],
        }
