#!/usr/bin/env python3
"""Fail if a tracked file (path or content) carries a denylisted name.

This repo is public and generic. Team, org, product and person names belong in
gitignored config/, never in a tracked file. The check needs the names to look
for them, but must not publish them, so it reads two sources:

* evals/checks/name-denylist.sha256 (tracked): SHA-256 of each lower-cased name,
  so CI and a fresh clone can run the check without learning the names.
* config/name-denylist.txt (gitignored, optional): plain names, one per line,
  for a team's own additions. `name` or `name substr`.

Matching is on lower-cased [a-z0-9]+ tokens. Mode `token` matches a whole token;
mode `substr` matches inside a token too (for names no English word contains).
Hits are printed masked, so the check's own output does not leak the name.

Exemptions are (path, name-hash) pairs in name-exemptions.txt, never whole
files, so an exempt file still fails on any other name.

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


def load_exemptions() -> set[tuple[str, str]]:
    path = HERE / "name-exemptions.txt"
    out: set[tuple[str, str]] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) == 2:
                out.add((parts[0], parts[1]))
    return out


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


def mask(line: str, deny: dict[str, tuple[str, int]]) -> str:
    def repl(m: re.Match[str]) -> str:
        word = m.group(0)
        return word[0] + "*" * (len(word) - 1) if hits_in(word, deny) else word

    return re.sub(r"[A-Za-z0-9]+", repl, line.strip())[:160]


def main() -> int:
    target = Path(os.environ.get("HARNESS_EVAL_TARGET") or os.getcwd())
    deny = load_denylist()
    if not deny:
        print("no-names: denylist is empty -- refusing to report a clean tree", file=sys.stderr)
        return 2
    exempt = load_exemptions()
    files = subprocess.run(
        ["git", "-C", str(target), "ls-files", "-z"], capture_output=True, text=True, check=True
    ).stdout.split("\0")
    problems: list[str] = []
    for rel in filter(None, files):
        for d in hits_in(rel, deny):
            if (rel, d) not in exempt:
                problems.append(f"{rel}: (file path) {mask(rel, deny)}")
        path = target / rel
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue  # binary
        for lineno, line in enumerate(text.splitlines(), 1):
            for d in set(hits_in(line, deny)):
                if (rel, d) not in exempt:
                    problems.append(f"{rel}:{lineno}: {mask(line, deny)}")
    if problems:
        for p in sorted(set(problems)):
            print(p)
        print(f"no-names: {len(set(problems))} hit(s) for a denylisted name "
              f"({len(deny)} names checked); move it to config/ or make it generic")
        return 1
    print(f"no-names: clean ({len(files) - 1} tracked files, {len(deny)} names checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
