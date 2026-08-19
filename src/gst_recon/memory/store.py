"""Exception memory: resolved cases become retrievable precedents.

The compounding claim this project actually tests: if a vendor's invoice-number
quirk broke matching last month and a human confirmed the resolution, the agent
should find that in one lookup rather than rediscovering it in five probes.

Two backends behind one interface. The Postgres/pgvector one is what a
deployment uses; the in-memory one is what the tests and the offline
experiments use, so the memory-compounding curve can be reproduced from a
clean clone with no database running.

Only human-confirmed resolutions are stored. A precedent store fed by the
agent's own unreviewed conclusions would compound its mistakes just as
efficiently as its successes -- the memory would get more confident without
getting more correct, which is worse than having no memory at all.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Protocol

from gst_recon.domain.taxonomy import ExceptionClass

EMBEDDING_DIMENSIONS = 256


class Embedder(Protocol):
    dimensions: int

    def embed(self, text: str) -> list[float]: ...


@dataclass(slots=True)
class HashEmbedder:
    """Deterministic bag-of-tokens embedding. No network, no model.

    Good enough for the retrieval this system does, which is finding prior
    cases with the same exception class and overlapping vocabulary -- not
    semantic paraphrase. Being deterministic matters more here than being
    clever: the memory-compounding curve has to be reproducible, and an
    embedding that drifts with a model version would make it unrepeatable.
    """

    dimensions: int = EMBEDDING_DIMENSIONS

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = [token for token in text.upper().replace("/", " ").split() if token]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


@dataclass(frozen=True, slots=True)
class Precedent:
    """One resolved case, retained because a human agreed with the outcome."""

    precedent_id: str
    exception_class: ExceptionClass
    gstin: str
    summary: str
    probe_sequence: tuple[str, ...]
    resolution: str
    human_confirmed: bool
    cycle: int

    def as_hint(self) -> str:
        probes = " -> ".join(self.probe_sequence) or "(none)"
        return (
            f"[{self.exception_class.value}] {self.summary}\n"
            f"  probes: {probes}\n  outcome: {self.resolution}"
        )


@dataclass(slots=True)
class InMemoryPrecedentStore:
    """Reference implementation. Same interface as the Postgres one."""

    embedder: Embedder = field(default_factory=HashEmbedder)
    top_k: int = 3
    _rows: list[tuple[Precedent, list[float]]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self._rows)

    def _text(self, exception_class: str, gstin: str, summary: str = "") -> str:
        return f"{exception_class} {gstin} {summary}"

    def add(self, precedent: Precedent) -> None:
        if not precedent.human_confirmed:
            # Storing unreviewed agent output would let the store compound its
            # own errors; a wrong precedent is retrieved just as readily as a
            # right one and carries the same apparent authority.
            return
        vector = self.embedder.embed(
            self._text(precedent.exception_class.value, precedent.gstin, precedent.summary)
        )
        self._rows.append((precedent, vector))

    def search(self, *, exception_class: str, gstin: str = "") -> dict[str, object]:
        if not self._rows:
            return {"precedent_count": 0, "precedents": []}
        query = self.embedder.embed(self._text(exception_class, gstin))
        scored = sorted(
            (
                (cosine(query, vector), precedent)
                for precedent, vector in self._rows
                # Class is an exact filter rather than a soft signal: a
                # precedent from a different class is not a weaker match, it is
                # a different question.
                if precedent.exception_class.value == exception_class
            ),
            key=lambda row: (-row[0], row[1].precedent_id),
        )[: self.top_k]
        return {
            "precedent_count": len(scored),
            "precedents": [
                {
                    "precedent_id": precedent.precedent_id,
                    "similarity": round(score, 4),
                    "summary": precedent.summary,
                    "probe_sequence": list(precedent.probe_sequence),
                    "resolution": precedent.resolution,
                    "same_vendor": precedent.gstin == gstin,
                }
                for score, precedent in scored
            ],
        }

    def hint_for(self, exception_class: str, gstin: str = "") -> str:
        found = self.search(exception_class=exception_class, gstin=gstin)
        rows = found["precedents"]
        if not rows:
            return ""
        lines = []
        for row in rows:  # type: ignore[union-attr]
            probes = " -> ".join(row["probe_sequence"]) or "(none)"
            lines.append(f"- {row['summary']} (probes: {probes}; outcome: {row['resolution']})")
        return "\n".join(lines)


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS precedents (
    precedent_id     TEXT PRIMARY KEY,
    exception_class  TEXT NOT NULL,
    gstin            TEXT NOT NULL,
    summary          TEXT NOT NULL,
    probe_sequence   TEXT NOT NULL,
    resolution       TEXT NOT NULL,
    human_confirmed  BOOLEAN NOT NULL,
    cycle            INTEGER NOT NULL,
    embedding        VECTOR(256) NOT NULL
);
CREATE INDEX IF NOT EXISTS precedents_class_idx ON precedents (exception_class);
"""


@dataclass(slots=True)
class PostgresPrecedentStore:
    """pgvector-backed store, used when a deployment actually has a database.

    Kept in the same Postgres instance the durable workflow already needs.
    Below a few million vectors a second datastore buys nothing and costs an
    extra thing to operate.
    """

    connection_url: str
    embedder: Embedder = field(default_factory=HashEmbedder)
    top_k: int = 3

    def _connect(self):
        # Imported lazily and deliberately: the offline experiments and the
        # whole test suite must run with no database and no driver present.
        import psycopg  # noqa: PLC0415

        return psycopg.connect(self.connection_url)

    def ensure_schema(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(SCHEMA)
            connection.commit()

    def add(self, precedent: Precedent) -> None:
        if not precedent.human_confirmed:
            return
        vector = self.embedder.embed(
            f"{precedent.exception_class.value} {precedent.gstin} {precedent.summary}"
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO precedents (precedent_id, exception_class, gstin, summary,
                                        probe_sequence, resolution, human_confirmed,
                                        cycle, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (precedent_id) DO NOTHING
                """,
                (
                    precedent.precedent_id,
                    precedent.exception_class.value,
                    precedent.gstin,
                    precedent.summary,
                    " -> ".join(precedent.probe_sequence),
                    precedent.resolution,
                    precedent.human_confirmed,
                    precedent.cycle,
                    str(vector),
                ),
            )
            connection.commit()

    def search(self, *, exception_class: str, gstin: str = "") -> dict[str, object]:
        query = str(self.embedder.embed(f"{exception_class} {gstin}"))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT precedent_id, summary, probe_sequence, resolution, gstin,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM precedents
                WHERE exception_class = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query, exception_class, query, self.top_k),
            )
            rows = cursor.fetchall()
        return {
            "precedent_count": len(rows),
            "precedents": [
                {
                    "precedent_id": row[0],
                    "summary": row[1],
                    "probe_sequence": row[2].split(" -> ") if row[2] else [],
                    "resolution": row[3],
                    "same_vendor": row[4] == gstin,
                    "similarity": round(float(row[5]), 4),
                }
                for row in rows
            ],
        }
