"""
search_codebase tool — BM25 + embedding hybrid search over repo files.

The agent uses this to find relevant files before reading them. Prevents
the most expensive failure mode: reading irrelevant files and filling the
context window with noise.

Architecture:
- BM25 over tokenised file contents (fast, keyword-based, good for identifiers)
- Sentence-transformer embeddings for semantic similarity (good for concepts)
- Results are merged: BM25_score * 0.6 + embedding_score * 0.4
- Returns top-k results with file path, line number, and a context snippet

The index is built once per task (on first search call) and cached in memory.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from rank_bm25 import BM25Okapi

from observability.metrics import metrics
from observability.tracing import get_tracer
from sandbox.docker_workspace import DockerWorkspace

tracer = get_tracer(__name__)

SEARCH_TOOL_SCHEMA: dict[str, Any] = {
    "name": "search_codebase",
    "description": (
        "Search the repository for files and code relevant to a query.\n\n"
        "Uses BM25 keyword search combined with semantic similarity to find "
        "the most relevant files and functions. Returns file paths, line numbers, "
        "and surrounding context.\n\n"
        "Use this BEFORE reading files — it is much cheaper to search first "
        "than to read every file in the repo.\n\n"
        "Examples:\n"
        '  {"query": "RidgeClassifierCV store_cv_values parameter"}\n'
        '  {"query": "fit method linear model base class"}\n'
        '  {"query": "test_ridge_classifier", "file_pattern": "test_*.py"}\n'
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language or code query to search for.",
            },
            "file_pattern": {
                "type": "string",
                "description": (
                    "Optional glob pattern to restrict search to specific files. "
                    "E.g. '*.py', 'test_*.py', 'src/*.py'"
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return. Default: 10.",
            },
        },
        "required": ["query"],
    },
}

# Weight for BM25 vs embedding scores in the final ranking
BM25_WEIGHT = 0.6
EMBED_WEIGHT = 0.4

# Maximum characters of context to show per search result
SNIPPET_CHARS = 400


@dataclass
class SearchResult:
    filepath: str
    start_line: int
    snippet: str
    bm25_score: float = 0.0
    embed_score: float = 0.0
    combined_score: float = 0.0


@dataclass
class RepoIndex:
    """In-memory index of a repo's file contents, built once per task."""

    chunks: list[dict] = field(default_factory=list)  # {filepath, start_line, text, tokens}
    bm25: BM25Okapi | None = None
    embeddings: np.ndarray | None = None
    embed_model: Any = None

    @classmethod
    def build(cls, workspace: DockerWorkspace, file_pattern: str | None = None) -> RepoIndex:
        """Build the index by reading all Python files from the workspace."""
        with tracer.start_as_current_span("search.build_index"):
            # Get list of files
            if file_pattern:
                result = workspace.run(
                    f"find /repo -name '{file_pattern}' -not -path '*/.git/*' "
                    f"-not -path '*/node_modules/*' | sort | head -500"
                )
            else:
                result = workspace.run(
                    "find /repo -name '*.py' -not -path '*/.git/*' "
                    "-not -path '*/node_modules/*' | sort | head -500"
                )

            filepaths = [line.strip() for line in result.stdout.splitlines() if line.strip()]

            chunks = []
            for filepath in filepaths:
                content_result = workspace.run(f"cat '{filepath}' 2>/dev/null")
                if not content_result.success or not content_result.stdout:
                    continue

                content = content_result.stdout
                lines = content.splitlines()

                # Chunk by function/class boundaries (every ~30 lines)
                chunk_size = 30
                for i in range(0, len(lines), chunk_size):
                    chunk_lines = lines[i : i + chunk_size]
                    text = "\n".join(chunk_lines)
                    tokens = _tokenize(text)
                    chunks.append(
                        {
                            "filepath": filepath,
                            "start_line": i + 1,
                            "text": text,
                            "tokens": tokens,
                        }
                    )

            if not chunks:
                return cls()

            # Build BM25 index
            corpus = [c["tokens"] for c in chunks]
            bm25 = BM25Okapi(corpus)

            # Build embeddings (lazy — only if sentence-transformers is available)
            embeddings = None
            embed_model = None
            try:
                from sentence_transformers import SentenceTransformer

                embed_model = SentenceTransformer("all-MiniLM-L6-v2")
                texts = [c["text"][:512] for c in chunks]
                embeddings = embed_model.encode(texts, show_progress_bar=False)
            except Exception:
                pass  # Fall back to BM25-only if transformers not available

            return cls(
                chunks=chunks,
                bm25=bm25,
                embeddings=embeddings,
                embed_model=embed_model,
            )


