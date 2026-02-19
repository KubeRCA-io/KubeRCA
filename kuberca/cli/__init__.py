"""KubeRCA command-line interface.

Exposes:
    cli -- Click group entry point (registered as ``kuberca`` script).
"""

from kuberca.cli.main import cli

__all__ = ["cli"]
