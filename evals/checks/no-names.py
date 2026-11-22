#!/usr/bin/env python3
"""Fail if a tracked file (path or content) carries a denylisted name.

This repo is public and generic. Team, org, product and person names belong in
gitignored config/, never in a tracked file. The check needs the names to look
for them, but must not publish them, so it reads two sources:

* evals/checks/name-denylist.sha256 (tracked): SHA-256 of each lower-cased name,
  so CI and a fresh clone can run the check without the names in plain text.
  This avoids publishing them; it is not confidentiality -- a guessed name can
  be confirmed against a hash. Names that must stay confidential go only in
  the gitignored file below.
* config/name-denylist.txt (gitignored, optional): plain names, one per line,
  for a team's own additions. `name` or `name substr`.

Matching is on lower-cased [a-z0-9]+ tokens. Mode `token` matches a whole token;
mode `substr` matches inside a token too (for names no English word contains).
Hits are printed fully redacted (paths too), so the check's own output and
bin/eval's saved transcript do not leak the name.

Content is read from git's index (blobs, symlink targets included), not the
working tree, so what is checked is what would be committed.

Exemptions in name-exemptions.txt are (path, name-hash, line-prefix) triples,
never whole files: the name is tolerated only on lines of that file that start
with that prefix.

Target tree: $HARNESS_EVAL_TARGET (set by bin/eval), else the cwd.
Exit 0 clean, 1 hits, 2 misconfigured (an empty denylist is a failure, loudly).
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS_ROOT = Path(os.environ.get("HARNESS_ROOT") or HERE.parent.parent)
TOKEN = re.compile(r"[a-z0-9]+")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_denylist() -> dict[str, tuple[str, int]]:
    """hash -> (mode, length). Length is needed for substring matching."""
    entries: dict[str, tuple[str, int]] = {}
    hashed = HERE / "name-denylist.sha256"
    if hashed.exists():
        for line in hashed.read_text().splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) == 3:
                digest, mode, length = parts
                entries[digest] = (mode, int(length))
    plain = HARNESS_ROOT / "config" / "name-denylist.txt"
    if plain.exists():
        for line in plain.read_text().splitlines():
            parts = line.split("#", 1)[0].split()
            if parts:
                name = parts[0].lower()
                mode = parts[1] if len(parts) > 1 else "token"
                entries[sha(name)] = (mode, len(name))
    return entries


def load_exemptions() -> list[tuple[str, str, str]]:
    path = HERE / "name-exemptions.txt"
    out: list[tuple[str, str, str]] = []
    if path.exists():
        for line in path.read_text().splitlines():
            parts = line.split("#", 1)[0].split(None, 2)
            if len(parts) == 3:
                out.append((parts[0], parts[1], parts[2].strip()))
    return out


def exempt(rel: str, digest: str, line: str | None, rules: list[tuple[str, str, str]]) -> bool:
    if line is None:  # a file path is never exempt
        return False
    return any(rel == p and digest == d and line.lstrip().startswith(prefix)
               for p, d, prefix in rules)


def hits_in(text: str, deny: dict[str, tuple[str, int]]) -> list[str]:
    """Digests of denylisted names found in ``text``."""
    substr_lengths = sorted({n for mode, n in deny.values() if mode == "substr"})
    found: list[str] = []
    for token in set(TOKEN.findall(text.lower())):
        digest = sha(token)
        if digest in deny:
            found.append(digest)
            continue
        for n in substr_lengths:
            for i in range(len(token) - n + 1):
                d = sha(token[i : i + n])
                if deny.get(d, ("", 0))[0] == "substr":
                    found.append(d)
    return found


def mask(text: str, deny: dict[str, tuple[str, int]]) -> str:
    """Replace every word that carries a denylisted name with [redacted]."""
    def repl(m: re.Match[str]) -> str:
        return "[redacted]" if hits_in(m.group(0), deny) else m.group(0)

    return re.sub(r"[A-Za-z0-9]+", repl, text.strip())[:160]


def index_blobs(target: Path) -> list[tuple[str, bytes]]:
    """(path, content) for every entry in git's index; symlinks yield their target."""
    listing = subprocess.run(
        ["git", "-C", str(target), "ls-files", "-z", "-s"],
        capture_output=True, check=True,
    ).stdout.split(b"\0")
    entries = []
    for row in filter(None, listing):
        meta, _, path = row.partition(b"\t")
        mode, sha, _stage = meta.split()
        if mode == b"160000":  # submodule: no blob to read
            entries.append((path.decode(errors="replace"), sha, False))
        else:
            entries.append((path.decode(errors="replace"), sha, True))
    shas = b"".join(sha + b"\n" for _, sha, blob in entries if blob)
    out = subprocess.run(["git", "-C", str(target), "cat-file", "--batch"],
                         input=shas, capture_output=True, check=True).stdout
    blobs: dict[bytes, bytes] = {}
    pos = 0
    while pos < len(out):
        nl = out.index(b"\n", pos)
        sha, _type, size = out[pos:nl].split()
        start = nl + 1
        blobs[sha] = out[start : start + int(size)]
        pos = start + int(size) + 1
    return [(path, blobs.get(sha, b"")) for path, sha, _ in entries]


def main() -> int:
    target = Path(os.environ.get("HARNESS_EVAL_TARGET") or os.getcwd())
    deny = load_denylist()
    if not deny:
        print("no-names: denylist is empty -- refusing to report a clean tree", file=sys.stderr)
        return 2
    rules = load_exemptions()
    entries = index_blobs(target)
    problems: list[str] = []
    for rel, blob in entries:
        shown = mask(rel, deny)
        for d in set(hits_in(rel, deny)):
            if not exempt(rel, d, None, rules):
                problems.append(f"{shown}: (file path)")
        if b"\0" in blob[:8000]:
            continue  # binary
        text = blob.decode(errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            for d in set(hits_in(line, deny)):
                if not exempt(rel, d, line, rules):
                    problems.append(f"{shown}:{lineno}: {mask(line, deny)}")
    if problems:
        for p in sorted(set(problems)):
            print(p)
        print(f"no-names: {len(set(problems))} hit(s) for a denylisted name "
              f"({len(deny)} names checked); move it to config/ or make it generic")
        return 1
    print(f"no-names: clean ({len(entries)} tracked files, {len(deny)} names checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
