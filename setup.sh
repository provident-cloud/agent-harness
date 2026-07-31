#!/usr/bin/env bash
#
# setup.sh -- bootstrap and supervise the local half of the agent harness.
#
# Installs dependencies, picks the hardware tier, pulls the tier's Ollama models,
# starts the LiteLLM proxy on 127.0.0.1:4000 (or the address in config/harness.yaml),
# and smoke tests the three aliases (local-big / local-fast / local-embed) the rest
# of the harness talks to.
#
# Re-running is the update path, so every step detects before it acts: nothing is
# reinstalled, re-pulled, or started twice. `--check` diagnoses without touching
# anything; `--dry-run` prints the plan, including for a tier you are not on.
#
# The tier -> model table lives in lib/harness_lib.py and nowhere else. This script
# shells out to it rather than duplicating thresholds or tags.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HARNESS_ROOT="$ROOT"

VAR_DIR="$ROOT/var"
LOG_FILE="$VAR_DIR/litellm.log"
PID_FILE="$VAR_DIR/litellm.pid"
LITELLM_HOST="127.0.0.1"
LITELLM_PORT="4000"
OLLAMA_PORT="11434"
OLLAMA_BASE="http://127.0.0.1:$OLLAMA_PORT"
DEFAULT_MASTER_KEY="sk-harness-local"
HEALTH_TIMEOUT="90"

MODE="setup"        # setup | check | start | stop | restart
TIER_FLAG=""
DRY_RUN="false"
SKIP_BREW="false"
SKIP_PULL="false"
EXIT_CODE=0

# Filled in by resolve_tier().
RAM_GB=""
DETECTED_TIER=""
TIER=""
CONFIG_TIER=""
KEEP_ALIVE=""
PROFILE=""
MODEL_ALIASES=()
MODEL_TAGS=()

# Filled in by smoke_test(): "<alias>\t<PASS|FAIL|skipped>\t<detail>" per entry.
SMOKE_RESULTS=()

# --------------------------------------------------------------------------
# Output helpers. Human progress goes to stderr; see lib/harness_lib.py, which
# uses the same prefixes so script and Python output read as one stream.
# --------------------------------------------------------------------------

if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_STEP=$'\033[1;36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_STEP=""; C_OK=""; C_WARN=""; C_ERR=""
fi

step() { printf '%s==>%s %s\n' "$C_STEP" "$C_RESET" "$*" >&2; }
ok()   { printf '%s  ok %s%s\n' "$C_OK" "$C_RESET" "$*" >&2; }
warn() { printf '%swarn %s%s\n' "$C_WARN" "$C_RESET" "$*" >&2; }
err()  { printf '%serror %s%s\n' "$C_ERR" "$C_RESET" "$*" >&2; }
info() { printf '%s\n' "$*" >&2; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET" >&2; }
die()  { err "$*"; exit 1; }

# Records a non-fatal problem: reported now, reflected in the exit status later.
fail_soft() { warn "$*"; EXIT_CODE=1; }

# Prints what would happen instead of doing it. Returns 0 when it printed, so
# callers read as: `dry "..." && return 0`.
dry() {
  if [ "$DRY_RUN" = "true" ]; then
    printf '%s  dry %s%s\n' "$C_DIM" "$*" "$C_RESET" >&2
    return 0
  fi
  return 1
}

usage() {
  cat <<'EOF'
setup.sh -- bootstrap and supervise the local half of the agent harness.

USAGE
  ./setup.sh [options]

With no options: install dependencies, detect the tier, pull that tier's Ollama
models, start LiteLLM on 127.0.0.1:4000, and smoke test local-big / local-fast /
local-embed. Safe to re-run -- that is how you update.

OPTIONS
  --check              Diagnose only. Reports tooling, tier, ports, models, keys
                       and proxy state, changes nothing, and exits 0.
  --start              Start the proxy (skips install and model pulls).
  --stop               Stop the proxy from var/litellm.pid and remove the pidfile.
  --restart            --stop followed by --start.
  --tier <t>           Force 128gb | 64gb | 32gb instead of detecting from RAM.
  --dry-run            Print every action without performing it. Combine with
                       --tier to inspect another machine's plan from this one.
  --skip-brew          Do not run `brew bundle`.
  --skip-pull          Do not run `ollama pull`.
  -h, --help           This text.

EXAMPLES
  ./setup.sh                        # first run, or update after `git pull`
  ./setup.sh --check                # what is missing / what is running
  ./setup.sh --dry-run --tier 128gb # what a 128GB box would install
  ./setup.sh --restart              # pick up an edited litellm/profiles/*.yaml

FILES
  Brewfile                    dependencies (LiteLLM is a `uv tool`, not a formula)
  litellm/profiles/<tier>.yaml the proxy config chosen by tier
  var/litellm.log             proxy log      var/litellm.pid  proxy pid
  .envrc                      OSS_API_KEY and friends (copy .envrc.template)
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --check)     MODE="check" ;;
      --start)     MODE="start" ;;
      --stop)      MODE="stop" ;;
      --restart)   MODE="restart" ;;
      --dry-run)   DRY_RUN="true" ;;
      --skip-brew) SKIP_BREW="true" ;;
      --skip-pull) SKIP_PULL="true" ;;
      --tier)
        [ $# -ge 2 ] || die "--tier needs a value: 128gb, 64gb or 32gb"
        TIER_FLAG="$2"
        shift
        ;;
      --tier=*)    TIER_FLAG="${1#*=}" ;;
      -h|--help)   usage; exit 0 ;;
      *)           usage >&2; die "unknown option: $1" ;;
    esac
    shift
  done

  case "$TIER_FLAG" in
    ""|128gb|64gb|32gb) ;;
    *) die "unknown tier '$TIER_FLAG'; expected 128gb, 64gb or 32gb" ;;
  esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# --------------------------------------------------------------------------