def _tokenize(text: str) -> list[str]:
    """Tokenize code text for BM25: split on non-alphanumeric, lowercase, remove empties."""
    tokens = re.split(r"[^a-zA-Z0-9_]", text.lower())
    return [t for t in tokens if len(t) > 1]


# Per-task index cache: task_id -> RepoIndex
_index_cache: dict[str, RepoIndex] = {}


def run_search(
    workspace: DockerWorkspace,
    query: str,
    file_pattern: str | None = None,
    top_k: int = 10,
    task_id: str | None = None,
) -> str:
    """
    Search the codebase and return formatted results for the LLM.

    Args:
        workspace:    Active DockerWorkspace.
        query:        The search query.
        file_pattern: Optional glob to restrict files.
        top_k:        Number of results to return.
        task_id:      Used for index caching.

    Returns:
        Formatted string of search results for the LLM.
    """
    t0 = time.monotonic()
    cache_key = task_id or workspace.task_id

    with tracer.start_as_current_span("tool.search") as span:
        span.set_attribute("query", query)
        span.set_attribute("top_k", top_k)

        # Build or retrieve index
        if cache_key not in _index_cache:
            _index_cache[cache_key] = RepoIndex.build(workspace, file_pattern)

        index = _index_cache[cache_key]

        if not index.chunks:
            return "No indexable files found in repository."

        results = _search(index, query, top_k)
        duration_ms = int((time.monotonic() - t0) * 1000)

        metrics.tool_called("search_codebase", duration_ms=duration_ms, error=False)
        span.set_attribute("num_results", len(results))
        span.set_attribute("duration_ms", duration_ms)

    if not results:
        return f"No results found for query: {query!r}"

    lines = [f"Search results for: {query!r} (top {len(results)})\n"]
    for i, r in enumerate(results, 1):
        lines.append(
            f"[{i}] {r.filepath}:{r.start_line}  (score={r.combined_score:.3f})\n"
            f"```\n{r.snippet[:SNIPPET_CHARS]}\n```\n"
        )

    return "\n".join(lines)


def _search(index: RepoIndex, query: str, top_k: int) -> list[SearchResult]:
    """Hybrid BM25 + embedding search over the index."""
    query_tokens = _tokenize(query)
    bm25_scores = np.array(index.bm25.get_scores(query_tokens))

    # Normalise BM25 scores to [0, 1]
    bm25_max = bm25_scores.max()
    if bm25_max > 0:
        bm25_norm = bm25_scores / bm25_max
    else:
        bm25_norm = bm25_scores

    # Embedding scores
    if index.embeddings is not None and index.embed_model is not None:
        query_emb = index.embed_model.encode([query], show_progress_bar=False)
        # Cosine similarity
        norms = np.linalg.norm(index.embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        normalised = index.embeddings / norms
        query_norm = query_emb / (np.linalg.norm(query_emb) + 1e-9)
        embed_scores = (normalised @ query_norm.T).flatten()
        embed_scores = np.clip(embed_scores, 0, 1)  # cosine → [0,1]
        combined = BM25_WEIGHT * bm25_norm + EMBED_WEIGHT * embed_scores
    else:
        embed_scores = np.zeros_like(bm25_norm)
        combined = bm25_norm

    # Get top-k indices
    top_indices = np.argsort(combined)[::-1][:top_k]

    results = []
    for idx in top_indices:
        if combined[idx] < 0.01:
            continue
        chunk = index.chunks[idx]
        results.append(
            SearchResult(
                filepath=chunk["filepath"],
                start_line=chunk["start_line"],
                snippet=chunk["text"],
                bm25_score=float(bm25_norm[idx]),
                embed_score=float(embed_scores[idx]),
                combined_score=float(combined[idx]),
            )
        )

    return results


def clear_index(task_id: str) -> None:
    """Clear the cached index for a task. Call on task teardown."""
    _index_cache.pop(task_id, None)
