"""Count a RuneLite plugin's tokens the way the Plugin Hub's review bot limits them.

The review bot only reviews a plugin whose Java under src/ is under 200,000 tokens with comments stripped
(maintainers on runelite/plugin-hub #15139: "Only java files in src/ with their comments stripped count"). Its code
is not public, so this counts with the Anthropic API's free messages.count_tokens and is checked against the bot's
own published numbers (`calibrate`, and the research file in the owner's notes).

    token_count.py count <dir>               print the report for a checked-out plugin
    token_count.py comment                   the /tokens comment command (issue_comment event; see README)
    token_count.py calibrate <targets.json>  count each listed commit in every variant and model, against the
                                             Hub bot's number
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
# The model whose tokenizer matched the Hub bot best in calibration; COUNT_MODEL overrides it.
COUNT_MODEL = "claude-opus-5-5"
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


# ---------- counting ----------

def _client():
    import anthropic

    return anthropic.Anthropic(max_retries=4, **common.client_kwargs())


def count_text(client, model: str, text: str) -> int:
    """Tokens of `text` alone: the request's count minus the count for the same request around one character."""
    def raw(content: str) -> int:
        return client.messages.count_tokens(model=model, messages=[{"role": "user", "content": content}]).input_tokens

    return raw(text) - raw(".") + 1 if text else 0


@dataclass
class Report:
    total: int
    files: list[tuple[str, int]] = field(default_factory=list)  # (path, tokens), largest first
    file_count: int = 0


def measure(client, model: str, files: list[JavaFile], per_file: bool = True) -> Report:
    total = count_text(client, model, "\n".join(f.text for f in files))
    sizes = []
    if per_file:
        # Only the biggest files by size are counted one by one: the report lists ten.
        for f in sorted(files, key=lambda f: len(f.text), reverse=True)[:15]:
            sizes.append((f.path, count_text(client, model, f.text)))
        sizes.sort(key=lambda s: s[1], reverse=True)
    return Report(total, sizes[:10], len(files))


def render(report: Report, target: str, model: str, note: str = "") -> str:
    gap = LIMIT - report.total
    verdict = (f"✅ **{gap:,} under** the 200,000 limit" if gap >= 0
               else f"❌ **{-gap:,} over** the 200,000 limit")
    lines = [
        COMMENT_MARKER,
        f"### 🔢 Tokens: `{target}`",
        "",
        f"**{report.total:,} tokens**: {verdict} for the Plugin Hub's review bot "
        f"({report.file_count} `.java` files under `src/`, comments stripped).",
    ]
    if report.files:
        lines += ["", "| Largest files | Tokens |", "|---|---:|"]
        lines += [f"| `{path}` | {tokens:,} |" for path, tokens in report.files]
    lines += ["", f"<sub>Counted with `{model}`'s tokenizer (Anthropic `count_tokens`, free).{(' ' + note) if note else ''}"
              "</sub>"]
    return "\n".join(lines)


# ---------- checking out ----------