# Python bridge. Tier thresholds and model tags have exactly one home
# (lib/harness_lib.py); this script asks it rather than re-encoding them.
# --------------------------------------------------------------------------

run_py() {
  local src="$1"
  shift
  uv run --quiet --with pyyaml python -c "$src" "$@"
}

PY_TIER=$(cat <<'PY'
"""Emit the tier facts setup.sh needs, as parseable key=value lines."""
import os
import sys

sys.path.insert(0, os.path.join(os.environ["HARNESS_ROOT"], "lib"))
import harness_lib as h  # noqa: E402

try:
    requested = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = h.load_config(required=False)
    ram = h.detect_ram_gb()
    tier = h.resolve_tier(requested) if requested else cfg.tier
    models = h.tier_models(tier)
except h.HarnessError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2)

config_exists = (h.config_dir() / "harness.yaml").exists()
print("host=%s" % cfg.host)
print("port=%d" % cfg.port)
print("ram_gb=%d" % ram)
print("detected_tier=%s" % h.detect_tier(ram))
print("tier=%s" % tier)
print("config_tier=%s" % (cfg.tier_setting if config_exists else ""))
print("keep_alive=%s" % models.get("keep_alive", ""))
print("profile=%s" % (h.harness_root() / "litellm" / "profiles" / ("%s.yaml" % tier)))
for alias, tag in models.items():
    if alias != "keep_alive":
        print("model\t%s\t%s" % (alias, tag))
PY
)

PY_SMOKE=$(cat <<'PY'
"""Probe each alias through the running proxy; one TSV line per alias."""
import json
import os
import sys
import urllib.error
import urllib.request

base = os.environ["HARNESS_BASE_URL"]
key = os.environ.get("LITELLM_MASTER_KEY") or "sk-harness-local"


def post(path, payload, timeout=180):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


rc = 0
for alias in sys.argv[1:]:
    try:
        if "embed" in alias:
            data = post("/embeddings", {"model": alias, "input": ["harness smoke test"]})
            rows = data.get("data") or [{}]
            dims = len(rows[0].get("embedding") or [])
            detail = "%s, %d dims" % (data.get("model") or alias, dims)
        else:
            data = post(
                "/chat/completions",
                {
                    "model": alias,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                    "max_tokens": 16,
                    "temperature": 0,
                },
            )
            choices = data.get("choices") or [{}]
            text = (choices[0].get("message") or {}).get("content") or ""
            detail = "%s answered %r" % (data.get("model") or alias, " ".join(text.split())[:40])
        print("%s\tPASS\t%s" % (alias, detail))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace").replace("\n", " ")[:200]
        print("%s\tFAIL\tHTTP %s %s" % (alias, exc.code, body))
        rc = 1
    except Exception as exc:  # network, timeout, malformed body
        print("%s\tFAIL\t%s" % (alias, str(exc).replace("\n", " ")[:200]))
        rc = 1
sys.exit(rc)
PY
)

