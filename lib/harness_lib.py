"""Shared runtime library for the agent harness.

Every ``bin/`` script and the MCP offload server imports this module. It owns:

* locating the harness root and the generated ``config/`` directory
* hardware tier detection and the tier -> model mapping (with overrides)
* a dependency-light OpenAI-compatible client pointed at LiteLLM
* the usage journal that ``bin/usage-report`` reads

Only stdlib plus PyYAML. Scripts that import it are ``uv`` PEP-723 scripts and
declare ``pyyaml`` themselves; see ``bin/local`` for the canonical shebang.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - surfaced as a friendly error
    yaml = None  # type: ignore[assignment]

__all__ = [
    "ALIASES",
    "REMOTE_ALIAS",
    "DEFAULT_MODELS",
    "TIERS",
    "HarnessError",
    "Completion",
    "Config",
    "harness_root",
    "config_dir",
    "var_dir",
    "detect_ram_gb",
    "detect_tier",
    "resolve_tier",
    "tier_models",
    "load_config",
    "load_repos",
    "save_yaml",
    "load_yaml",
    "chat",
    "embed",
    "litellm_up",
    "log_usage",
    "read_usage",
    "read_input",
    "chunk_text",
    "info",
    "warn",
    "error",
    "ok",
    "step",
    "die",
    "bold",
    "dim",
]

# --------------------------------------------------------------------------
# Alias contract. Nothing downstream of this file may name a concrete model.
# --------------------------------------------------------------------------

ALIASES = ("local-big", "local-fast", "local-embed")
REMOTE_ALIAS = "local-big-remote"

TIERS = ("128gb", "64gb", "32gb")

# Model picks are pinned in exactly two places: here (defaults) and
# config/tier-overrides.yaml (per-team pins). Verify tags with `ollama list` /
# `ollama show <tag>`; setup.sh warns rather than fails on a missing tag,
# because these move every quarter.
#
# Avoid reasoning/thinking models for these roles. Measured on qwen3.6:35b: a
# one-sentence summary cost 4,249 characters of hidden thinking, and at any
# sane max_tokens the budget is exhausted before a single content token is
# emitted -- the caller gets an empty string. Compression and drafting are
# high-volume, low-judgment jobs; they want a model that answers immediately.
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "128gb": {
        "local-big": "ollama/llama3.3:70b",
        "local-fast": "ollama/qwen2.5-coder:14b",
        "local-embed": "ollama/nomic-embed-text",
        "local-big-remote": "openai/qwen2.5-72b-instruct",
        "keep_alive": "30m",
    },
    "64gb": {
        "local-big": "ollama/qwen2.5:32b",
        "local-fast": "ollama/qwen2.5-coder:14b",
        "local-embed": "ollama/nomic-embed-text",
        "local-big-remote": "openai/qwen2.5-72b-instruct",
        "keep_alive": "15m",
    },
    "32gb": {
        # local-big is hosted on this tier: a weak local summarizer that drops
        # details costs more than it saves. The local tag below is the degraded
        # no-key path -- setup.sh warns loudly when it is what you get.
        "local-big": "openai/qwen2.5-72b-instruct",
        "local-big-degraded": "ollama/qwen2.5-coder:14b",
        "local-fast": "ollama/qwen2.5-coder:7b",
        "local-embed": "ollama/nomic-embed-text",
        "local-big-remote": "openai/qwen2.5-72b-instruct",
        "keep_alive": "2m",
    },
}

DEFAULT_PORT = 4000
DEFAULT_HOST = "127.0.0.1"
DEFAULT_MASTER_KEY = "sk-harness-local"
OLLAMA_BASE = "http://127.0.0.1:11434"


class HarnessError(RuntimeError):
    """Raised for user-facing failures; callers print and exit non-zero."""


class Completion(str):
    """The assistant text, plus how it was actually produced.

    This subclasses ``str`` on purpose: every caller can keep treating a
    completion as plain text, while the ones that care can ask which model
    really answered. That matters because of the fallback ladder -- asking for
    ``local-big`` on a keyless 32GB box gets you the small local model, and a
    caller with no way to notice cannot be honest about it. ``bin/local
    health`` and the MCP tools use these fields to say so out loud.
    """

    __slots__ = (
        "alias",
        "model_resolved",
        "route",
        "fallback",
        "prompt_tokens",
        "completion_tokens",
        "duration_ms",
    )

    def __new__(cls, text: str, **meta: Any) -> "Completion":
        obj = super().__new__(cls, text)
        obj.alias = meta.get("alias", "")
        obj.model_resolved = meta.get("model_resolved", "")
        obj.route = meta.get("route", "local")
        obj.fallback = bool(meta.get("fallback", False))
        obj.prompt_tokens = int(meta.get("prompt_tokens") or 0)
        obj.completion_tokens = int(meta.get("completion_tokens") or 0)
        obj.duration_ms = int(meta.get("duration_ms") or 0)
        return obj

    @property
    def degraded_note(self) -> str:
        """One line a tool can surface when the answer came from a fallback."""
        if not self.fallback:
            return ""
        return (
            f"answered by {self.model_resolved}, not {self.alias} "
            f"-- a fallback rung was used, so treat this output as degraded"
        )


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def harness_root() -> Path:
    """Root of the harness checkout.

    ``HARNESS_ROOT`` wins so the MCP server can be launched from anywhere;
    otherwise walk up from this file (``lib/harness_lib.py`` -> root).
    """
    env = os.environ.get("HARNESS_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    return harness_root() / "config"


def var_dir() -> Path:
    d = harness_root() / "var"
    d.mkdir(parents=True, exist_ok=True)
    return d


def bootstrap_path() -> None:
    """Convenience for scripts: put ``lib/`` on sys.path. Idempotent."""
    lib = str(harness_root() / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)


# --------------------------------------------------------------------------
# YAML helpers
# --------------------------------------------------------------------------


def _require_yaml():
    if yaml is None:
        raise HarnessError(
            "PyYAML is not available. Run this script via `uv run --script` "
            "(the shebang does this) or `pip install pyyaml`."
        )
    return yaml


def load_yaml(path: Path, default: Any = None) -> Any:
    y = _require_yaml()
    if not path.exists():
        if default is not None:
            return default
        raise HarnessError(f"missing file: {path}")
    with path.open() as fh:
        loaded = y.safe_load(fh)
    if loaded is None:
        return default if default is not None else {}
    return loaded


def save_yaml(path: Path, data: Any, header: str | None = None) -> None:
    y = _require_yaml()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = y.safe_dump(data, sort_keys=False, default_flow_style=False, width=100)
    if header:
        text = "".join(f"# {line}\n" for line in header.strip().splitlines()) + text
    path.write_text(text)


# --------------------------------------------------------------------------
# Hardware tier
# --------------------------------------------------------------------------


def detect_ram_gb() -> int:
    """Physical RAM in GB. macOS via sysctl, Linux via /proc/meminfo."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True
            )
            return round(int(out.stdout.strip()) / (1024**3))
        meminfo = Path("/proc/meminfo")
        if meminfo.exists():
            for line in meminfo.read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / (1024**2))
    except Exception:
        pass
    return 16