def fetch_commit(repo: str, ref: str, dest: Path, token: str = "") -> str:
    """A shallow checkout of repo@ref into dest; returns the commit SHA."""
    url = f"https://github.com/{repo}.git"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    auth = ["-c", f"http.extraheader=AUTHORIZATION: basic {_basic(token)}"] if token else []
    dest.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(["git", *auth, *args], cwd=dest, check=True, env=env,  # noqa: E731
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
    if not common.claude_available():
        print(f"{common.NO_CLAUDE}; nothing to do.")
        return
    gh = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"]))
    here = os.environ["GITHUB_REPOSITORY"]
    gh_repo = gh.get_repo(here)
    dry_run = common.env_flag("DRY_RUN", default=True)
    try:
        repo, ref, label = resolve_target(arg, here, gh_repo)
        with tempfile.TemporaryDirectory() as tmp:
            token = os.environ["GITHUB_TOKEN"] if repo.lower() == here.lower() else ""
            sha = fetch_commit(repo, ref, Path(tmp), token)
            label = f"{label} ({sha[:7]})" if sha[:7] not in label else label
            model = os.environ.get("COUNT_MODEL", "").strip() or COUNT_MODEL
            report = measure(_client(), model, java_files(Path(tmp)))
        body = render(report, label, model, os.environ.get("TOKENS_NOTE", "").strip())
    except Exception as exc:  # the reply says what went wrong instead of a silent red run
        body = f"{COMMENT_MARKER}\n🔢 `/tokens {arg}`: could not count ({type(exc).__name__}: {exc})."
    print(body)
    common.write_step_summary(body)
    if dry_run:
        print("Dry run: no reply posted.")
        return
    gh_repo.get_issue(int(os.environ["ISSUE_NUMBER"])).create_comment(body)


# ---------- calibration ----------

def _main(files):
    return [f for f in files if f.path.startswith("src/main/java/")]


# The first variant is counted with every model; the rest only with the first model.
VARIANTS = {
    "main-stripped": lambda files: "\n".join(f.text for f in _main(files)),
    "src-stripped": lambda files: "\n".join(f.text for f in files),
    "main-kept": lambda files: "\n".join(f.original for f in _main(files)),
    "main-stripped-collapsed": lambda files: collapse_whitespace("\n".join(f.text for f in _main(files))),
}


def calibrate_main(path: str) -> None:
    targets = json.loads(Path(path).read_text(encoding="utf-8"))
    client = _client()
    wanted = os.environ.get("CALIBRATE_MODELS", "").strip()
    if wanted == "all":
        models = [m.id for m in client.models.list(limit=100)]
    else:
        models = [m.strip() for m in (wanted or COUNT_MODEL).split(",") if m.strip()]
    print(f"Models: {', '.join(models)}")
    rows = ["| Target | Hub bot | Variant | Model | Count | Count − bot | Count ÷ bot |", "|---|---:|---|---|---:|---:|---:|"]
    for t in targets:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                sha = fetch_commit(t["repo"], t["commit"], Path(tmp))
            except subprocess.CalledProcessError as exc:  # history rewritten since the bot counted it
                print(f"== {t['repo']}@{t['commit'][:7]}: cannot fetch ({(exc.stderr or '').strip()[:120]}); skipped")
                rows.append(f"| `{t['repo']}@{t['commit'][:7]}` | {t.get('expected') or ''} | gone | | | | |")
                continue
            files = java_files(Path(tmp))
            chars = len(VARIANTS["main-stripped"](files))
            print(f"== {t['repo']}@{sha[:7]}: {len(files)} files, {chars:,} stripped characters, bot {t.get('expected')}")
            for index, (variant, build) in enumerate(VARIANTS.items()):
                text = build(files)
                for model in models if index == 0 else models[:1]:
                    try:
                        n = count_text(client, model, text)
                    except Exception as exc:
                        print(f"   {variant} {model}: {type(exc).__name__}: {str(exc)[:160]}")
                        continue
                    expected = t.get("expected")
                    diff = f"{n - expected:+,}" if expected else ""
                    ratio = f"{n / expected:.4f}" if expected else ""
                    print(f"   {variant:20} {model:24} {n:>9,} {diff:>9} {ratio}")
                    rows.append(f"| `{t['repo']}@{sha[:7]}` | {expected or ''} | {variant} | {model} | {n:,} | {diff} | {ratio} |")
    common.write_step_summary("\n".join(rows))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "comment":
        comment_main()
    elif mode == "count" and len(sys.argv) > 2:
        model = os.environ.get("COUNT_MODEL", "").strip() or COUNT_MODEL
        print(render(measure(_client(), model, java_files(Path(sys.argv[2]))), sys.argv[2], model))
    elif mode == "calibrate" and len(sys.argv) > 2:
        calibrate_main(sys.argv[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
