"""Shared helpers for the Helper-Bot scripts: Claude calls, repo context, JSON parsing, GitHub output."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5-5"

# Models that still accept a fixed thinking budget. Newer models reject
# {"type": "enabled", "budget_tokens": N} with a 400 and take adaptive thinking instead.
BUDGET_THINKING_MODELS = {"claude-opus-4-6", "claude-sonnet-4-6"}

# Models whose safety classifiers can decline a request. With server-side fallbacks the API re-runs a
# declined request on the model Anthropic recommends for that kind of decline, inside the same call.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-fable-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Roughly 4 characters per token; keeps a request well inside a 1M-token context window.
MAX_PROMPT_CHARS = 2_400_000

# Directories that are never part of the plugin's own source.
SKIP_DIRS = {".git", ".helper-bot", "build", ".gradle", "out", "node_modules", ".idea"}

# Workload identity federation: the IDs from the Claude Console, kept at the root of Helper-Bot so every plugin
# repository uses the same ones. They are identifiers, not secrets: without a GitHub token that matches the rule
# they let nobody in. Each can be overridden by the environment variable of the same meaning.
FEDERATION_FILE = Path(__file__).resolve().parents[2] / "federation.json"
FEDERATION_FIELDS = {
    "federation_rule_id": "ANTHROPIC_FEDERATION_RULE_ID",
    "organization_id": "ANTHROPIC_ORGANIZATION_ID",
    "service_account_id": "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "workspace_id": "ANTHROPIC_WORKSPACE_ID",
}
FEDERATION_REQUIRED = ("federation_rule_id", "organization_id", "service_account_id")
GITHUB_OIDC_AUDIENCE = "https://api.anthropic.com"


class TooLarge(Exception):
    """The prompt would not fit; the caller reports it instead of silently truncating."""


def model_name() -> str:
    return os.environ.get("CLAUDE_MODEL", "").strip() or DEFAULT_MODEL


def thinking_params(model: str, budget: int | None) -> dict | None:
    """The thinking setting for a request, or None to send no thinking parameter."""
    if budget is None:
        return None
    if model in BUDGET_THINKING_MODELS:
        return {"type": "enabled", "budget_tokens": budget}
    return {"type": "adaptive"}


def request_params(model: str, system: str, user: str, *, max_tokens: int, effort: str,
                   thinking_budget: int | None) -> dict:
    """The keyword arguments for one Messages request on `model`."""
    params: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    thinking = thinking_params(model, thinking_budget)
    if thinking is not None:
        params["thinking"] = thinking
    if model not in BUDGET_THINKING_MODELS:
        # Thinking depth on current models; Claude Opus 5.5 defaults to "medium", so always say.
        params["output_config"] = {"effort": effort}
    if model in FALLBACK_MODELS:
        params["betas"] = [FALLBACK_BETA]
        params["fallbacks"] = "default"
    return params


def answer_text(content) -> str:
    """The answer's text. After a fallback, only what the model that finished wrote."""
    last_fallback = max((i for i, block in enumerate(content) if block.type == "fallback"), default=-1)
    return "".join(block.text for block in content[last_fallback + 1 :] if block.type == "text")


