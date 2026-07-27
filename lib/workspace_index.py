"""Incremental semantic index over the workspace repos.

Used by the ``workspace_search`` MCP tool and by ``bin/local index|search``.
The point is cross-repo recall: one question, ranked ``path:start-end``
citations from every repo in the workspace, without shipping the code to a
frontier model to find them.

Design notes worth knowing before you change anything here:

* **Storage is one SQLite file** at ``var/index.db``. Files carry an mtime,
  size and content hash, so a re-run only re-embeds what actually changed.
  Nothing else in the harness needs a vector database, and a single file is
  trivially deletable when it goes wrong.
* **Embeddings are batched.** A cold index over several repos is thousands of
  chunks; one HTTP request per chunk would make it unusable. Chunks are
  flushed to ``h.embed()`` in batches bounded by both count and characters.
* **Retrieval is hybrid.** Cosine similarity finds paraphrases; a ripgrep
  literal pass finds the exact identifier you typed. Neither alone is good
  enough on code, so the literal hits boost the vector ranking rather than
  forming a separate list.
* **Failure is loud.** If ``local-embed`` cannot be reached there is no
  degraded keyword-only mode that silently returns worse answers -- you get a
  ``HarnessError`` naming the fix.

Pure library: no argparse, no import-time side effects.
"""

from __future__ import annotations

import hashlib
import operator
import os
import shutil
import sqlite3
import subprocess
import time
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path
from typing import Callable, Iterator, Sequence

import harness_lib as h

__all__ = [
    "SearchHit",
    "IndexStats",
    "build_index",
    "search",
    "index_status",
    "db_path",
]

SCHEMA_VERSION = 1
DB_NAME = "index.db"

# Chunking. Line-based with overlap so a hit can cite real line numbers; the
# char cap stops one pathological long line from blowing up an embedding call.
CHUNK_LINES = 60
CHUNK_OVERLAP_LINES = 12
CHUNK_MAX_CHARS = 4000

# Embedding batches: bounded by both count and characters because chunk sizes
# vary wildly between prose and minified-adjacent code.
BATCH_CHUNKS = 64
BATCH_CHARS = 60_000

MAX_FILE_BYTES = 1_000_000  # ~1MB; larger is a data file, not something to read

# Directories that are never worth indexing. `git ls-files` already filters
# most of these via .gitignore; this list is what keeps the non-git fallback
# walk honest.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "target",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".nuxt",
        ".cache",
        ".gradle",
        ".terraform",
        ".idea",
        ".vscode",
        "coverage",
        "vendor",
        "Pods",
        "DerivedData",
        ".ipynb_checkpoints",
    }
)

# Runtime directories at a repo's top level. Only matched at depth 1, so a
# genuine `src/var/` or `internal/tmp/` package is still indexed. This is what
# keeps a repo that accidentally committed its own runtime state out of the
# index -- the harness's own `var/` being the obvious example.
SKIP_TOP_DIRS = frozenset({"var", "tmp", "logs"})

SKIP_NAMES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "npm-shrinkwrap.json",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "Podfile.lock",
        "go.sum",
        "flake.lock",
        "pdm.lock",
        ".DS_Store",
    }
)

SKIP_SUFFIXES = frozenset(
    {
        # archives / binaries
        ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war",
        ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".obj", ".class", ".pyc",
        ".pyo", ".wasm", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
        # media
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".icns", ".webp",
        ".svg", ".tif", ".tiff", ".psd", ".pdf", ".mp3", ".mp4", ".wav",
        ".mov", ".avi", ".mkv", ".ogg", ".webm",
        # fonts
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        # generated / vendored blobs
        ".lock", ".map", ".snap", ".pack", ".idx",
        # runtime droppings -- some repos commit these by accident, and a log
        # is noise in a semantic index even when it is tracked
        ".log", ".pid", ".tmp", ".bak", ".swp",
    }
)

# Tokens too common to be worth a literal ripgrep pass.
_STOPWORDS = frozenset(
    """a an and are as at be but by do does for from get has have how i if in
    into is it its of on or our so than that the their then there these they
    this to use used using was we what when where which who why will with
    would you your""".split()
)