def detect_tier(ram_gb: int | None = None) -> str:
    """Map RAM to a profile name. Anything under 48GB gets the hosted rung."""
    ram = detect_ram_gb() if ram_gb is None else ram_gb
    if ram >= 96:
        return "128gb"
    if ram >= 48:
        return "64gb"
    return "32gb"


def resolve_tier(value: str | None) -> str:
    """Turn a configured tier value into a concrete tier.

    ``None`` / ``"auto"`` re-detect from hardware, which is what makes a
    ``config/`` directory portable between machines.
    """
    if not value or str(value).lower() == "auto":
        return detect_tier()
    tier = str(value).lower()
    if tier not in DEFAULT_MODELS:
        raise HarnessError(f"unknown tier {value!r}; expected auto or one of {', '.join(TIERS)}")
    return tier


def tier_models(tier: str, overrides_path: Path | None = None) -> dict[str, str]:
    """Defaults for ``tier``, with ``config/tier-overrides.yaml`` applied."""
    tier = resolve_tier(tier)
    models = dict(DEFAULT_MODELS[tier])
    path = overrides_path or (config_dir() / "tier-overrides.yaml")
    if path.exists():
        data = load_yaml(path, default={}) or {}
        models.update({k: v for k, v in (data.get("models") or {}).items() if v})
        if data.get("keep_alive"):
            models["keep_alive"] = data["keep_alive"]
    return models


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass
class Config:
    """Parsed ``config/harness.yaml`` -- everything bin/init generates."""

    team: str = "workspace"
    workspace_root: Path = field(default_factory=lambda: Path.home() / "workspace")
    tier_setting: str = "auto"
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    oss_enabled: bool = False
    frontier_enabled: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def tier(self) -> str:
        """Concrete tier: re-detected when ``tier_setting`` is ``auto``."""
        return resolve_tier(self.tier_setting)

    @property
    def tier_is_pinned(self) -> bool:
        return bool(self.tier_setting) and str(self.tier_setting).lower() != "auto"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    @property
    def profile_path(self) -> Path:
        return harness_root() / "litellm" / "profiles" / f"{self.tier}.yaml"

    @property
    def models(self) -> dict[str, str]:
        return tier_models(self.tier_setting)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "team": self.team,
            "workspace_root": str(self.workspace_root),
            # "auto" keeps config/ portable across machines; pin a tier to override.
            "tier": self.tier_setting,
            "litellm": {"host": self.host, "port": self.port},
            "oss": {
                "enabled": self.oss_enabled,
                "api_base_env": "OSS_API_BASE",
                "api_key_env": "OSS_API_KEY",
            },
            "frontier": {"enabled": self.frontier_enabled, "api_key_env": "ANTHROPIC_API_KEY"},
        }