# --------------------------------------------------------------------------
# 1. Preflight
# --------------------------------------------------------------------------

preflight() {
  step "Preflight"
  local tool present=() missing=()
  for tool in brew uv git jq rg gh ollama litellm direnv mise; do
    if have "$tool"; then
      present+=("$tool")
    elif [ "$tool" = "litellm" ] && [ -x "$HOME/.local/bin/litellm" ]; then
      present+=("litellm(~/.local/bin)")
    else
      missing+=("$tool")
    fi
  done
  ok "present: ${present[*]}"
  if [ ${#missing[@]} -gt 0 ]; then
    warn "missing: ${missing[*]}"
    case " ${missing[*]} " in
      *" direnv "*) dim "      direnv loads .envrc per directory; without it, export OSS_API_KEY yourself" ;;
    esac
    case " ${missing[*]} " in
      *" mise "*) dim "      mise pins per-repo runtimes so agents get your toolchain; optional for the proxy" ;;
    esac
  fi
  have uv || die "uv is required (it runs every bin/ script). Install: brew install uv"
}

# --------------------------------------------------------------------------
# 2. Homebrew dependencies
# --------------------------------------------------------------------------

# A formula counts as satisfied when brew has it OR its command is already on
# PATH from somewhere else -- the Ollama Mac app and a curl-installed uv both
# land there, and reinstalling over them causes port and version fights.
formula_command() {
  case "$1" in
    ripgrep) printf 'rg' ;;
    *)       printf '%s' "$1" ;;
  esac
}