class _Unavailable(Exception):
    """Internal: embeddings backend is unreachable (re-raised as HarnessError)."""


# --------------------------------------------------------------------------
# Public result types
# --------------------------------------------------------------------------


@dataclass
class SearchHit:
    """One ranked chunk. ``path`` is relative to the workspace root."""

    path: str
    repo: str
    start_line: int
    end_line: int
    score: float
    snippet: str

    @property
    def citation(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"


@dataclass
class IndexStats:
    """What one ``build_index`` run actually did.

    ``files`` / ``chunks`` count work performed this run (new or changed
    files), not the size of the whole index -- a no-op re-run reports zero,
    which is the honest answer. ``index_status()`` reports totals.
    ``skipped`` counts files examined but not re-embedded: unchanged, or
    excluded by the filters.
    """

    files: int = 0
    chunks: int = 0
    skipped: int = 0
    duration_s: float = 0.0


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def db_path() -> Path:
    """Location of the index database (``var/index.db``)."""
    return h.var_dir() / DB_NAME


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL UNIQUE,   -- workspace-relative, posix separators
    repo       TEXT NOT NULL,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    hash       TEXT NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_repo ON files(repo);
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    text       TEXT NOT NULL,
    vector     BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
"""


def _connect(create: bool = True) -> sqlite3.Connection:
    path = db_path()
    if not create and not path.exists():
        raise h.HarnessError(
            f"no workspace index at {path}. Build one with `bin/local index` "
            "(the workspace_search MCP tool builds it on first use)."
        )
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def _meta_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _resolve_workspace(workspace_root: Path) -> Path:
    root = Path(workspace_root).expanduser()
    if not root.is_dir():
        raise h.HarnessError(
            f"workspace root does not exist: {root}. Fix `workspace_root` in "
            "config/harness.yaml, or run `bin/init`."
        )
    return root.resolve()


def _discover_repos(root: Path, repos: Sequence[str] | None) -> list[tuple[str, Path]]:
    """Repo name -> directory. A workspace that is itself a repo counts as one."""
    found: list[tuple[str, Path]] = []
    if (root / ".git").exists():
        found.append((root.name, root))
    else:
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and not entry.name.startswith(".") and not entry.is_symlink():
                found.append((entry.name, entry))
    if not found:
        raise h.HarnessError(
            f"no repos found under {root}. Clone them with `bin/sync-workspace`, "
            "or point `workspace_root` at the right directory."
        )
    if repos:
        wanted = {r.strip() for r in repos if r and r.strip()}
        by_name = dict(found)
        missing = sorted(wanted - by_name.keys())
        if missing:
            raise h.HarnessError(
                f"unknown repo(s): {', '.join(missing)}. Available under {root}: "
                + ", ".join(sorted(by_name))
            )
        found = [(n, p) for n, p in found if n in wanted]
    return found


def _git_files(repo_path: Path) -> list[Path] | None:
    """Tracked + untracked-but-not-ignored files, or None if this is not a repo."""
    try:
        proc = subprocess.run(
            [
                "git", "-C", str(repo_path), "ls-files", "-z",
                "--cached", "--others", "--exclude-standard",
            ],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = {n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n}
    return [repo_path / n for n in sorted(names)]


def _walk_files(repo_path: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            out.append(Path(dirpath) / name)
    return out


def _is_skippable_name(path: Path) -> bool:
    name = path.name
    if name in SKIP_NAMES:
        return True
    low = name.lower()
    if low.endswith((".min.js", ".min.css", ".min.mjs")):
        return True
    if path.suffix.lower() in SKIP_SUFFIXES:
        return True
    if len(path.parts) > 1 and path.parts[0] in SKIP_TOP_DIRS:
        return True
    return any(part in SKIP_DIRS for part in path.parts)


def _candidate_files(repo_path: Path) -> list[Path]:
    files = _git_files(repo_path)
    if files is None:
        files = _walk_files(repo_path)
    keep: list[Path] = []
    for f in files:
        try:
            rel = f.relative_to(repo_path)
        except ValueError:  # pragma: no cover - defensive
            continue
        if _is_skippable_name(rel):
            continue
        keep.append(f)
    return keep


def _read_text(path: Path, size: int) -> tuple[str, str] | None:
    """(text, sha1) for an indexable text file, or None if it should be skipped."""
    if size > MAX_FILE_BYTES:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    # Minified / generated: a handful of enormous lines. Embedding these
    # produces one useless vector per file and burns the batch budget.
    longest = max(len(line) for line in lines)
    if longest > 2000 and len(lines) < 50:
        return None
    if len(lines) > 4 and (len(text) / len(lines)) > 400:
        return None
    return text, hashlib.sha1(data).hexdigest()


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def _chunk_lines(text: str) -> Iterator[tuple[int, int, str]]:
    """Yield ``(start_line, end_line, chunk_text)``, 1-based inclusive lines."""
    lines = text.splitlines()
    total = len(lines)
    i = 0
    while i < total:
        end = min(total, i + CHUNK_LINES)
        chars = 0
        j = i
        while j < end:
            chars += len(lines[j]) + 1
            j += 1
            if chars >= CHUNK_MAX_CHARS:
                break
        end = max(j, i + 1)
        body = "\n".join(lines[i:end])
        if body.strip():
            yield i + 1, end, body
        if end >= total:
            return
        i = max(end - CHUNK_OVERLAP_LINES, i + 1)


# --------------------------------------------------------------------------
# Embedding
# --------------------------------------------------------------------------


# Asymmetric embedding models want the passage and the question marked as such;
# with nomic-embed-text the prefixes roughly double the score margin between a
# correct hit and a distractor, so an index built without them is measurably
# worse. This is the only place outside harness_lib.py that reasons about a
# model family, and it deliberately probes for the family rather than pinning a
# tag: a team that pins a different embedder in config/tier-overrides.yaml gets
# no prefixes, because for most models they are just noise in the input.
_PREFIX_SCHEMES = {
    "nomic": {"document": "search_document: ", "query": "search_query: "},
    "none": {"document": "", "query": ""},
}


def _embed_scheme(cfg: h.Config | None = None) -> str:
    """Prefix scheme required by whatever ``local-embed`` currently resolves to."""
    try:
        tag = str((cfg or h.load_config(required=False)).models.get("local-embed") or "")
    except h.HarnessError:
        return "none"
    return "nomic" if "nomic" in tag.lower() else "none"


def _prefixed(texts: Sequence[str], scheme: str, kind: str) -> list[str]:
    prefix = _PREFIX_SCHEMES.get(scheme, _PREFIX_SCHEMES["none"])[kind]
    return [prefix + t for t in texts] if prefix else list(texts)


def _embed_batch(texts: Sequence[str], tool: str, scheme: str, kind: str) -> list[list[float]]:
    try:
        vectors = h.embed(_prefixed(texts, scheme, kind), model="local-embed", tool=tool)
    except h.HarnessError as exc:
        raise _Unavailable(str(exc)) from exc
    if len(vectors) != len(texts):
        raise _Unavailable(
            f"local-embed returned {len(vectors)} vectors for {len(texts)} inputs"
        )
    return vectors


def _unavailable(detail: str) -> h.HarnessError:
    return h.HarnessError(
        f"local-embed is unreachable: {detail}\n"
        "Fix: start the proxy with `./setup.sh --start`, then check "
        "`./setup.sh --check`. If the proxy is up, the embedding model may not be "
        "pulled -- `ollama list` should show the tag your tier profile maps "
        "`local-embed` to."
    )


def _pack(vector: Sequence[float]) -> bytes:
    """Store L2-normalised float32 so search is a plain dot product."""
    norm = sqrt(sum(v * v for v in vector)) or 1.0
    return array("f", [v / norm for v in vector]).tobytes()


def _unpack(blob: bytes) -> array:
    vec = array("f")
    vec.frombytes(blob)
    return vec


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------


def build_index(
    workspace_root: Path,
    *,
    repos: list[str] | None = None,
    force: bool = False,
    progress: Callable[[str], None] | None = None,
    tool: str = "workspace_index",
) -> IndexStats:
    """Create or refresh the index for ``workspace_root``.

    Incremental by default: a file whose mtime, size and content hash are
    unchanged is not re-embedded. ``force=True`` discards and rebuilds the
    scope. ``repos`` limits the scope to named top-level repos. ``progress``
    receives short human-readable status lines. ``tool`` is the usage-journal
    label passed through to ``h.embed()``.
    """
    started = time.time()
    root = _resolve_workspace(workspace_root)
    say = progress or (lambda _msg: None)
    stats = IndexStats()
    scheme = _embed_scheme()

    targets = _discover_repos(root, repos)
    conn = _connect()
    try:
        # The database is bound to one workspace root; pointing the harness at
        # a different workspace invalidates every stored path.
        stored_root = _meta_get(conn, "workspace_root")
        if stored_root and stored_root != str(root):
            say(f"workspace root changed ({stored_root} -> {root}); rebuilding")
            force = True
        if _meta_get(conn, "schema_version") not in (None, str(SCHEMA_VERSION)):
            force = True
        # Mixing prefixed queries against unprefixed documents is worse than
        # doing neither, so a scheme change invalidates the whole index.
        stored_scheme = _meta_get(conn, "embed_scheme")
        if stored_scheme is not None and stored_scheme != scheme:
            say(f"embedding prefix scheme changed ({stored_scheme} -> {scheme}); rebuilding")
            force = True
            stored_root = None  # wipe everything, not just this run's repos
            conn.execute("DELETE FROM files")
            conn.commit()

        if force:
            scope = [name for name, _ in targets]
            if stored_root and stored_root != str(root):
                conn.execute("DELETE FROM files")
            else:
                conn.executemany("DELETE FROM files WHERE repo = ?", [(n,) for n in scope])
            conn.execute("DELETE FROM chunks WHERE file_id NOT IN (SELECT id FROM files)")
            conn.commit()

        _meta_set(conn, "workspace_root", str(root))
        _meta_set(conn, "schema_version", str(SCHEMA_VERSION))
        _meta_set(conn, "embed_scheme", scheme)
        conn.commit()

        pending: list[tuple[str, int, int, str]] = []  # (path, start, end, text)
        pending_chars = 0
        dim_seen = 0

        def flush() -> None:
            nonlocal pending, pending_chars, dim_seen
            if not pending:
                return
            vectors = _embed_batch([p[3] for p in pending], tool, scheme, "document")
            rows = []
            for (path, start, end, text), vec in zip(pending, vectors):
                dim_seen = dim_seen or len(vec)
                rows.append((path, start, end, text, _pack(vec)))
            conn.executemany(
                "INSERT INTO chunks(file_id, start_line, end_line, text, vector) "
                "SELECT id, ?, ?, ?, ? FROM files WHERE path = ?",
                [(s, e, t, v, p) for (p, s, e, t, v) in rows],
            )
            conn.commit()
            stats.chunks += len(pending)
            pending = []
            pending_chars = 0

        for repo_name, repo_path in targets:
            say(f"scanning {repo_name}")
            known: dict[str, tuple[float, int, str]] = {
                r[0]: (r[1], r[2], r[3])
                for r in conn.execute(
                    "SELECT path, mtime, size, hash FROM files WHERE repo = ?", (repo_name,)
                )
            }
            seen: set[str] = set()
            changed_in_repo = 0

            for abs_path in _candidate_files(repo_path):
                try:
                    st = abs_path.stat()
                except OSError:
                    continue
                if not abs_path.is_file() or abs_path.is_symlink():
                    continue
                rel = abs_path.relative_to(root).as_posix()
                seen.add(rel)

                prior = known.get(rel)
                if prior and abs(prior[0] - st.st_mtime) < 1e-6 and prior[1] == st.st_size:
                    stats.skipped += 1
                    continue

                loaded = _read_text(abs_path, st.st_size)
                if loaded is None:
                    if prior:
                        conn.execute("DELETE FROM files WHERE path = ?", (rel,))
                    stats.skipped += 1
                    continue
                text, digest = loaded

                if prior and prior[2] == digest:
                    # Touched but identical (a checkout, a formatter no-op).
                    conn.execute(
                        "UPDATE files SET mtime = ?, size = ? WHERE path = ?",
                        (st.st_mtime, st.st_size, rel),
                    )
                    stats.skipped += 1
                    continue

                conn.execute("DELETE FROM files WHERE path = ?", (rel,))
                conn.execute(
                    "INSERT INTO files(path, repo, mtime, size, hash, indexed_at) "
                    "VALUES(?, ?, ?, ?, ?, ?)",
                    (rel, repo_name, st.st_mtime, st.st_size, digest, time.time()),
                )
                for start, end, body in _chunk_lines(text):
                    pending.append((rel, start, end, body))
                    pending_chars += len(body)
                    if len(pending) >= BATCH_CHUNKS or pending_chars >= BATCH_CHARS:
                        flush()
                stats.files += 1
                changed_in_repo += 1
                if changed_in_repo % 50 == 0:
                    say(f"{repo_name}: {changed_in_repo} files embedded")

            flush()
            gone = sorted(known.keys() - seen)
            if gone:
                conn.executemany("DELETE FROM files WHERE path = ?", [(g,) for g in gone])
                say(f"{repo_name}: dropped {len(gone)} deleted file(s)")
            conn.commit()
            say(f"{repo_name}: {changed_in_repo} file(s) (re)indexed")

        flush()
        if dim_seen:
            _meta_set(conn, "embed_dim", str(dim_seen))
        _meta_set(conn, "built_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        conn.commit()
    except _Unavailable as exc:
        conn.rollback()
        raise _unavailable(str(exc)) from exc
    finally:
        conn.close()

    stats.duration_s = round(time.time() - started, 2)
    return stats


# --------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------


def _tokens(query: str) -> list[str]:
    """Literal tokens worth a ripgrep pass, longest (most selective) first."""
    raw = [t for t in "".join(c if c.isalnum() or c in "_.-/" else " " for c in query).split()]
    out: list[str] = []
    for tok in raw:
        tok = tok.strip(".-/")
        if len(tok) < 3 or tok.lower() in _STOPWORDS:
            continue
        if tok not in out:
            out.append(tok)
    out.sort(key=len, reverse=True)
    return out[:6]


def _looks_like_identifier(tok: str) -> bool:
    """True for code-shaped tokens: snake_case, dotted paths, CONSTANTS, camelCase.

    These are the tokens an embedding handles worst and a literal match handles
    best, so they are allowed to count as evidence even when they appear in a
    fair number of files.
    """
    if any(c in tok for c in "_./-") or any(c.isdigit() for c in tok):
        return True
    return tok[1:] != tok[1:].lower()  # internal uppercase: camelCase / CONST


def _ripgrep_lines(
    root: Path, tokens: Sequence[str], repo: str | None
) -> dict[str, dict[str, set[int]]]:
    """Workspace-relative path -> {token -> line numbers containing it}.

    Keeping the matches split per token is what lets the ranker reward a chunk
    for covering *more of the query* rather than for simply being long enough
    to contain more matching lines.
    """
    if not tokens or not shutil.which("rg"):
        return {}
    target = root / repo if repo else root
    hits: dict[str, dict[str, set[int]]] = {}
    # One pass per token rather than one combined pass: rg reports which line
    # matched but not which pattern, and we need the attribution.
    for tok in tokens:
        cmd = [
            "rg", "--no-heading", "--line-number", "--color", "never",
            "--fixed-strings", "--ignore-case", "--max-columns", "400",
            "--max-count", "200", "--glob", "!.git",
            "-e", tok, str(target),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in proc.stdout.splitlines():
            path_part, _, rest = line.partition(":")
            lineno_part, _, _ = rest.partition(":")
            if not lineno_part.isdigit():
                continue
            try:
                rel = Path(path_part).resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                continue
            hits.setdefault(rel, {}).setdefault(tok, set()).add(int(lineno_part))
    return hits


def _snippet(text: str, max_lines: int = 12, max_chars: int = 700) -> str:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    body = "\n".join(lines[:max_lines])
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + " ..."
    elif len(lines) > max_lines:
        body += "\n..."
    return body


def search(
    query: str,
    *,
    workspace_root: Path,
    limit: int = 10,
    repo: str | None = None,
    tool: str = "workspace_index",
) -> list[SearchHit]:
    """Ranked chunks for ``query``.

    Cosine similarity over the stored vectors, boosted where ripgrep found the
    query's literal tokens on a line inside the chunk. Overlapping chunks of
    the same region collapse to their best-scoring representative.
    """
    query = (query or "").strip()
    if not query:
        raise h.HarnessError("search needs a non-empty query")
    root = _resolve_workspace(workspace_root)

    conn = _connect(create=False)
    try:
        stored_root = _meta_get(conn, "workspace_root")
        if stored_root and stored_root != str(root):
            raise h.HarnessError(
                f"the index at {db_path()} was built for {stored_root}, not {root}. "
                "Rebuild it with `bin/local index --force`."
            )
        total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if not total_chunks:
            raise h.HarnessError(
                f"the workspace index at {db_path()} is empty. Build it with "
                "`bin/local index` (first build over several repos takes a few minutes)."
            )
        if repo:
            known_repos = [r[0] for r in conn.execute("SELECT DISTINCT repo FROM files")]
            if repo not in known_repos:
                raise h.HarnessError(
                    f"repo {repo!r} is not in the index. Indexed repos: "
                    + (", ".join(sorted(known_repos)) or "(none)")
                )

        # Embed the query under the same prefix scheme the documents were
        # stored with. Reading it back from the index rather than recomputing
        # it means a mid-flight model swap can never silently compare
        # `search_query:`-prefixed vectors against unprefixed documents.
        scheme = _meta_get(conn, "embed_scheme") or _embed_scheme()
        try:
            qvec = _embed_batch([query], tool, scheme, "query")[0]
        except _Unavailable as exc:
            raise _unavailable(str(exc)) from exc
        norm = sqrt(sum(v * v for v in qvec)) or 1.0
        qv = [v / norm for v in qvec]

        query_tokens = _tokens(query)
        literal = _ripgrep_lines(root, query_tokens, repo)

        # A token that turns up all over the workspace is not evidence of
        # anything. Measured on a real query here, "fails" hit 31% of files and
        # "gets" 28% -- between them they out-boosted the one file that
        # actually answered the question, which matched only "deploy" (5%).
        # So the literal pass only gets a vote on genuinely selective terms.
        #
        # Identifiers get a looser bar than prose. Exact matching on
        # `detect_ram_gb` or `OSS_API_KEY` is precisely what the embedding is
        # bad at and ripgrep is good at; matching on an ordinary English word
        # tells us nothing the embedding has not already weighed.
        indexed_paths = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] or 1
        selective = set()
        for tok in query_tokens:
            df = sum(1 for per_tok in literal.values() if tok in per_tok)
            limit_frac = 0.40 if _looks_like_identifier(tok) else 0.15
            if df and df <= max(1, limit_frac * indexed_paths):
                selective.add(tok)

        sql = (
            "SELECT f.path, f.repo, c.start_line, c.end_line, c.text, c.vector "
            "FROM chunks c JOIN files f ON f.id = c.file_id"
        )
        params: tuple = ()
        if repo:
            sql += " WHERE f.repo = ?"
            params = (repo,)

        scored: list[tuple[float, str, str, int, int, str]] = []
        for path, repo_name, start, end, text, blob in conn.execute(sql, params):
            vec = _unpack(blob)
            if len(vec) != len(qv):
                continue  # embedding model changed under us; rebuild will fix
            score = sum(map(operator.mul, qv, vec))
            per_token = literal.get(path)
            if per_token:
                # Count DISTINCT query tokens covered by this chunk, not the
                # number of matching lines. Line counts scale with chunk size,
                # so they systematically buried short, exactly-relevant files
                # (a 2-line runbook) under long ones that merely repeated a
                # common word. Token coverage is length-independent.
                covered = sum(
                    1
                    for tok in selective
                    if any(start <= ln <= end for ln in per_token.get(tok, ()))
                )
                if covered:
                    # Exact-token evidence is strong on code; cap it so a file
                    # full of the word cannot bury a better semantic match.
                    score += min(0.24, 0.08 * covered)
            scored.append((score, path, repo_name, start, end, text))
    finally:
        conn.close()

    scored.sort(key=lambda row: row[0], reverse=True)

    hits: list[SearchHit] = []
    taken: dict[str, list[tuple[int, int]]] = {}
    for score, path, repo_name, start, end, text in scored:
        if any(start <= e and s <= end for s, e in taken.get(path, ())):
            continue  # same region already cited by a better-scoring chunk
        taken.setdefault(path, []).append((start, end))
        hits.append(
            SearchHit(
                path=path,
                repo=repo_name,
                start_line=start,
                end_line=end,
                score=round(float(score), 4),
                snippet=_snippet(text),
            )
        )
        if len(hits) >= max(1, int(limit)):
            break
    return hits


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------


def index_status(workspace_root: Path) -> dict:
    """Index size, age and staleness for ``workspace_root``.

    Cheap: staleness is a stat-only comparison against the recorded mtime and
    size, so callers can use it to decide whether to rebuild before searching.
    Never raises for a missing database -- ``exists`` is False instead.
    """
    path = db_path()
    root = Path(workspace_root).expanduser()
    status: dict = {
        "db": str(path),
        "exists": path.exists(),
        "workspace_root": str(root),
        "files": 0,
        "chunks": 0,
        "repos": {},
        "built_at": None,
        "embed_dim": None,
        # Which prefix scheme the stored vectors use. Surfaced so a mismatch
        # against the current local-embed model is diagnosable rather than
        # showing up as quietly worse search results.
        "embed_scheme": None,
        "embed_scheme_current": None,
        "db_bytes": path.stat().st_size if path.exists() else 0,
        "stale_files": 0,
        "missing_files": 0,
        "new_files": 0,
        "stale": True,
    }
    if not path.exists():
        status["reason"] = "no index yet"
        return status

    conn = _connect()
    try:
        stored_root = _meta_get(conn, "workspace_root")
        status["indexed_workspace_root"] = stored_root
        status["built_at"] = _meta_get(conn, "built_at")
        dim = _meta_get(conn, "embed_dim")
        status["embed_dim"] = int(dim) if dim and dim.isdigit() else None
        status["embed_scheme"] = _meta_get(conn, "embed_scheme")
        try:
            status["embed_scheme_current"] = _embed_scheme()
        except Exception:
            # Status must never fail just because config is unreadable.
            status["embed_scheme_current"] = None
        status["files"] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        status["chunks"] = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        for repo_name, nfiles, nchunks in conn.execute(
            "SELECT f.repo, COUNT(DISTINCT f.id), COUNT(c.id) "
            "FROM files f LEFT JOIN chunks c ON c.file_id = f.id GROUP BY f.repo"
        ):
            status["repos"][repo_name] = {"files": nfiles, "chunks": nchunks}

        if stored_root and root.is_dir() and str(root.resolve()) != stored_root:
            status["reason"] = f"index was built for {stored_root}"
            return status
        if not status["files"]:
            status["reason"] = "index is empty"
            return status
        if not root.is_dir():
            status["reason"] = f"workspace root missing: {root}"
            return status

        known = {
            r[0]: (r[1], r[2])
            for r in conn.execute("SELECT path, mtime, size FROM files")
        }
        seen: set[str] = set()
        resolved = root.resolve()
        try:
            targets = _discover_repos(resolved, None)
        except h.HarnessError as exc:
            status["reason"] = str(exc)
            return status
        for _name, repo_path in targets:
            for abs_path in _candidate_files(repo_path):
                try:
                    st = abs_path.stat()
                except OSError:
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    continue
                rel = abs_path.relative_to(resolved).as_posix()
                seen.add(rel)
                prior = known.get(rel)
                if prior is None:
                    status["new_files"] += 1
                elif abs(prior[0] - st.st_mtime) > 1e-6 or prior[1] != st.st_size:
                    status["stale_files"] += 1
        status["missing_files"] = len(known.keys() - seen)
        drift = status["new_files"] + status["stale_files"] + status["missing_files"]
        status["stale"] = drift > 0
        status["reason"] = "up to date" if not drift else f"{drift} file(s) changed since last build"
    finally:
        conn.close()
    return status
