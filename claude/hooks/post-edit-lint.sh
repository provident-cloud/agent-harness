#!/usr/bin/env bash
# post-edit-lint.sh -- Claude Code PostToolUse hook, matcher "Edit|Write".
#
# Deterministic lint/typecheck feedback with no model in the loop, so the
# edit -> lint -> fix cycle costs nothing. Reads the hook payload on stdin,
# takes .tool_input.file_path, picks a linter from what actually exists in that
# file's repo, and runs it scoped to the single file.
#
# The linter does not have to be installed globally: Python tools are reached
# through `uvx` and JS tools through the project's own node_modules (then
# `npx --no-install`). Silence means the project configures no linter, never
# that a configured linter was missing from PATH.
#
# Exit codes are the interface:
#   0  nothing to say (also the case when no linter could be identified)
#   2  lint failed -- stderr is fed back to the model, which then fixes it
#
# It stays silent unless it has something true and specific to report. A hook
# that fires noisily on every edit gets disabled within a day, and then it
# protects nothing.
#
# Escape hatches:
#   HARNESS_LINT_SKIP=1        disable entirely
#   HARNESS_LINT_TIMEOUT=<s>   per-check wall clock cap (default 30)
#   make lint-file FILE=<path> if a Makefile up-tree defines that target, it
#                              wins over every built-in detection below

set -euo pipefail

# Linter output is read by a model, not a terminal. Colour escapes are noise.
export NO_COLOR=1
export TERM=dumb

if [[ "${HARNESS_LINT_SKIP:-0}" == "1" ]]; then
  exit 0
fi

# jq parses the payload. Without it this hook has no input, so it does nothing.
if ! command -v jq >/dev/null 2>&1; then
  exit 0
fi

payload=$(cat)
file=$(printf '%s' "$payload" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)

# No path, or the file is already gone: nothing to lint.
if [[ -z "$file" || ! -f "$file" ]]; then
  exit 0
fi

file_dir=$(cd -P "$(dirname "$file")" && pwd)
file_abs="$file_dir/$(basename "$file")"
ext="${file_abs##*/}"
ext="${ext##*.}"

timeout_secs="${HARNESS_LINT_TIMEOUT:-30}"

# Nearest ancestor directory containing any of the given markers.
find_up() {
  local dir=$1 marker
  shift
  while [[ -n "$dir" && "$dir" != "/" ]]; do
    for marker in "$@"; do
      if [[ -e "$dir/$marker" ]]; then
        printf '%s\n' "$dir"
        return 0
      fi
    done
    dir=$(dirname "$dir")
  done
  return 1
}

# grep for a section header in a config file that may or may not exist.
has_section() {
  local path=$1 pattern=$2
  [[ -f "$path" ]] && grep -qE "$pattern" "$path" 2>/dev/null
}

# Portable wall-clock cap. A linter that hangs must not hang the session.
# macOS ships no timeout(1), so the fallback below is the path that normally
# runs; keep it working.
run_limited() {
  if command -v timeout >/dev/null 2>&1; then
    timeout -k 5 "$timeout_secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout -k 5 "$timeout_secs" "$@"
  else
    "$@" &
    local pid=$! status=0
    (
      sleep "$timeout_secs"
      # Kill the child's children too. Killing only the child leaves a
      # grandchild holding the output pipe open, which blocks the caller's
      # command substitution long past the timeout -- the exact hang this
      # function exists to prevent.
      pkill -TERM -P "$pid" || true
      kill -TERM "$pid" || true
      sleep 2
      pkill -KILL -P "$pid" || true
      kill -KILL "$pid" || true
    ) >/dev/null 2>&1 &
    local watchdog=$!
    wait "$pid" || status=$?
    kill "$watchdog" >/dev/null 2>&1 || true
    wait "$watchdog" >/dev/null 2>&1 || true
    return "$status"
  fi
}

findings=""

# Report failures on stderr, where exit 2 makes Claude Code hand them to the
# model. Truncated, and stripped of the ANSI escapes some linters emit even
# when their output is a pipe.
emit_findings() {
  {
    printf 'Lint failed for %s\n\n' "$file_abs"
    # `|| true` because head closing the pipe early is a SIGPIPE, and under
    # pipefail that would replace the exit 2 below with a status the model
    # never sees.
    printf '%s' "$findings" | sed $'s/\x1b\[[0-9;]*[a-zA-Z]//g' | head -c 4000 || true
  } >&2
  exit 2
}

# argv prefix that actually runs a linter on this machine, set by resolve_tool.
runner=()

# Resolve a linter to a runnable argv. Hardly anyone installs ruff, mypy, or
# eslint globally any more -- they reach them through uv and npm -- so a hook
# that only fires for tools on PATH fires for almost nobody. Kinds:
#
#   path  global install only (Go and Rust toolchains, make)
#   uvx   global, else `uvx` (uv is a hard dependency of this harness)
#   npx   the project's node_modules/.bin, else global, else `npx --no-install`
#
# Returns 1 when the tool is genuinely unavailable, which the caller turns into
# silence -- never into a complaint.
resolve_tool() {
  local kind=$1 tool=$2 dir pkg
  runner=()

  if [[ "$kind" == "npx" ]]; then
    # The project's own copy wins: it is the version its CI runs.
    if dir=$(find_up "$file_dir" node_modules 2>/dev/null); then
      if [[ -x "$dir/node_modules/.bin/$tool" ]]; then
        runner=("$dir/node_modules/.bin/$tool")
        return 0
      fi
    fi
  fi

  if command -v "$tool" >/dev/null 2>&1; then
    runner=("$tool")
    return 0
  fi

  case "$kind" in
    uvx)
      case "$tool" in
        shellcheck) pkg=shellcheck-py ;; # PyPI name differs from the binary
        *) pkg=$tool ;;
      esac
      if command -v uvx >/dev/null 2>&1; then
        # The first run downloads the tool and may hit the timeout; uv caches
        # it, so the next edit gets the lint.
        runner=(uvx --quiet --from "$pkg" "$tool")
        return 0
      fi
      if command -v uv >/dev/null 2>&1; then
        runner=(uv tool run --quiet --from "$pkg" "$tool")
        return 0
      fi
      ;;
    npx)
      # --no-install so this runs the project's own dependency and never
      # downloads one. A hook that fetches packages mid-edit gets disabled.
      if command -v npx >/dev/null 2>&1; then
        runner=(npx --no-install "$tool")
        return 0
      fi
      ;;
  esac
  return 1
}

