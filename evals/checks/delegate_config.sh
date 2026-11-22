#!/usr/bin/env bash
# The delegate adapter x model alias check, as `bin/local delegate-check` runs it
# (and `bin/local health`, and the delegate server before every call).
# Builds throwaway harness roots with one config each and asserts both the exit
# code and the diagnostic, so a crash cannot pass for a rejection.
# Target tree: $HARNESS_EVAL_TARGET (bin/eval sets it), else the cwd.
set -uo pipefail
TARGET="${HARNESS_EVAL_TARGET:-$PWD}"
command -v uv >/dev/null || { echo "delegate-config: uv not on PATH"; exit 77; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0

# Stub CLIs: a current and a too-old Claude Code, and a codex.
mkdir -p "$TMP/cli-new" "$TMP/cli-old"
printf '#!/bin/sh\necho "2.1.330 (Claude Code)"\n' > "$TMP/cli-new/claude"
printf '#!/bin/sh\necho "2.1.100 (Claude Code)"\n' > "$TMP/cli-old/claude"
printf '#!/bin/sh\necho "codex-cli 0.0.0"\n' > "$TMP/cli-new/codex"
cp "$TMP/cli-new/codex" "$TMP/cli-old/codex"
chmod +x "$TMP"/cli-*/*

case_() {  # name expected-exit expected-substring cli-dir harness.yaml-block [tier-overrides]
  local name="$1" want="$2" needle="$3" clidir="$4" block="$5" overrides="${6:-}" root="$TMP/$1"
  mkdir -p "$root/config" "$root/bin"
  ln -s "$TARGET/lib" "$root/lib"
  ln -s "$TARGET/bin/local" "$root/bin/local"
  printf 'version: 1\ntier: 32gb\n%s\n' "$block" > "$root/config/harness.yaml"
  [ -n "$overrides" ] && printf '%s\n' "$overrides" > "$root/config/tier-overrides.yaml"
  PATH="$TMP/$clidir:$PATH" HARNESS_ROOT="$root" NO_COLOR=1 "$root/bin/local" delegate-check \
    > "$root/out" 2>&1
  local got=$?
  if [ "$got" = "$want" ] && grep -qF -- "$needle" "$root/out" && ! grep -q Traceback "$root/out"; then
    echo "ok   $name (exit $got)"
  else
    echo "FAIL $name: want exit $want and \"$needle\", got exit $got"; sed 's/^/     /' "$root/out"; fail=1
  fi
}

case_ default-pair        0 "adapter codex + model frontier-default" cli-new ""
case_ codex-default       0 "adapter codex + model frontier-default" cli-new $'delegate:\n  adapter: codex\n  model: frontier-default'
case_ claude-deep         0 "adapter claude + model frontier-deep"   cli-new $'delegate:\n  adapter: claude\n  model: frontier-deep'
case_ codex-deep-unmapped 1 "has no model mapped for 'frontier-deep'" cli-new $'delegate:\n  adapter: codex\n  model: frontier-deep'
case_ local-alias         1 "is a local harness alias"               cli-new $'delegate:\n  adapter: claude\n  model: local-big'
case_ concrete-name       1 "is not a delegate model alias"          cli-new $'delegate:\n  adapter: codex\n  model: some-concrete-model-id'
case_ unknown-adapter     1 "unknown delegate adapter"               cli-new $'delegate:\n  adapter: no-such-cli\n  model: frontier-default'
case_ codex-deep-mapped   0 "adapter codex + model frontier-deep"    cli-new $'delegate:\n  adapter: codex\n  model: frontier-deep' \
                            $'delegate_models:\n  frontier-deep:\n    codex: some-codex-model'
case_ mapping-is-list     1 "must be a model string or null"         cli-new $'delegate:\n  adapter: codex\n  model: frontier-deep' \
                            $'delegate_models:\n  frontier-deep:\n    codex: [a, b]'
case_ adapter-not-string  1 "delegate.adapter must be a non-empty string" cli-new $'delegate:\n  adapter: 0\n  model: frontier-default'
case_ adapter-empty       1 "delegate.adapter must be a non-empty string" cli-new $'delegate:\n  adapter: ""'
case_ delegate-not-map    1 "must be a mapping"                      cli-new $'delegate: [codex]'
case_ claude-too-old      1 "older than 2.1.259"                     cli-old $'delegate:\n  adapter: claude\n  model: frontier-default'

[ "$fail" = 0 ] && echo "delegate-config: all pairs judged correctly" || echo "delegate-config: FAIL"
exit "$fail"
