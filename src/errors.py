"""Error hierarchy. Rule: never swallow silently — either raise, or record
into ToolRecorder.warnings so it lands in the output JSON's system.warnings."""
from __future__ import annotations


class DataAgentError(Exception):
    """Base class for all framework errors."""


class SearchError(DataAgentError):
    """Lexical/text search backend failure."""


class ParseError(DataAgentError):
    """tree-sitter / structural parse failure on a file."""


class GitError(DataAgentError):
    """git CLI failure (all git access here is read-only)."""


class GraphError(DataAgentError):
    """Semantica context-graph failure."""


class LLMError(DataAgentError):
    """LLM endpoint failure. Not-configured is *not* an error (heuristic mode);
    configured-but-broken IS — we refuse to silently degrade answer quality."""
