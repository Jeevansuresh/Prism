"""Prism - Engineering Portfolio Intelligence.

Build an evidence-backed graph of what one person has built in a repository:
their commits, the files and symbols they actually wrote, and the technologies
they demonstrably used - every relationship carrying the commit or source
location that justifies it.

Phase 1 is deterministic. No language models, no embeddings, no network.
"""

__version__ = "0.1.0"

from .pipeline import BuildOptions, BuildOutcome, build
from .query import PortfolioGraph

__all__ = ["build", "BuildOptions", "BuildOutcome", "PortfolioGraph", "__version__"]
