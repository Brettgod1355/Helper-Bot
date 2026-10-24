"""Count a RuneLite plugin's tokens the way the Plugin Hub's review bot limits them.

The review bot only reviews a plugin whose Java is under 200,000 tokens with comments stripped (maintainers on
runelite/plugin-hub #15139: "Only java files in src/ with their comments stripped count"). Its code is not public.
Calibrated on 2026-10-24 against seven counts the bot published (`.github/token-calibration.json`):

- Anthropic's count_tokens does not reproduce them: the bot's number is 0.51-0.60 of it (Claude Opus 4.7 and later)
  or 0.68-0.83 (Claude 4.6 and older), varying by plugin.
- OpenAI's o200k_base tokenizer, over every .java under src/ except src/test/, comments stripped, comes closest:
  the bot's number is 0.947-1.062 of it (two plugins within 0.2%). No method matched to the token.

So the count here is o200k_base (free, offline, no API), reported with the range the bot could give.

    token_count.py count <dir>               print the report for a checked-out plugin
    token_count.py comment                   the /tokens comment command (issue_comment event; see README)
    token_count.py calibrate <targets.json>  count each listed commit in every scope and tokenizer against the Hub
                                             bot's number; CALIBRATE_MODELS=all (or a list) adds Claude count_tokens
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402

LIMIT = 200_000
COMMENT_MARKER = "<!-- helper-bot:tokens -->"
ENCODING = "o200k_base"
# The Hub bot's published count divided by ours, lowest and highest over the calibration targets (2026-10-24).
BOT_RATIO = (0.947, 1.062)
TARGET = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:@(?P<ref>[A-Za-z0-9_./-]+))?$")


# ---------- stripping ----------

def strip_java_comments(source: str) -> str:
    """Java source without its comments (line, block and Javadoc); string, char and text-block literals untouched.

    A line comment leaves its newline; a block comment leaves the newlines it spanned, so line numbers keep.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            end = source.find("\n", i)
            i = n if end < 0 else end
        elif c == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            stop = n if end < 0 else end + 2
            out.append("\n" * source.count("\n", i, stop))
            i = stop
        elif source.startswith('"""', i):
            end = i + 3
            while True:
                end = source.find('"""', end)
                if end < 0:
                    end = n
                    break
                backslashes = 0
                k = end - 1
                while k >= 0 and source[k] == "\\":
                    backslashes += 1
                    k -= 1
                if backslashes % 2 == 0:
                    end += 3
                    break
                end += 1
            out.append(source[i:end])
            i = end
        elif c in "\"'":
            j = i + 1
            while j < n and source[j] != c and source[j] != "\n":
                j += 2 if source[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(source[i:j])
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class JavaFile:
    path: str
    text: str           # comments stripped
    original: str = ""  # as committed


def java_files(root: Path, under: str = "src") -> list[JavaFile]:
    base = root / under
    files = []
    for path in sorted(base.rglob("*.java")) if base.is_dir() else []:
        original = path.read_text(encoding="utf-8", errors="replace")
        files.append(JavaFile(path.relative_to(root).as_posix(), strip_java_comments(original), original))
    return files


def review_scope(files: list[JavaFile]) -> list[JavaFile]:
    """What the bot appears to count: every .java under src/ except src/test/ (calibration, 2026-10-24)."""
    return [f for f in files if not f.path.startswith("src/test/")]


# ---------- counting ----------

def tiktoken_counter(name: str = ENCODING):
    import tiktoken

    encoding = tiktoken.get_encoding(name)
    return lambda text: len(encoding.encode(text, disallowed_special=())) if text else 0


def claude_counter(client, model: str):
    """Anthropic count_tokens for `text` alone: the request's count minus the same request around one character."""
    def raw(content: str) -> int:
        return client.messages.count_tokens(model=model, messages=[{"role": "user", "content": content}]).input_tokens

    return lambda text: raw(text) - raw(".") + 1 if text else 0


@dataclass
class Report:
    total: int
    files: list[tuple[str, int]] = field(default_factory=list)  # (path, tokens), largest first
    file_count: int = 0


def measure(count, files: list[JavaFile]) -> Report:
    total = count("\n".join(f.text for f in files))
    sizes = sorted(((f.path, count(f.text)) for f in files), key=lambda s: s[1], reverse=True)
    return Report(total, sizes[:10], len(files))


def bot_range(total: int) -> tuple[int, int]:
    return round(total * BOT_RATIO[0]), round(total * BOT_RATIO[1])


def render(report: Report, target: str) -> str:
    low, high = bot_range(report.total)
    gap = LIMIT - report.total
    if high < LIMIT:
        verdict = f"✅ under the 200,000 limit even at the top of that range ({LIMIT - high:,} to spare)"
    elif low >= LIMIT:
        verdict = f"❌ over the 200,000 limit even at the bottom of that range ({low - LIMIT:,} too many)"
    else:
        verdict = "⚠️ too close to call: the limit falls inside that range"
    lines = [
        COMMENT_MARKER,
        f"### 🔢 Tokens: `{target}`",
        "",
        f"**{report.total:,} tokens** ({gap:+,} from 200,000) in {report.file_count} `.java` files under `src/` "
        f"(not `src/test/`), comments stripped.",
        "",
        f"The Plugin Hub's review bot would likely say **{low:,} to {high:,}**: {verdict}.",
    ]
    if report.files:
        lines += ["", "| Largest files | Tokens |", "|---|---:|"]
        lines += [f"| `{path}` | {tokens:,} |" for path, tokens in report.files]
    lines += ["", f"<sub>Counted with OpenAI's `{ENCODING}` tokenizer. The review bot's method is not public: on "
                  f"seven counts it published, its number was {BOT_RATIO[0]:.3f} to {BOT_RATIO[1]:.3f} times this one "
                  "(Helper-Bot calibration, 2026-10-24).</sub>"]
    return "\n".join(lines)


# ---------- checking out ----------

def fetch_commit(repo: str, ref: str, dest: Path, token: str = "") -> str:
    """A shallow checkout of repo@ref into dest; returns the commit SHA."""
    url = f"https://github.com/{repo}.git"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    auth = ["-c", f"http.extraheader=AUTHORIZATION: basic {_basic(token)}"] if token else []
    dest.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> str:
        return subprocess.run(["git", *auth, *args], cwd=dest, check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    run("init", "-q")
    run("fetch", "-q", "--depth", "1", url, ref)
    run("checkout", "-q", "FETCH_HEAD")
    return run("rev-parse", "HEAD")


def _basic(token: str) -> str:
    import base64

    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


# ---------- the /tokens comment command ----------

def parse_command(body: str) -> tuple[bool, str]:
    """(is a /tokens command, its argument or '')."""
    first = (body or "").strip().splitlines()[0].strip() if (body or "").strip() else ""
    if not re.match(r"^/tokens(\s|$)", first):
        return False, ""
    parts = first.split()
    return True, parts[1] if len(parts) > 1 else ""


def resolve_target(arg: str, here_repo: str, gh_repo) -> tuple[str, str, str]:
    """(repo, ref to fetch, label) from the argument, else this repository: the PR's head, or the default branch."""
    if arg:
        match = TARGET.match(arg)
        if not match:
            raise ValueError(f"`{arg}` is not `owner/repo` or `owner/repo@ref`")
        repo = match["repo"]
        ref = match["ref"] or "HEAD"
        return repo, ref, f"{repo}@{match['ref']}" if match["ref"] else repo
    issue = gh_repo.get_issue(int(os.environ["ISSUE_NUMBER"]))
    if issue.pull_request is not None:
        pr = gh_repo.get_pull(issue.number)
        return pr.head.repo.full_name, pr.head.sha, f"{pr.head.repo.full_name}@{pr.head.sha[:7]} (PR #{issue.number})"
    return here_repo, gh_repo.default_branch, f"{here_repo}@{gh_repo.default_branch}"


def full_sha(gh, repo: str, ref: str) -> str:
    """The 40-character commit for a short SHA, branch, tag or HEAD: git fetches a commit only by its full SHA.

    (`/tokens owner/repo@7a5f3b7` failed with "couldn't find remote ref 7a5f3b7", 2026-10-24.)
    """
    gh_repo = gh.get_repo(repo)
    return gh_repo.get_commit(gh_repo.default_branch if ref == "HEAD" else ref).sha


def count_target(repo: str, ref: str, label: str, token: str = "") -> str:
    with tempfile.TemporaryDirectory() as tmp:
        sha = fetch_commit(repo, ref, Path(tmp), token)
        report = measure(tiktoken_counter(), review_scope(java_files(Path(tmp))))
    if sha[:7] not in label:
        label = f"{label} ({sha[:7]})"
    return render(report, label)


def comment_main() -> None:
    from github import Auth, Github

    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    comment = event.get("comment") or {}
    owner = os.environ.get("TOKENS_OWNER", "").strip()
    login = (comment.get("user") or {}).get("login", "")
    is_command, arg = parse_command(comment.get("body", ""))
    if not is_command:
        print("Not a /tokens comment; nothing to do.")
        return
    if not owner or login != owner:
        print(f"/tokens is the owner's only ({owner or 'no owner set'}); {login} asked. Nothing to do.")
        return
    gh = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
    here = os.environ["GITHUB_REPOSITORY"]
    gh_repo = gh.get_repo(here)
    dry_run = common.env_flag("DRY_RUN", default=True)
    try:
        repo, ref, label = resolve_target(arg, here, gh_repo)
        token = os.environ["GITHUB_TOKEN"] if repo.lower() == here.lower() else ""
        body = count_target(repo, full_sha(gh, repo, ref), label, token)
    except Exception as exc:  # the reply says what went wrong instead of a silent red run
        detail = (getattr(exc, "stderr", "") or str(exc)).strip()[:300]
        body = f"{COMMENT_MARKER}\n🔢 `/tokens {arg}`: could not count ({type(exc).__name__}: {detail})."
    print(body)
    common.write_step_summary(body)
    if dry_run:
        print("Dry run: no reply posted.")
        return
    gh_repo.get_issue(int(os.environ["ISSUE_NUMBER"])).create_comment(body)


# ---------- calibration ----------

SCOPES = {
    "src-minus-test": lambda files: "\n".join(f.text for f in review_scope(files)),
    "src": lambda files: "\n".join(f.text for f in files),
    "main-only": lambda files: "\n".join(f.text for f in files if f.path.startswith("src/main/java/")),
    "src-minus-test, comments kept": lambda files: "\n".join(f.original for f in review_scope(files)),
    "src-minus-test, whitespace collapsed": lambda files: collapse_whitespace(
        "\n".join(f.text for f in review_scope(files))),
}


def calibrate_main(path: str) -> None:
    targets = json.loads(Path(path).read_text(encoding="utf-8"))
    counters = {name: tiktoken_counter(name) for name in (ENCODING, "cl100k_base")}
    wanted = os.environ.get("CALIBRATE_MODELS", "").strip()
    if wanted:
        import anthropic

        client = anthropic.Anthropic(max_retries=4, **common.client_kwargs())
        models = [m.id for m in client.models.list(limit=100)] if wanted == "all" else wanted.split(",")
        counters.update({f"claude:{m.strip()}": claude_counter(client, m.strip()) for m in models if m.strip()})
    rows = ["| Target | Hub bot | Scope | Counter | Count | Bot ÷ count |", "|---|---:|---|---|---:|---:|"]
    for t in targets:
        expected = t.get("expected")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                sha = fetch_commit(t["repo"], t["commit"], Path(tmp))
            except subprocess.CalledProcessError as exc:  # history rewritten since the bot counted it
                print(f"== {t['repo']}@{t['commit'][:7]}: cannot fetch ({(exc.stderr or '').strip()[:120]}); skipped")
                rows.append(f"| `{t['repo']}@{t['commit'][:7]}` | {expected or ''} | gone | | | |")
                continue
            files = java_files(Path(tmp))
            print(f"== {t['repo']}@{sha[:7]}: {len(files)} files, bot {expected}")
            for index, (scope, build) in enumerate(SCOPES.items()):
                text = build(files)
                # Every counter on the main scope; the other scopes with the main tokenizer only.
                for name, count in counters.items() if index == 0 else [(ENCODING, counters[ENCODING])]:
                    try:
                        n = count(text)
                    except Exception as exc:
                        print(f"   {scope} {name}: {type(exc).__name__}: {str(exc)[:160]}")
                        continue
                    ratio = f"{expected / n:.4f}" if expected and n else ""
                    print(f"   {scope:38} {name:32} {n:>9,} {ratio}")
                    rows.append(f"| `{t['repo']}@{sha[:7]}` | {expected or ''} | {scope} | {name} | {n:,} | {ratio} |")
    common.write_step_summary("\n".join(rows))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "comment":
        comment_main()
    elif mode == "count" and len(sys.argv) > 2:
        print(render(measure(tiktoken_counter(), review_scope(java_files(Path(sys.argv[2])))), sys.argv[2]))
    elif mode == "calibrate" and len(sys.argv) > 2:
        calibrate_main(sys.argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