def federation_settings() -> dict | None:
    """The federation IDs from federation.json and the environment, or None until the required ones are set."""
    try:
        stored = json.loads(FEDERATION_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        stored = {}
    settings = {key: os.environ.get(env, "").strip() or str(stored.get(key) or "").strip()
                for key, env in FEDERATION_FIELDS.items()}
    if not all(settings[key] for key in FEDERATION_REQUIRED):
        return None
    return settings


def github_oidc_available() -> bool:
    """GitHub sets these only in a job with `id-token: write`, and never for a pull request from a fork."""
    return bool(os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"))


def github_identity_token() -> str:
    """A new GitHub Actions identity token for Anthropic.

    Fetched on every call: Anthropic accepts each token only once (its `jti`), and the SDK calls this again for
    every token exchange, so a long run never re-sends a used one.
    """
    import requests

    resp = requests.get(
        os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"],
        params={"audience": GITHUB_OIDC_AUDIENCE},
        headers={"Authorization": f"Bearer {os.environ['ACTIONS_ID_TOKEN_REQUEST_TOKEN']}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["value"]


def auth_method() -> str | None:
    """How this run reaches Claude: "federation", "api_key", or None when it cannot.

    Federation comes first, so a leftover ANTHROPIC_API_KEY secret never quietly takes over once it is set up.
    """
    if federation_settings() is not None and github_oidc_available():
        return "federation"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "api_key"
    return None


def claude_available() -> bool:
    return auth_method() is not None


NO_CLAUDE = ("no Claude credentials in this run: federation needs federation.json filled in and `id-token: write` "
             "(a pull request from a fork never gets one), else an ANTHROPIC_API_KEY secret")


def client_kwargs() -> dict:
    """Credentials for anthropic.Anthropic(), passed explicitly so an empty environment variable never decides."""
    method = auth_method()
    if method == "federation":
        from anthropic import WorkloadIdentityCredentials

        settings = federation_settings()
        return {"credentials": WorkloadIdentityCredentials(
            identity_token_provider=github_identity_token,
            federation_rule_id=settings["federation_rule_id"],
            organization_id=settings["organization_id"],
            service_account_id=settings["service_account_id"],
            workspace_id=settings["workspace_id"] or None,
        )}
    if method == "api_key":
        return {"api_key": os.environ["ANTHROPIC_API_KEY"].strip()}
    raise RuntimeError(NO_CLAUDE)


def ask_claude(system: str, user: str, *, max_tokens: int, effort: str = "high",
               thinking_budget: int | None = None) -> str:
    """Send one request and return the text of the answer.

    Streams so that long thinking runs never hit the HTTP timeout.
    """
    import anthropic

    total = len(system) + len(user)
    if total > MAX_PROMPT_CHARS:
        raise TooLarge(f"prompt is {total:,} characters; the limit is {MAX_PROMPT_CHARS:,}")

    model = model_name()
    params = request_params(model, system, user, max_tokens=max_tokens, effort=effort,
                            thinking_budget=thinking_budget)
    client = anthropic.Anthropic(max_retries=4, **client_kwargs())
    messages = client.beta.messages if "betas" in params else client.messages
    with messages.stream(**params) as stream:
        message = stream.get_final_message()

    usage = message.usage
    print(
        f"[claude] auth={auth_method()} model={model} served_by={message.model} stop={message.stop_reason} "
        f"input_tokens={usage.input_tokens} output_tokens={usage.output_tokens}",
        file=sys.stderr,
    )
    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request (stop_reason=refusal), fallback included")
    if message.stop_reason == "max_tokens":
        print("[claude] warning: the answer hit max_tokens and may be cut short", file=sys.stderr)
    return answer_text(message.content)


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """The last JSON object in a reply: a ```json fence first, else the outermost braces."""
    for candidate in reversed(_FENCE.findall(text)):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        if isinstance(value, dict):
            return value
    return None


def iter_repo_files(root: Path, pattern: str):
    """Files under root matching a glob, skipping build output and the Helper-Bot checkout."""
    for path in sorted(root.rglob(pattern)):
        if path.is_file() and not any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            yield path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def file_block(label: str, body: str) -> str:
    return f'<file path="{label}">\n{body}\n</file>'


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def git_show(ref: str, path: str, cwd: Path | None = None) -> str | None:
    """A file's text at a commit, or None when it does not exist there."""
    result = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=cwd, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def load_repo_context(root: Path, ref: str | None = None) -> str:
    """`.github/BOT_CONTEXT.md` and `CLAUDE.md` from the calling repository, for the system prompt.

    With `ref` (a PR's base commit) they are read as they are on the base branch, so a pull request
    cannot rewrite the reviewer's own instructions.
    """
    parts = []
    for rel in (".github/BOT_CONTEXT.md", "CLAUDE.md"):
        text = git_show(ref, rel, cwd=root) if ref else None
        if text is None and (root / rel).is_file():
            text = read_text(root / rel)
        if text and text.strip():
            parts.append(f"## Repository notes from `{rel}`\n\n{text.strip()}")
    return "\n\n".join(parts)


def set_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def write_step_summary(markdown: str) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def env_flag(name: str, default: bool = True) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}
