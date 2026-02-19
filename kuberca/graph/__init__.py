"""Resource dependency graph for cross-resource traversal.

Provides in-memory graph built from K8s API declared configuration
(ownerReferences, PVC->PV bindings, Service->Endpoint selectors,
ConfigMap/Secret volume mounts, Pod->Node assignments, ResourceQuota scoping).
"""

from kuberca.graph.dependency_graph import DependencyGraph
from kuberca.graph.models import DependencyResult, EdgeType, GraphEdge, GraphNode

__all__ = [
    "DependencyGraph",
    "DependencyResult",
    "EdgeType",
    "GraphEdge",
    "GraphNode",
]
