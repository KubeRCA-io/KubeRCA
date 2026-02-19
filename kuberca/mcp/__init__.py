"""MCP (Model Context Protocol) server for KubeRCA.

Exposes:
    MCPServer -- stdio transport server with k8s_cluster_status and
                 k8s_analyze_incident tools.
"""

from kuberca.mcp.server import MCPServer

__all__ = ["MCPServer"]
