"""Knowledge-graph package for the RCA / CAPA service.

Provides an in-memory clinical-lab failure knowledge graph
(:class:`knowledge_graph.KnowledgeGraph`) and an optional Neo4j-backed
adapter (:class:`neo4j_adapter.Neo4jGraph`). Use
:func:`neo4j_adapter.get_graph` to obtain the right backend for the current
configuration.
"""

from __future__ import annotations

from .knowledge_graph import KnowledgeGraph
from .neo4j_adapter import Neo4jGraph, get_graph

__all__ = ["KnowledgeGraph", "Neo4jGraph", "get_graph"]