def load_config(required: bool = True) -> Config:
    """Load ``config/harness.yaml``; fall back to detected defaults if absent."""
    path = config_dir() / "harness.yaml"
    if not path.exists():
        if required:
            raise HarnessError(
                "no config/harness.yaml -- run `bin/init` first "
                "(or `/harness-init` inside Claude Code)."
            )
        return Config()
    data = load_yaml(path, default={}) or {}
    litellm = data.get("litellm") or {}
    default_ws = str(Path.home() / "workspace")
    return Config(
        team=data.get("team", "workspace"),
        workspace_root=Path(str(data.get("workspace_root") or default_ws)).expanduser(),
        tier_setting=str(data.get("tier") or "auto"),
        host=litellm.get("host", DEFAULT_HOST),
        port=int(litellm.get("port", DEFAULT_PORT)),
        oss_enabled=bool((data.get("oss") or {}).get("enabled")),
        frontier_enabled=bool((data.get("frontier") or {}).get("enabled")),
        raw=data,
    )


def load_repos() -> list[dict[str, Any]]:
    """Entries from ``config/repos.yaml``; empty list if not configured yet."""
    data = load_yaml(config_dir() / "repos.yaml", default={}) or {}
    repos = data.get("repos") or []
    out: list[dict[str, Any]] = []
    for r in repos:
        if isinstance(r, str):
            r = {"remote": r, "name": r.rstrip("/").split("/")[-1].removesuffix(".git")}
        if not r.get("name") and r.get("remote"):
            r["name"] = str(r["remote"]).rstrip("/").split("/")[-1].removesuffix(".git")
        out.append(r)
    return out


# --------------------------------------------------------------------------
# LiteLLM / OpenAI-compatible client
# --------------------------------------------------------------------------


def _api_key() -> str:
    return os.environ.get("LITELLM_MASTER_KEY") or DEFAULT_MASTER_KEY


