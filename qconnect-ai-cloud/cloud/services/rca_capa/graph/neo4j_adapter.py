"""Optional Neo4j-backed RCA graph adapter.

The production deployment stores the failure-knowledge graph in Neo4j. The
driver is an *optional* dependency: the import is guarded so the service runs
with the pure-stdlib :class:`~knowledge_graph.KnowledgeGraph` whenever the
driver is missing or ``NEO4J_URI`` is not configured.

Use :func:`get_graph` to obtain the appropriate backend for the current
settings; tests always exercise the in-memory graph.
"""

from __future__ import annotations

from typing import Any

from .knowledge_graph import KnowledgeGraph

try:  # pragma: no cover - driver is an optional, often-absent dependency
    from neo4j import GraphDatabase  # type: ignore

    _NEO4J_IMPORTABLE = True
except Exception:  # pragma: no cover
    GraphDatabase = None  # type: ignore[assignment]
    _NEO4J_IMPORTABLE = False


class Neo4jGraph:
    """Neo4j-backed RCA graph exposing the same ``infer`` interface.

    The class mirrors :class:`KnowledgeGraph`. When the driver is unavailable or
    a connection cannot be established, :attr:`available` is ``False`` and the
    caller should fall back to the in-memory graph (``get_graph`` does this).
    """

    backend_name = "neo4j"

    def __init__(
        self,
        uri: str,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
    ) -> None:
        self._uri = uri
        self._database = database
        self._driver: Any = None
        self.available = False
        # Local twin of the seeded ruleset, used to translate Neo4j query rows
        # into the same response shape and to provide action text.
        self._fallback = KnowledgeGraph()

        if not _NEO4J_IMPORTABLE or GraphDatabase is None:
            return
        try:  # pragma: no cover - requires a live Neo4j instance
            auth = (user, password) if user is not None else None
            self._driver = GraphDatabase.driver(self._uri, auth=auth)
            self._driver.verify_connectivity()
            self.available = True
        except Exception:  # pragma: no cover
            self._driver = None
            self.available = False

    def close(self) -> None:  # pragma: no cover - requires a live driver
        """Close the underlying driver, if any."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def infer(self, symptoms: dict[str, Any]) -> list[dict[str, Any]]:  # pragma: no cover
        """Rank probable causes by querying Neo4j.

        Falls back to the in-memory graph if the driver is not usable so the
        caller always gets a well-formed ranking.
        """
        if not self.available or self._driver is None:
            return self._fallback.infer(symptoms)
        try:
            return self._query(symptoms)
        except Exception:
            return self._fallback.infer(symptoms)

    def _query(self, symptoms: dict[str, Any]) -> list[dict[str, Any]]:  # pragma: no cover
        """Run the weighted-evidence Cypher query and shape the rows.

        The Cypher mirrors the in-memory scoring: matched ``(:Symptom)-[:INDICATES
        {weight}]->(:Cause)`` edges are summed per cause and normalised by the
        cause's total edge weight. Action text is attached from related
        ``(:Action)`` nodes.
        """
        symptom_keys = [k for k, v in symptoms.items() if v not in (None, False, "")]
        cypher = (
            "MATCH (s:Symptom)-[r:INDICATES]->(c:Cause) "
            "WHERE s.key IN $keys "
            "WITH c, sum(r.weight) AS score, collect(r.rationale) AS evidence "
            "MATCH (c)-[:CORRECTED_BY]->(ca:Action) "
            "MATCH (c)-[:PREVENTED_BY]->(pa:Action) "
            "RETURN c.name AS cause, c.category AS category, "
            "c.max_score AS max_score, score, evidence, "
            "ca.text AS corrective_action, pa.text AS preventive_action "
            "ORDER BY score DESC, cause ASC"
        )
        ranked: list[dict[str, Any]] = []
        with self._driver.session(database=self._database) as session:
            for row in session.run(cypher, keys=symptom_keys):
                max_score = row["max_score"] or 1.0
                confidence = round(min(1.0, float(row["score"]) / float(max_score)), 4)
                ranked.append(
                    {
                        "cause": row["cause"],
                        "category": row["category"],
                        "confidence": confidence,
                        "evidence": list(row["evidence"]),
                        "corrective_action": row["corrective_action"],
                        "preventive_action": row["preventive_action"],
                    }
                )
        return ranked


def get_graph(settings: Any) -> KnowledgeGraph | Neo4jGraph:
    """Return the RCA graph backend for the given settings.

    Returns a :class:`Neo4jGraph` only when a ``neo4j_uri`` is configured, the
    driver is importable, and connectivity succeeds; otherwise returns the
    in-memory :class:`KnowledgeGraph`.

    ``settings`` may be any object (or ``None``) exposing optional
    ``neo4j_uri`` / ``neo4j_user`` / ``neo4j_password`` / ``neo4j_database``
    attributes.
    """
    uri = getattr(settings, "neo4j_uri", None) if settings is not None else None
    if uri and _NEO4J_IMPORTABLE:
        graph = Neo4jGraph(
            uri=uri,
            user=getattr(settings, "neo4j_user", None),
            password=getattr(settings, "neo4j_password", None),
            database=getattr(settings, "neo4j_database", None),
        )
        if graph.available:  # pragma: no cover - requires a live Neo4j instance
            return graph
    return KnowledgeGraph()
