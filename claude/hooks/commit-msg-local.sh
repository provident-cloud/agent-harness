#!/usr/bin/env bash
# commit-msg-local.sh -- git prepare-commit-msg hook.
#
# Drafts the commit message from the staged diff with a local model, so writing
# commit messages never costs frontier tokens. The message is only a prefill:
# git opens the editor as usual and you edit or delete it.
#
# Install (per repo -- symlink so harness updates land automatically):
#
#     ln -sf /path/to/agent-harness/claude/hooks/commit-msg-local.sh \
#            .git/hooks/prepare-commit-msg
#
# Install for every repo at once, if you keep no other global hooks:
#
#     mkdir -p ~/.git-hooks
#     ln -sf /path/to/agent-harness/claude/hooks/commit-msg-local.sh \
#            ~/.git-hooks/prepare-commit-msg
#     git config --global core.hooksPath ~/.git-hooks
#
# Uninstall: remove the symlink. Skip once: HARNESS_COMMIT_MSG_SKIP=1 git commit
#
# git calls this with $1=message file, $2=source, $3=commit sha.
#
# This hook can never fail a commit. Everything below is best-effort and the
# EXIT trap forces status 0: a tool that can stop you from committing is worse
# than no tool at all.

set -euo pipefail
trap 'exit 0' EXIT

msg_file=${1:-}
msg_source=${2:-}

[[ -n "$msg_file" && -f "$msg_file" ]] || exit 0

if [[ "${HARNESS_COMMIT_MSG_SKIP:-0}" == "1" ]]; then
  exit 0
fi

# Leave every case where the message already exists or is not ours to write:
#   message  -- -m/-F/--message was supplied
#   merge    -- merge commit, git's message describes the merge
#   squash   -- squash/fixup, the collected messages matter
#   commit   -- --amend or -c/-C, reusing an existing message
#   template -- the project chose a template on purpose
case "$msg_source" in
  message | merge | squash | commit | template) exit 0 ;;
esac

# A rebase, cherry-pick, or merge in progress means git owns the message.
git_dir=$(git rev-parse --git-dir 2>/dev/null || true)
if [[ -n "$git_dir" ]]; then
  for marker in MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD rebase-merge rebase-apply; do
    if [[ -e "$git_dir/$marker" ]]; then
      exit 0
    fi
  done
fi

# Never overwrite text that is already in the file (a commit.template, or a
# retry after an aborted commit). Comments and blank lines do not count.
if grep -vE '^#|^[[:space:]]*$' "$msg_file" 2>/dev/null | grep -q .; then
  exit 0
fi

# Resolve the harness root through the symlink this file is usually installed as.
src=${BASH_SOURCE[0]}
while [[ -L "$src" ]]; do
  target=$(readlink "$src")
  if [[ "$target" == /* ]]; then
    src=$target
  else
    src=$(cd -P "$(dirname "$src")" && pwd)/$target
  fi
done
script_dir=$(cd -P "$(dirname "$src")" && pwd)
harness_root=${HARNESS_ROOT:-$(cd -P "$script_dir/../.." && pwd)}
local_bin="$harness_root/bin/local"

[[ -x "$local_bin" ]] || exit 0

staged=$(git diff --staged --no-color 2>/dev/null || true)
[[ -n "$staged" ]] || exit 0

# Wall-clock cap. If LiteLLM is down or a model is cold, the request must not
# leave someone staring at a hung `git commit`.
timeout_secs=${HARNESS_COMMIT_MSG_TIMEOUT:-45}

# macOS ships no timeout(1), so the fallback below is the path that normally
# runs here.
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
      # Kill the child's children too: `uv run` spawns python, and an orphaned
      # python holding the output pipe would keep `git commit` waiting well
      # past the timeout.
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

draft=$(printf '%s\n' "$staged" | run_limited "$local_bin" draft commit 2>/dev/null || true)

# Anything unexpected -- empty output, a stack trace, a usage message, or a wall
# of text where a subject line belongs -- is treated as "no draft available".
[[ -n "$draft" ]] || exit 0
case "$draft" in
  Traceback* | usage:* | Usage:* | error:* | Error:*) exit 0 ;;
esac
if [[ ${#draft} -gt 4000 ]]; then
  exit 0
fi

tmp="$msg_file.harness.$$"
{
  printf '%s\n' "$draft"
  printf '\n# Drafted locally by the agent harness (local-fast). Edit freely;\n'
  printf '# clear the message to abort the commit.\n'
  cat "$msg_file"
} >"$tmp" 2>/dev/null || exit 0

mv -f "$tmp" "$msg_file" 2>/dev/null || rm -f "$tmp"

exit 0
