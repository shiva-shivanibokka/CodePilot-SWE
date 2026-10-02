"""
Ranked keyword search over a repository — the `search_code` tool's engine.

Ported from Autonomous-SWE-Agent (`agent/tools/search.py`). The agent uses it to
find where an issue's vocabulary lives before reading files: a regular
expression (`search`) answers "where does this exact string appear", BM25
answers "which chunks are most about these words", which is the more useful
question when all you have is an issue written in prose.

What changed in the port, and why:

* **Files come from the Workspace**, not from `find` and `cat` through a shell.
  The original shelled out once per file, which is slow, POSIX-only, and
  ignored .gitignore.
* **BM25 only.** The original optionally blended in sentence-transformer
  embeddings when that package happened to be installed. A tool whose ranking
  depends on what is installed on the machine makes two benchmark runs
  incomparable, and the package pulls in PyTorch, so the optional half was
  left out rather than carried as a silent variable.
* **The cache is invalidated by edits.** The original built the index once per
  task and kept answering from it after the agent had changed the files. Here
  the cache lives on the ToolContext and `edit_file` / `write_file` clear it.
  The original's fix for a different bug is kept: the cache key includes the
  file pattern, because an index built for `test_*.py` cannot answer a search
  over everything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

#: Lines per chunk. A chunk is the unit that is ranked and shown.
CHUNK_LINES = 30
#: Characters of each hit shown to the model.
SNIPPET_CHARS = 400
#: Files indexed at most. A repository larger than this is searched over the
#: first files in sorted order, and the result says so.
MAX_FILES = 3000
#: Bytes; larger files are skipped (generated code, data, vendored bundles).
MAX_FILE_BYTES = 400_000


def tokenize(text: str) -> list[str]:
    """Split code on non-identifier characters, lowercase, drop 1-char tokens.

    Identifiers are also split on underscores and camelCase so that a query for
    "file mode" matches `from_file(mode=...)` and `FileMode`.
    """
    raw = [t for t in re.split(r"[^A-Za-z0-9_]", text) if t]
    tokens: list[str] = []
    for t in raw:
        low = t.lower()
        if len(low) > 1:
            tokens.append(low)
        parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", t.replace("_", " "))
        if len(parts) > 1:
            tokens.extend(p.lower() for p in parts if len(p) > 1)
    return tokens


@dataclass
class Chunk:
    path: str
    start_line: int
    text: str


@dataclass
class SearchIndex:
    chunks: list[Chunk] = field(default_factory=list)
    bm25: object | None = None
    tokens: list[set[str]] = field(default_factory=list)
    truncated: bool = False

    @classmethod
    def build(cls, root, files: list[str], file_pattern: str | None = None) -> SearchIndex:
        from rank_bm25 import BM25Plus

        if file_pattern:
            chosen = [
                f for f in files
                if fnmatch(f, file_pattern) or fnmatch(f.rsplit("/", 1)[-1], file_pattern)
            ]
        else:
            chosen = [f for f in files if f.endswith(".py")]
        truncated = len(chosen) > MAX_FILES
        chunks: list[Chunk] = []
        for rel in chosen[:MAX_FILES]:
            path = root / rel
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i in range(0, len(lines), CHUNK_LINES):
                text = "\n".join(lines[i : i + CHUNK_LINES])
                if text.strip():
                    chunks.append(Chunk(rel, i + 1, text))
        if not chunks:
            return cls(truncated=truncated)
        tokenized = [tokenize(f"{c.path}\n{c.text}") for c in chunks]
        return cls(
            chunks=chunks,
            # BM25+ rather than the original's BM25Okapi: Okapi's IDF goes
            # negative for a term in more than half the chunks, so on a small
            # repository (or a narrow file_pattern) a perfectly good match
            # scored zero and the search reported nothing.
            bm25=BM25Plus(tokenized),
            tokens=[set(t) for t in tokenized],
            truncated=truncated,
        )

    def search(self, query: str, top_k: int = 10) -> list[tuple[float, Chunk]]:
        if not self.chunks or self.bm25 is None:
            return []
        wanted = set(tokenize(query))
        scores = list(self.bm25.get_scores(list(wanted)))
        # BM25+ gives every chunk a small floor score; only chunks that contain
        # at least one query token are hits.
        candidates = [i for i in range(len(scores)) if self.tokens[i] & wanted]
        if not candidates:
            return []
        best = max(scores[i] for i in candidates)
        ranked = sorted(candidates, key=lambda i: scores[i], reverse=True)
        return [(scores[i] / best, self.chunks[i]) for i in ranked[:top_k]]


def render(query: str, hits: list[tuple[float, Chunk]], truncated: bool) -> str:
    if not hits:
        return f"No results for {query!r}."
    out = [f"Top {len(hits)} chunks for {query!r}:\n"]
    for n, (score, chunk) in enumerate(hits, 1):
        out.append(
            f"[{n}] {chunk.path}:{chunk.start_line}  (score {score:.2f})\n"
            f"{chunk.text[:SNIPPET_CHARS]}\n"
        )
    if truncated:
        out.append(f"(only the first {MAX_FILES} matching files were indexed)")
    return "\n".join(out)
