"""Data structures for the resource dependency graph."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class EdgeType(StrEnum):
    """Types of relationships between Kubernetes resources."""

    OWNER_REFERENCE = "owner_reference"
    VOLUME_BINDING = "volume_binding"
    CONFIG_REFERENCE = "config_reference"
    SERVICE_SELECTOR = "service_selector"
    NODE_ASSIGNMENT = "node_assignment"
    QUOTA_SCOPE = "quota_scope"


@dataclass(frozen=True)
class GraphNode:
    """A node in the dependency graph representing a Kubernetes resource."""

    kind: str
    namespace: str
    name: str
    uid: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the unique key for this node."""
        return (self.kind, self.namespace, self.name)


@dataclass(frozen=True)
class GraphEdge:
    """A typed edge between two nodes in the dependency graph."""

    source: GraphNode
    target: GraphNode
    edge_type: EdgeType
    source_field: str  # JSONPath of the field that creates this relationship
    discovered_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass
class DependencyResult:
    """Result of a graph traversal query."""

    resources: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    depth_reached: int = 0
    truncated: bool = False  # True if max_depth hit before exhausting graph
