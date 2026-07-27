# Dependencies for the agent harness.
#
# `setup.sh` runs `brew bundle --file=Brewfile` for you and skips the whole step
# when every formula is already present. Both are idempotent: re-running installs
# nothing that is already there, so this file doubles as the update path.
#
# Note on ollama: if you installed the Ollama Mac app, the CLI is already on your
# PATH and setup.sh will not touch this formula. Homebrew and the app fight over
# the same server port, so pick one.
#
# LiteLLM is deliberately absent: it has no Homebrew formula. setup.sh installs it
# with `uv tool install "litellm[proxy]"`, which is why uv is pulled in below.

brew "ollama"    # runs the local models behind local-big / local-fast / local-embed
brew "gh"        # issue/PR access for /fix-issue and /review-pr
brew "mise"      # per-repo runtime pinning so agents get the same toolchain you do
brew "direnv"    # loads .envrc secrets per directory instead of into your shell profile
brew "jq"        # JSON wrangling in hooks and the usage journal
brew "uv"        # runs every bin/ script as a self-contained PEP-723 script
brew "ripgrep"   # fast literal search; the cheap first pass before any semantic search