def _post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_api_key()}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def litellm_up(cfg: Config | None = None, timeout: float = 2.0) -> bool:
    """True if the LiteLLM proxy answers on its liveness endpoint."""
    cfg = cfg or load_config(required=False)
    url = f"http://{cfg.host}:{cfg.port}/health/liveliness"
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True  # answering at all means the proxy is up
    except Exception:
        return False


def chat(
    prompt: str | None = None,
    *,
    model: str = "local-big",
    system: str | None = None,
    messages: Sequence[dict[str, str]] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: float = 600.0,
    cfg: Config | None = None,
    tool: str = "chat",
) -> str:
    """One completion against a harness alias. Returns the assistant text.

    ``model`` must be an alias (``local-big`` / ``local-fast``), never a
    concrete model name -- that mapping lives in the LiteLLM profile.
    """
    cfg = cfg or load_config(required=False)
    if messages is None:
        if prompt is None:
            raise HarnessError("chat() needs either prompt or messages")
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
    payload = {
        "model": model,
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    started = time.time()
    try:
        data = _post(f"{cfg.base_url}/chat/completions", payload, timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        log_usage(
            tool=tool,
            alias=model,
            ok=False,
            duration_ms=int((time.time() - started) * 1000),
            detail=detail[:200],
        )
        raise HarnessError(f"{model}: HTTP {exc.code} from LiteLLM -- {detail}") from exc
    except urllib.error.URLError as exc:
        log_usage(
            tool=tool,
            alias=model,
            ok=False,
            duration_ms=int((time.time() - started) * 1000),
            detail=str(exc.reason)[:200],
        )
        raise HarnessError(
            f"cannot reach LiteLLM at {cfg.base_url} ({exc.reason}). "
            "Start it with `./setup.sh --start`."
        ) from exc

    usage = data.get("usage") or {}
    resolved = str(data.get("model") or model)
    duration_ms = int((time.time() - started) * 1000)
    fell_back = _is_fallback(resolved, model)
    log_usage(
        tool=tool,
        alias=model,
        model_resolved=resolved,
        route=_route_of(resolved, model),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        duration_ms=duration_ms,
        fallback=fell_back,
        ok=True,
    )
    choices = data.get("choices") or []
    if not choices:
        raise HarnessError(f"{model}: empty response from LiteLLM")
    text = (choices[0].get("message") or {}).get("content") or ""
    if not text.strip():
        # Reasoning models burn the whole budget thinking and return nothing.
        # Failing loudly beats handing a caller an empty brief it will treat
        # as a real answer. See the note above DEFAULT_MODELS.
        raise HarnessError(
            f"{model}: {resolved} returned no content ({usage.get('completion_tokens', 0)} "
            "completion tokens used). This is what a reasoning/thinking model does when "
            "its budget is spent before it emits an answer -- pin a non-reasoning model "
            "for this alias in config/tier-overrides.yaml, or raise max_tokens."
        )
    if fell_back:
        _warn_fallback_once(model, resolved)
    return Completion(
        text,
        alias=model,
        model_resolved=resolved,
        route=_route_of(resolved, model),
        fallback=fell_back,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        duration_ms=duration_ms,
    )


def embed(
    texts: Sequence[str],
    *,
    model: str = "local-embed",
    timeout: float = 300.0,
    cfg: Config | None = None,
    tool: str = "embed",
) -> list[list[float]]:
    """Embedding vectors for ``texts``, in order."""
    cfg = cfg or load_config(required=False)
    started = time.time()
    try:
        data = _post(f"{cfg.base_url}/embeddings", {"model": model, "input": list(texts)}, timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise HarnessError(f"{model}: HTTP {exc.code} from LiteLLM -- {detail}") from exc
    except urllib.error.URLError as exc:
        raise HarnessError(
            f"cannot reach LiteLLM at {cfg.base_url} ({exc.reason}). Is the proxy running?"
        ) from exc
    usage = data.get("usage") or {}
    log_usage(
        tool=tool,
        alias=model,
        model_resolved=str(data.get("model") or model),
        route="local",
        prompt_tokens=usage.get("prompt_tokens", 0),
        duration_ms=int((time.time() - started) * 1000),
        ok=True,
    )
    rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
    return [r["embedding"] for r in rows]


def _route_of(resolved: str, alias: str) -> str:
    low = resolved.lower()
    if "claude" in low or low.startswith("gpt-") or "gpt-4" in low or "gpt-5" in low:
        return "frontier"
    if "ollama" in low:
        return "local"
    if alias.endswith("-remote") or "remote" in low:
        return "remote"
    return "local"


def _is_fallback(resolved: str, alias: str) -> bool:
    """A fallback fired when LiteLLM answered with something other than the alias."""
    return bool(resolved) and resolved != alias and not resolved.startswith(alias)


_WARNED_FALLBACKS: set[tuple[str, str]] = set()


def _warn_fallback_once(alias: str, resolved: str) -> None:
    """Say it out loud the first time an alias degrades, then stay quiet.

    Silent degradation is the failure mode this whole harness is meant to make
    visible: a machine quietly falling back all day shows up in the numbers,
    not the invoice. Warned once per alias->model pair per process so a long
    session does not turn into a wall of repeats. Goes to stderr, which is
    safe for the MCP server (stdout is the protocol).
    """
    if os.environ.get("HARNESS_QUIET_FALLBACK"):
        return
    key = (alias, resolved)
    if key in _WARNED_FALLBACKS:
        return
    _WARNED_FALLBACKS.add(key)
    warn(f"{alias} answered by {resolved} (fallback rung) -- output quality is degraded")


# --------------------------------------------------------------------------
# Usage journal -- the input to bin/usage-report
# --------------------------------------------------------------------------

USAGE_LOG = "usage.jsonl"


def log_usage(**record: Any) -> None:
    """Append one JSONL row to ``var/usage.jsonl``. Never raises."""
    if os.environ.get("HARNESS_NO_USAGE_LOG"):
        return
    try:
        record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        record.setdefault("session", os.environ.get("HARNESS_SESSION", ""))
        with (var_dir() / USAGE_LOG).open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def read_usage(since: datetime | None = None) -> list[dict[str, Any]]:
    """All journal rows, optionally filtered to those newer than ``since``."""
    path = var_dir() / USAGE_LOG
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since is not None:
            try:
                ts = datetime.fromisoformat(str(row.get("ts")))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < since:
                continue
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# CLI ergonomics
# --------------------------------------------------------------------------


def read_input(source: str | None) -> str:
    """Read a file, ``-`` for stdin, or stdin when ``source`` is None."""
    if source in (None, "-"):
        if sys.stdin.isatty():
            raise HarnessError("no input: pass a file path or pipe text on stdin")
        return sys.stdin.read()
    path = Path(str(source)).expanduser()
    if not path.exists():
        raise HarnessError(f"no such file: {source}")
    return path.read_text(errors="replace")


def chunk_text(text: str, size: int = 6000, overlap: int = 400) -> Iterator[str]:
    """Split long text into overlapping windows for map-reduce summarization."""
    if len(text) <= size:
        yield text
        return
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        yield text[start:end]
        if end == len(text):
            return
        start = max(end - overlap, start + 1)


_COLOR = sys.stderr.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def info(msg: str) -> None:
    print(msg, file=sys.stderr)


def step(msg: str) -> None:
    print(_c("1;36", "==>") + " " + msg, file=sys.stderr)


def ok(msg: str) -> None:
    print(_c("32", "  ok ") + msg, file=sys.stderr)


def warn(msg: str) -> None:
    print(_c("33", "warn ") + msg, file=sys.stderr)


def error(msg: str) -> None:
    print(_c("31", "error ") + msg, file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    error(msg)
    raise SystemExit(code)