brew_step() {
  step "Dependencies (Brewfile)"
  if [ "$SKIP_BREW" = "true" ]; then
    ok "skipped (--skip-brew)"
    return 0
  fi
  if ! have brew; then
    warn "no brew on PATH; install the Brewfile's tools yourself: $ROOT/Brewfile"
    return 0
  fi

  local brewfile="$ROOT/Brewfile"
  [ -f "$brewfile" ] || { warn "no Brewfile at $brewfile"; return 0; }

  local formulae=() missing=() satisfied_elsewhere=()
  local line name
  while IFS= read -r line; do
    case "$line" in
      brew\ \"*)
        name="${line#brew \"}"
        name="${name%%\"*}"
        formulae+=("$name")
        ;;
    esac
  done < "$brewfile"

  for name in "${formulae[@]}"; do
    if brew list --versions "$name" >/dev/null 2>&1; then
      continue
    fi
    if have "$(formula_command "$name")"; then
      satisfied_elsewhere+=("$name")
      continue
    fi
    missing+=("$name")
  done

  if [ ${#satisfied_elsewhere[@]} -gt 0 ]; then
    ok "already on PATH outside brew, not touching: ${satisfied_elsewhere[*]}"
  fi
  if [ ${#missing[@]} -eq 0 ]; then
    ok "all Brewfile formulae present; nothing to install"
    return 0
  fi

  local skip="${satisfied_elsewhere[*]:-}"
  dry "HOMEBREW_BUNDLE_BREW_SKIP=\"$skip\" brew bundle --file=$brewfile   (installs: ${missing[*]})" && return 0
  info "  installing: ${missing[*]}"
  # HOMEBREW_BUNDLE_BREW_SKIP keeps `brew bundle` from installing over a tool the
  # machine already provides (notably ollama from the Mac app).
  if HOMEBREW_BUNDLE_BREW_SKIP="$skip" brew bundle --file="$brewfile"; then
    ok "brew bundle complete"
  else
    fail_soft "brew bundle failed; install manually: brew install ${missing[*]}"
  fi
}

# --------------------------------------------------------------------------
# 3. LiteLLM (uv tool -- it has no Homebrew formula)
# --------------------------------------------------------------------------

LITELLM_BIN=""

resolve_litellm_bin() {
  if have litellm; then
    LITELLM_BIN="$(command -v litellm)"
    return 0
  fi
  if [ -x "$HOME/.local/bin/litellm" ]; then
    # Installed, just not reachable. Say so once, then use the absolute path so
    # the rest of the run works regardless.
    LITELLM_BIN="$HOME/.local/bin/litellm"
    warn "litellm is installed at $LITELLM_BIN but ~/.local/bin is not on your PATH"
    dim "      fix: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && exec zsh"
    return 0
  fi
  LITELLM_BIN=""
  return 1
}

# The response cache's disk backend imports `diskcache`, which litellm[proxy]
# does not bundle. Check with the tool's own interpreter, not the system one.
litellm_has_diskcache() {
  local py
  py="$(dirname "$LITELLM_BIN")/python"
  [ -x "$py" ] && "$py" -c "import diskcache" >/dev/null 2>&1
}

litellm_step() {
  step "LiteLLM proxy binary"
  if resolve_litellm_bin; then
    ok "litellm present: $LITELLM_BIN"
    if ! litellm_has_diskcache; then
      # Installs that predate the response cache lack diskcache; retrofit it.
      dry "uv tool install --force \"litellm[proxy]\" --with diskcache" && return 0
      info "  adding diskcache (response-cache backend) to the litellm install"
      if uv tool install --force "litellm[proxy]" --with diskcache; then
        ok "diskcache added"
      else
        fail_soft "could not add diskcache; the proxy will run with response caching disabled"
      fi
    fi
    return 0
  fi
  dry "uv tool install \"litellm[proxy]\" --with diskcache" && { LITELLM_BIN="$HOME/.local/bin/litellm"; return 0; }
  info "  installing litellm[proxy] with uv (no Homebrew formula exists)"
  if ! uv tool install "litellm[proxy]" --with diskcache; then
    fail_soft "uv tool install \"litellm[proxy]\" failed; retry manually, then re-run ./setup.sh"
    return 1
  fi
  resolve_litellm_bin || { fail_soft "litellm still not found after install"; return 1; }
  ok "installed: $LITELLM_BIN"
}

# --------------------------------------------------------------------------
# 4. Tier
# --------------------------------------------------------------------------

resolve_tier() {
  step "Hardware tier"
  local out line alias tag
  if ! out="$(run_py "$PY_TIER" "$TIER_FLAG")"; then
    die "could not read the tier table from lib/harness_lib.py"
  fi
  while IFS= read -r line; do
    case "$line" in
      # The proxy address comes from config/harness.yaml when it exists, so the
      # address this script binds is always the one h.chat()/bin/local dial.
      host=*)          LITELLM_HOST="${line#*=}" ;;
      port=*)          LITELLM_PORT="${line#*=}" ;;
      ram_gb=*)        RAM_GB="${line#*=}" ;;
      detected_tier=*) DETECTED_TIER="${line#*=}" ;;
      tier=*)          TIER="${line#*=}" ;;
      config_tier=*)   CONFIG_TIER="${line#*=}" ;;
      keep_alive=*)    KEEP_ALIVE="${line#*=}" ;;
      profile=*)       PROFILE="${line#*=}" ;;
      model*)
        IFS=$'\t' read -r _ alias tag <<< "$line"
        MODEL_ALIASES+=("$alias")
        MODEL_TAGS+=("$tag")
        ;;
    esac
  done <<< "$out"

  ok "${RAM_GB}GB RAM detected -> tier $DETECTED_TIER"
  if [ -n "$TIER_FLAG" ]; then
    if [ "$TIER" != "$DETECTED_TIER" ]; then
      warn "using tier $TIER because of --tier (this machine detects $DETECTED_TIER)"
    else
      ok "using tier $TIER (--tier)"
    fi
  else
    ok "using tier $TIER"
  fi

  # A pinned tier that disagrees with the hardware is the classic
  # copied-config-to-a-new-machine bug. Say it plainly and name the fix.
  if [ -n "$CONFIG_TIER" ] && [ "$CONFIG_TIER" != "auto" ] && [ "$CONFIG_TIER" != "$DETECTED_TIER" ]; then
    warn "config/harness.yaml pins tier: $CONFIG_TIER but this machine is ${RAM_GB}GB ($DETECTED_TIER)"
    dim "      fix: set 'tier: auto' in config/harness.yaml so it re-detects per machine"
  fi

  if [ ! -f "$PROFILE" ]; then
    fail_soft "no LiteLLM profile at $PROFILE"
  fi
  info "  profile:    $PROFILE"
  info "  keep_alive: $KEEP_ALIVE"
  local i
  for i in $(seq 0 $(( ${#MODEL_ALIASES[@]} - 1 ))); do
    printf '  %-20s %s\n' "${MODEL_ALIASES[$i]}" "${MODEL_TAGS[$i]}" >&2
  done
}

# --------------------------------------------------------------------------
# 5. Ollama models
# --------------------------------------------------------------------------

OLLAMA_TAGS=""

load_ollama_tags() {
  OLLAMA_TAGS=""
  have ollama || return 0
  OLLAMA_TAGS="$(ollama list 2>/dev/null | awk 'NR > 1 { print $1 }')" || OLLAMA_TAGS=""
}

# Ollama reports bare names as ":latest"; normalise before comparing.
normalise_tag() {
  case "$1" in
    *:*) printf '%s' "$1" ;;
    *)   printf '%s:latest' "$1" ;;
  esac
}

tag_present() {
  local want
  want="$(normalise_tag "$1")"
  printf '%s\n' "$OLLAMA_TAGS" | grep -Fxq "$want"
}

pull_models() {
  step "Ollama models (tier $TIER)"
  if [ "$SKIP_PULL" = "true" ]; then
    ok "skipped (--skip-pull)"
    return 0
  fi
  if ! have ollama; then
    fail_soft "no ollama on PATH; local aliases cannot work. Install the Ollama app or: brew install ollama"
    return 0
  fi
  if ! port_pid "$OLLAMA_PORT" >/dev/null; then
    fail_soft "nothing is listening on $OLLAMA_BASE -- start the Ollama app or run: ollama serve"
    dim "      model pulls and every local alias need it; re-run ./setup.sh once it is up"
    return 0
  fi

  load_ollama_tags
  local i alias tag
  for i in $(seq 0 $(( ${#MODEL_ALIASES[@]} - 1 ))); do
    alias="${MODEL_ALIASES[$i]}"
    tag="${MODEL_TAGS[$i]}"
    case "$tag" in
      ollama/*) tag="${tag#ollama/}" ;;
      # Hosted rungs (openai/...) are served over OSS_API_BASE, nothing to pull.
      *) dim "      $alias -> $tag (hosted, no pull)"; continue ;;
    esac
    if tag_present "$tag"; then
      ok "$alias -> $tag already pulled"
      continue
    fi
    dry "ollama pull $tag   ($alias)" && continue
    info "  pulling $tag for $alias (this can take a while)"
    if ollama pull "$tag"; then
      ok "$alias -> $tag pulled"
    else
      # Never abort the run for this: a moved tag should not cost you the proxy.
      warn "ollama pull $tag failed for $alias"
      dim "      model tags move quarterly -- check \`ollama list\` and pin in config/tier-overrides.yaml"
    fi
  done
}

# --------------------------------------------------------------------------
# 6. Keys
# --------------------------------------------------------------------------

key_check() {
  step "API keys"
  local oss_key="${OSS_API_KEY:-}" oss_base="${OSS_API_BASE:-}"

  if [ -n "$oss_key" ]; then
    ok "OSS_API_KEY set${oss_base:+ (base: $oss_base)}"
    [ -n "$oss_base" ] || warn "OSS_API_BASE is unset; set it in .envrc (see .envrc.template)"
  elif [ "$TIER" = "32gb" ]; then
    # local-big is hosted on this tier by design. With no key both hosted rungs
    # fail and the profile's fallback chain lands on local-big-degraded, which
    # answers -- worse. A silent quality drop deserves a loud warning.
    info ""
    printf '%s%s  OSS_API_KEY is NOT set, and this is the 32gb tier.%s\n' "$C_WARN$C_BOLD" "!!" "$C_RESET" >&2
    info "     local-big is HOSTED on 32gb. With no key every hosted rung fails and"
    info "     LiteLLM falls through to local-big-degraded: a small local model that"
    info "     answers immediately but drops details when compressing long context."
    info "     Nothing will error. You will simply get worse summaries."
    info "     Fix:   cp .envrc.template .envrc && \$EDITOR .envrc   # set OSS_API_KEY, then: direnv allow"
    info "     Check: bin/usage-report -- a nonzero fallback count means you are on that rung"
    info ""
    warn "continuing on the degraded rung -- local-fast and local-embed are unaffected"
  else
    warn "OSS_API_KEY unset; local-big has no remote fallback if Ollama dies (optional on $TIER)"
  fi

  if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    ok "ANTHROPIC_API_KEY set (optional)"
  else
    dim "      ANTHROPIC_API_KEY unset -- always optional; Claude Code authenticates itself"
  fi
}

# --------------------------------------------------------------------------
# 7. Proxy lifecycle
# --------------------------------------------------------------------------

# Prints the listening pid, or nothing (and returns 1) when the port is free.
port_pid() {
  local pid
  pid="$(lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true)"
  [ -n "$pid" ] || return 1
  printf '%s' "$pid"
}

pid_args() { ps -p "$1" -o args= 2>/dev/null | head -1 || true; }

pidfile_pid() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid="$(tr -dc '0-9' < "$PID_FILE")"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s' "$pid"
}

proxy_live() {
  curl -fsS -o /dev/null --max-time 3 \
    "http://$LITELLM_HOST:$LITELLM_PORT/health/liveliness" 2>/dev/null
}

port_report() {
  step "Ports"
  local pid
  if pid="$(port_pid "$OLLAMA_PORT")"; then
    ok "$OLLAMA_PORT ollama listening (pid $pid)"
  else
    warn "$OLLAMA_PORT nothing listening -- start the Ollama app or run: ollama serve"
  fi
  if pid="$(port_pid "$LITELLM_PORT")"; then
    if proxy_live; then
      ok "$LITELLM_PORT LiteLLM up and answering /health/liveliness (pid $pid)"
    else
      warn "$LITELLM_PORT occupied by pid $pid, which is not answering /health/liveliness"
      dim "      $(pid_args "$pid")"
    fi
  else
    info "  $LITELLM_PORT free (proxy not running)"
  fi
}

# Decides whether port 4000 is ours, a stale copy of ours, or somebody else's.
# Returns: 0 already-ours (reuse), 1 free, 2 foreign (caller must abort).
inspect_litellm_port() {
  local pid own args
  if ! pid="$(port_pid "$LITELLM_PORT")"; then
    return 1
  fi
  own="$(pidfile_pid || true)"
  args="$(pid_args "$pid")"
  if [ -n "$own" ] && [ "$own" = "$pid" ]; then
    return 0
  fi
  case "$args" in
    *litellm*) return 0 ;;  # a harness proxy we did not start; reuse it
  esac
  err "port $LITELLM_PORT is held by another program (pid $pid):"
  info "  $args"
  lsof -nP -iTCP:"$LITELLM_PORT" -sTCP:LISTEN >&2 || true
  info "  Free it, or change litellm.port in config/harness.yaml, then re-run."
  return 2
}

start_proxy() {
  step "LiteLLM proxy"

  local state=0
  inspect_litellm_port || state=$?
  case "$state" in
    0)
      local pid
      pid="$(port_pid "$LITELLM_PORT")"
      if proxy_live; then
        ok "already running on $LITELLM_HOST:$LITELLM_PORT (pid $pid); not starting a second one"
        # Adopt an existing healthy proxy so --stop can still reach it.
        [ "$DRY_RUN" = "true" ] || printf '%s' "$pid" > "$PID_FILE"
        return 0
      fi
      fail_soft "a litellm process (pid $pid) holds $LITELLM_PORT but is not healthy; try ./setup.sh --restart"
      return 1
      ;;
    2)
      exit 2
      ;;
  esac

  [ -n "$LITELLM_BIN" ] || resolve_litellm_bin || {
    fail_soft "litellm not installed; run ./setup.sh (without --start) to install it"
    return 1
  }
  [ -f "$PROFILE" ] || { fail_soft "cannot start: no profile at $PROFILE"; return 1; }

  # Profiles read master_key from os.environ/LITELLM_MASTER_KEY; harness_lib falls
  # back to the same default, so the no-secrets path just works.
  if [ -z "${LITELLM_MASTER_KEY:-}" ]; then
    export LITELLM_MASTER_KEY="$DEFAULT_MASTER_KEY"
    dim "      LITELLM_MASTER_KEY unset; using the harness default ($DEFAULT_MASTER_KEY)"
  fi

  local cmd="$LITELLM_BIN --config $PROFILE --host $LITELLM_HOST --port $LITELLM_PORT"
  dry "$cmd  >> $LOG_FILE 2>&1 &" && return 0

  mkdir -p "$VAR_DIR"
  info "  starting: $cmd"
  {
    printf '\n===== %s  setup.sh starting %s (tier %s) =====\n' \
      "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$PROFILE" "$TIER"
  } >> "$LOG_FILE"
  # cwd is pinned to the harness root so the profile's relative
  # disk_cache_dir (var/litellm-cache) always lands in var/, no matter
  # where setup.sh was invoked from.
  ( cd "$ROOT" && nohup "$LITELLM_BIN" --config "$PROFILE" --host "$LITELLM_HOST" --port "$LITELLM_PORT" \
    >> "$LOG_FILE" 2>&1 & echo $! > "$PID_FILE" )
  local pid
  pid="$(cat "$PID_FILE")"

  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    if proxy_live; then
      ok "up on http://$LITELLM_HOST:$LITELLM_PORT (pid $pid, ${waited}s)"
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done

  fail_soft "LiteLLM did not answer /health/liveliness within ${HEALTH_TIMEOUT}s"
  info "  log: $LOG_FILE"
  tail -n 15 "$LOG_FILE" >&2 || true
  rm -f "$PID_FILE"
  return 1
}

stop_proxy() {
  step "Stopping LiteLLM"
  local pid
  if ! pid="$(pidfile_pid)"; then
    rm -f "$PID_FILE"
    if pid="$(port_pid "$LITELLM_PORT")"; then
      warn "no live pidfile, but pid $pid still holds $LITELLM_PORT: $(pid_args "$pid")"
      dim "      this script only stops what it started; kill it yourself if it is yours"
    else
      ok "not running"
    fi
    return 0
  fi
  dry "kill $pid && rm $PID_FILE" && return 0

  kill "$pid" 2>/dev/null || true
  local waited=0
  while [ "$waited" -lt 10 ] && kill -0 "$pid" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    warn "pid $pid ignored SIGTERM; sending SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  ok "stopped (pid $pid)"
}

# --------------------------------------------------------------------------
# 8. Smoke test
# --------------------------------------------------------------------------

smoke_aliases() {
  local list="local-big local-fast local-embed"
  # With no key on 32gb, local-big cannot answer; also probe the degraded local
  # rung so the summary shows what *does* work offline.
  if [ -z "${OSS_API_KEY:-}" ] && [ "$TIER" = "32gb" ]; then
    local i
    for i in $(seq 0 $(( ${#MODEL_ALIASES[@]} - 1 ))); do
      [ "${MODEL_ALIASES[$i]}" = "local-big-degraded" ] && list="$list local-big-degraded"
    done
  fi
  printf '%s' "$list"
}

smoke_test() {
  step "Smoke test"
  local aliases
  # shellcheck disable=SC2207  # deliberate word-splitting of a space-separated list
  aliases=($(smoke_aliases))

  if [ "$DRY_RUN" = "true" ]; then
    local a
    for a in "${aliases[@]}"; do
      dry "probe $a through http://$LITELLM_HOST:$LITELLM_PORT/v1"
      SMOKE_RESULTS+=("$a"$'\t'"skipped"$'\t'"dry run")
    done
    return 0
  fi
  if ! proxy_live; then
    fail_soft "proxy is not answering; skipping smoke test"
    local a
    for a in "${aliases[@]}"; do
      SMOKE_RESULTS+=("$a"$'\t'"FAIL"$'\t'"proxy down")
    done
    return 1
  fi

  export HARNESS_BASE_URL="http://$LITELLM_HOST:$LITELLM_PORT/v1"
  local out rc=0 line alias status detail
  out="$(run_py "$PY_SMOKE" "${aliases[@]}")" || rc=$?
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    SMOKE_RESULTS+=("$line")
    IFS=$'\t' read -r alias status detail <<< "$line"
    if [ "$status" = "PASS" ]; then
      ok "$alias: $detail"
    else
      warn "$alias: $detail"
    fi
  done <<< "$out"
  [ "$rc" -eq 0 ] || fail_soft "one or more aliases failed their smoke test"
  return 0
}

# --------------------------------------------------------------------------
# 9. Summary
# --------------------------------------------------------------------------

summary() {
  info ""
  printf '%s%s%s\n' "$C_BOLD" "Harness summary" "$C_RESET" >&2
  printf '  tier        %s (%sGB RAM, detected %s)\n' "$TIER" "$RAM_GB" "$DETECTED_TIER" >&2
  printf '  profile     %s\n' "$PROFILE" >&2
  printf '  keep_alive  %s\n' "$KEEP_ALIVE" >&2
  printf '  proxy       http://%s:%s   log: %s\n' "$LITELLM_HOST" "$LITELLM_PORT" "$LOG_FILE" >&2

  if [ ${#SMOKE_RESULTS[@]} -gt 0 ]; then
    info ""
    local line alias status detail
    for line in "${SMOKE_RESULTS[@]}"; do
      IFS=$'\t' read -r alias status detail <<< "$line"
      printf '  %-20s %-8s %s\n' "$alias" "$status" "$detail" >&2
    done
  fi

  info ""
  printf '%sWhat to try first%s\n' "$C_BOLD" "$C_RESET" >&2
  info "  bin/init            generate config/ for your team and workspace"
  info "  bin/local health    re-check the proxy and every alias, any time"
  info "  /fix-issue <n>      in Claude Code: the first workflow worth running"
  if [ "$EXIT_CODE" -ne 0 ]; then
    info ""
    warn "finished with problems (exit $EXIT_CODE) -- see the warnings above"
  fi
}

# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

run_check() {
  preflight
  resolve_tier
  port_report
  step "Ollama models (tier $TIER)"
  if have ollama; then
    load_ollama_tags
    local i alias tag
    for i in $(seq 0 $(( ${#MODEL_ALIASES[@]} - 1 ))); do
      alias="${MODEL_ALIASES[$i]}"
      tag="${MODEL_TAGS[$i]}"
      case "$tag" in
        ollama/*)
          tag="${tag#ollama/}"
          if tag_present "$tag"; then
            ok "$alias -> $tag present"
          else
            warn "$alias -> $tag not pulled (./setup.sh pulls it)"
          fi
          ;;
        *) dim "      $alias -> $tag (hosted, nothing to pull)" ;;
      esac
    done
  else
    warn "no ollama on PATH; cannot list models"
  fi
  key_check
  step "Proxy"
  if proxy_live; then
    ok "LiteLLM answering on http://$LITELLM_HOST:$LITELLM_PORT"
    dim "      ./setup.sh --start runs the smoke test against it"
  else
    info "  not running -- start it with ./setup.sh --start"
  fi
  summary
  # --check is a diagnosis, not a verdict: it reports problems and still exits 0.
  exit 0
}

run_setup() {
  preflight
  brew_step
  litellm_step || true
  resolve_tier
  pull_models
  key_check
  if start_proxy; then
    smoke_test
  fi
  summary
}

run_start() {
  preflight
  resolve_tier
  key_check
  if start_proxy; then
    smoke_test
  fi
  summary
}

main() {
  parse_args "$@"
  [ "$DRY_RUN" = "true" ] || mkdir -p "$VAR_DIR"

  case "$MODE" in
    stop)
      stop_proxy
      exit 0
      ;;
    check)   run_check ;;
    start)   run_start ;;
    restart) stop_proxy; run_start ;;
    setup)   run_setup ;;
  esac

  exit "$EXIT_CODE"
}

main "$@"