# Run a check and record its output if it fails. Silently skips when the tool
# cannot be resolved -- an absent linter is not a lint error.
check() {
  local label=$1 kind=$2 tool=$3
  shift 3
  resolve_tool "$kind" "$tool" || return 0
  local out status=0
  out=$(run_limited "${runner[@]}" "$@" 2>&1) || status=$?
  if is_timeout "$status"; then
    return 0
  fi
  if [[ "$status" -ne 0 ]]; then
    findings+="[$label]"$'\n'"$out"$'\n\n'
  fi
}

# A linter we killed on the clock has told us nothing, so we say nothing.
# 124 is timeout(1); 143/137 are TERM/KILL from the fallback watchdog.
is_timeout() {
  case "$1" in
    124 | 137 | 143) return 0 ;;
    *) return 1 ;;
  esac
}

# --- project-defined escape hatch -------------------------------------------
# `make lint-file FILE=...` is an explicit statement of intent by the project
# and beats anything this script would infer.
if mk_dir=$(find_up "$file_dir" Makefile GNUmakefile 2>/dev/null); then
  mk="$mk_dir/Makefile"
  [[ -f "$mk" ]] || mk="$mk_dir/GNUmakefile"
  if grep -qE '^lint-file[[:space:]]*:' "$mk" 2>/dev/null; then
    check "make lint-file" path make -C "$mk_dir" --no-print-directory lint-file FILE="$file_abs"
    if [[ -n "$findings" ]]; then
      emit_findings
    fi
    exit 0
  fi
fi

# --- language detection ------------------------------------------------------
case "$ext" in
  py)
    if py_dir=$(find_up "$file_dir" ruff.toml .ruff.toml pyproject.toml 2>/dev/null); then
      if [[ -f "$py_dir/ruff.toml" || -f "$py_dir/.ruff.toml" ]] \
         || has_section "$py_dir/pyproject.toml" '^\[tool\.ruff'; then
        # concise: one line per diagnostic, which is what a model needs.
        check ruff uvx ruff check --quiet --output-format=concise "$file_abs"
      fi
    fi
    if my_dir=$(find_up "$file_dir" mypy.ini .mypy.ini pyproject.toml setup.cfg 2>/dev/null); then
      if [[ -f "$my_dir/mypy.ini" || -f "$my_dir/.mypy.ini" ]] \
         || has_section "$my_dir/pyproject.toml" '^\[tool\.mypy' \
         || has_section "$my_dir/setup.cfg" '^\[mypy\]'; then
        check mypy uvx mypy --no-error-summary --hide-error-context "$file_abs"
      fi
    fi
    ;;

  js | jsx | mjs | cjs | ts | tsx | mts | cts)
    # An eslint config is the signal. A bare package.json is not: plenty of
    # projects have one and no linter.
    if find_up "$file_dir" \
        eslint.config.js eslint.config.mjs eslint.config.cjs eslint.config.ts \
        .eslintrc .eslintrc.js .eslintrc.cjs .eslintrc.json .eslintrc.yml .eslintrc.yaml \
        >/dev/null 2>&1; then
      # --no-warn-ignored: an intentionally ignored file is not a failure.
      check eslint npx eslint --no-warn-ignored --format unix "$file_abs"
    fi
    ;;

  go)
    if find_up "$file_dir" go.mod >/dev/null 2>&1; then
      # gofmt reports by printing filenames, not by exiting non-zero.
      if command -v gofmt >/dev/null 2>&1; then
        gofmt_out=$(run_limited gofmt -l "$file_abs" 2>&1 || true)
        if [[ -n "$gofmt_out" ]]; then
          findings+="[gofmt] file is not gofmt-clean; run: gofmt -w $file_abs"$'\n\n'
        fi
      fi
      # go vet is per-package and must run inside the module: given a path, the
      # go tool still resolves the module from the current directory, which for
      # a hook is wherever the session happens to be.
      if command -v go >/dev/null 2>&1; then
        vet_status=0
        vet_out=$(cd "$file_dir" && run_limited go vet . 2>&1) || vet_status=$?
        if ! is_timeout "$vet_status" && [[ "$vet_status" -ne 0 ]]; then
          findings+="[go vet]"$'\n'"$vet_out"$'\n\n'
        fi
      fi
    fi
    ;;

  rs)
    # rustfmt only. cargo clippy compiles the crate and is far too slow to run
    # on every keystroke-sized edit; leave it to the repo's test command.
    if find_up "$file_dir" Cargo.toml >/dev/null 2>&1; then
      check rustfmt path rustfmt --check --edition 2021 "$file_abs"
    fi
    ;;

  sh | bash)
    # No config file to detect: a shell script either passes shellcheck or has
    # a real problem. uvx reaches it via the shellcheck-py package.
    check shellcheck uvx shellcheck --format=gcc "$file_abs"
    ;;

  *)
    # Unrecognised: say nothing at all.
    exit 0
    ;;
esac

if [[ -n "$findings" ]]; then
  emit_findings
fi

exit 0
