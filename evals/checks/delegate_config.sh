#!/usr/bin/env bash
# The delegate adapter x model alias check, as `bin/local delegate-check` runs it
# (and `bin/local health`, and the delegate server before every call).
# Builds throwaway harness roots with one config each and asserts the verdict.
# Target tree: $HARNESS_EVAL_TARGET (bin/eval sets it), else the cwd.
set -uo pipefail
TARGET="${HARNESS_EVAL_TARGET:-$PWD}"
command -v uv >/dev/null || { echo "delegate-config: uv not on PATH"; exit 77; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0

case_() {  # name expected-exit harness.yaml-delegate-block [tier-overrides body]
  local name="$1" want="$2" block="$3" overrides="${4:-}" root="$TMP/$1"
  mkdir -p "$root/config" "$root/bin"
  ln -s "$TARGET/lib" "$root/lib"
  ln -s "$TARGET/bin/local" "$root/bin/local"
  printf 'version: 1\ntier: 32gb\n%s\n' "$block" > "$root/config/harness.yaml"
  [ -n "$overrides" ] && printf '%s\n' "$overrides" > "$root/config/tier-overrides.yaml"
  HARNESS_ROOT="$root" NO_COLOR=1 "$root/bin/local" delegate-check > "$root/out" 2>&1
  local got=$?
  if [ "$got" = "$want" ]; then
    echo "ok   $name (exit $got)"
  else
    echo "FAIL $name: want exit $want, got $got"; sed 's/^/     /' "$root/out"; fail=1
  fi
}

case_ default-pair          0 ""
case_ codex-default         0 $'delegate:\n  adapter: codex\n  model: frontier-default'
case_ claude-deep           0 $'delegate:\n  adapter: claude\n  model: frontier-deep'
case_ codex-deep-unmapped   1 $'delegate:\n  adapter: codex\n  model: frontier-deep'
case_ local-alias           1 $'delegate:\n  adapter: claude\n  model: local-big'
case_ concrete-name         1 $'delegate:\n  adapter: codex\n  model: some-concrete-model-id'
case_ unknown-adapter       1 $'delegate:\n  adapter: no-such-cli\n  model: frontier-default'
case_ codex-deep-mapped     0 $'delegate:\n  adapter: codex\n  model: frontier-deep' \
                              $'delegate_models:\n  frontier-deep:\n    codex: some-codex-model'

[ "$fail" = 0 ] && echo "delegate-config: all pairs judged correctly" || echo "delegate-config: FAIL"
exit "$fail"
