# Helper-Bot

GitHub Actions automation shared by my RuneLite plugins: a PR audit and an issue triager that each plugin
repository calls as a reusable workflow, and a scheduled watcher that turns a Plugin Hub update into a GitHub
release and a Discord announcement.

Everything starts in **dry run**: it prints what it would post and changes nothing until you switch it on.

| What | File | Runs |
|---|---|---|
| PR audit | `.github/workflows/pr-audit.yml` (reusable) | when a plugin repository calls it on a pull request |
| Issue triage | `.github/workflows/issue-triage.yml` (reusable) | when a plugin repository calls it on a new issue |
| Hub release watcher | `.github/workflows/hub-release-watcher.yml` | every 30 minutes, and by hand |
| Self test | `.github/workflows/self-test.yml` | on every push and PR here (offline tests, no API calls) |
| Auth check | `.github/workflows/auth-check.yml` | by hand, and when the sign-in code or `federation.json` changes: signs in to Claude by federation and lists models, free |

## Reaching Claude: workload identity federation (no API key)

The workflows reach Claude without any stored key. Each run asks GitHub for a short-lived identity token that says
which repository and which workflow it is, and Anthropic swaps it for an access token that lasts minutes. One rule
in the Claude Console decides who may do that, so a new plugin repository needs nothing added in the Console or in
its secrets: only the caller files below. ([Anthropic's guide](https://platform.claude.com/docs/en/manage-claude/wif-providers/github-actions))

Set up once, in the Claude Console (an organization admin or owner):

1. **Settings > Workload identity > Connect workload > GitHub Actions.** The wizard registers the issuer
   (`https://token.actions.githubusercontent.com`), then the rule, then a service account. The rule as set up on
   2026-10-22:

   | Field | Value |
   |---|---|
   | GitHub organization | `Brettgod1355` (a user account works), numeric ID `50088793` |
   | Repository, Branch / ref | blank: every repository you own, any branch or pull request |
   | Allowed events | Any event (the condition below narrows it) |
   | Advanced: subject pattern | `repo:Brettgod1355@50088793/*`: GitHub's `sub` carries the owner and repository IDs (`repo:Brettgod1355@50088793/Helper-Bot@1430063823:ref:refs/heads/staging`), so the wizard's `repo:Brettgod1355/*` never matches |
   | Advanced: claims | `repository_owner` = `Brettgod1355`, `repository_owner_id` = `50088793` |
   | Advanced: audience | blank (the default, `https://api.anthropic.com`) |
   | Advanced: CEL condition | `claims.event_name != "pull_request_target" && ((has(claims.job_workflow_ref) && claims.job_workflow_ref.startsWith("Brettgod1355/Helper-Bot/.github/workflows/")) \|\| claims.workflow_ref.startsWith("Brettgod1355/Helper-Bot/.github/workflows/"))` |
   | Workspaces | one: Default (the Console's credit is the spending cap; a workspace with its own limit can come later) |
   | OAuth scope | `workspace:developer` (the wizard offers no narrower one; never `org:admin`) |
   | Token lifetime | 10 minutes |
   | Service account | new, `helper-bot` |

   In words: a run in one of your own repositories, running one of Helper-Bot's workflows, never on
   `pull_request_target` (the trigger that would run a fork's pull request with your repository's permissions).
   `job_workflow_ref` is the reusable workflow a plugin repository calls; `workflow_ref` covers the release watcher,
   which runs here. Someone else's repository fails the owner checks, and a pull request from a fork gets no
   identity token from GitHub at all.
2. The rule ID (`fdrl_...`), service account ID (`svac_...`), organization ID and workspace ID are in
   `federation.json`. They are identifiers, not secrets (Anthropic calls them safe to commit): without a GitHub
   token that passes the rule they let nobody in. The wizard's test step shows them all.

Each job that calls Claude has `id-token: write`, and so must each caller (the examples have it). The run log says
which way it authenticated (`auth=federation`); if an exchange is refused, the Console's Workload identity >
Authentication history shows why. To test the sign-in alone, run **Actions > Auth check** (free: it lists models
and sends no message).

An `ANTHROPIC_API_KEY` secret still works as a fallback while `federation.json` is empty, but federation wins
whenever both are there, and you never need the key.

## Secrets and variables

Add these under **Settings > Secrets and variables > Actions**.

### In this repository (Helper-Bot)

| Name | Kind | What it is |
|---|---|---|
| `GH_PAT` | Secret | A fine-grained personal access token (GitHub > Settings > Developer settings > Fine-grained tokens). Repository access: only the plugin repositories in `plugins.json`. Permissions: **Contents: Read and write** (tags and releases), Metadata: Read. Give it an expiry and renew it. |
| `DISCORD_WEBHOOK_URL` | Secret | Discord channel > Edit Channel > Integrations > Webhooks > New Webhook > Copy Webhook URL. Optional: without it, no Discord post. |
| `HUB_WATCHER_LIVE` | Variable | `true` lets the **scheduled** watcher publish. Leave unset while testing: scheduled runs then only log that the pin moved and spend no Claude tokens. |
| `RELEASE_TAGGER_NAME`, `RELEASE_TAGGER_EMAIL` | Variables | Optional. The tagger written into the annotated `v<version>` tag, e.g. your GitHub no-reply address. Without them GitHub uses the token's owner. |
| `CLAUDE_MODEL` | Variable | Optional. Overrides the watcher's model (default `claude-opus-5-5`). |

### In each plugin repository

| Name | Kind | What it is |
|---|---|---|
| `BOT_APP_CLIENT_ID` | Variable | Optional. Your helper bot's GitHub App Client ID (see "The helper bot's own account" below). |
| `BOT_APP_PRIVATE_KEY` | Secret | Optional. That app's private key: the whole `.pem` file's text. |

The PR audit posts with the run's own `GITHUB_TOKEN`, as `github-actions[bot]`. The issue triager does too until
you give it its own account.

## Visibility

**This repository is public**, because a public repository can only call reusable workflows from a public
repository. Its secrets stay private, and it holds no Claude key: access comes from the federation rule above.

## Who can use it, and on what

- **Only your own repositories spend your credits.** Anyone can call these public workflows from their own
  repository, but their runs carry their own name as `repository_owner`, so the federation rule refuses them.
- **Forks:** a fork belongs to someone else, and a pull request from a fork gets no identity token from GitHub
  (the example PR audit skips them anyway). Only your own branches and the issues on your own repositories reach
  Claude.
- **Each repository gets only its own context.** The workflows check out the repository that called them and read
  its own `.github/BOT_CONTEXT.md` and `CLAUDE.md`; nothing from another plugin is ever sent. Each repository's
  caller also sets its own inputs (dry run, which audits, `maintainer`, `bug_tip`, the daily limit).
- **Private repositories pay for Actions minutes** (public ones do not). Issue triage is for public plugins, where
  players open issues; private ones only need the PR audit.

## Adding it to a plugin repository

From the PC, per repository:

1. Copy `examples/pr-audit.yml` (every plugin) and `examples/issue-triage.yml` (public plugins) into its
   `.github/workflows/`, and optionally `examples/BOT_CONTEXT.md` to `.github/BOT_CONTEXT.md`. Commit them the way
   that repository takes changes.
2. Nothing for Claude: the federation rule already covers every repository you own. For the bot's own account,
   `gh variable set BOT_APP_CLIENT_ID -R Brettgod1355/<repo>` and
   `gh secret set BOT_APP_PRIVATE_KEY -R Brettgod1355/<repo> < your-app.private-key.pem`, and install the app on
   that repository.
3. Test in dry run (open a test PR; run "Issue triage" by hand on an issue), then set `dry_run: false`.

## PR audit

Inputs: `run_hub_compliance` (default true), `run_deep_stress_test` (default true), `dry_run` (default true),
plus `pr_number` (to re-run on a PR by hand), `model` (default `claude-opus-5-5`) and `helper_bot_ref`
(default `main`).

- **Hub compliance:** sends the diff, `build.gradle`, `runelite-plugin.properties` and the full text of each
  changed source file (so `startUp()` and `shutDown()` are seen together). It checks two things: everything
  `startUp()` registers is unregistered in `shutDown()`, and there is no reflection, raw `HttpURLConnection` or
  `client.*` call off the client thread.
- **Deep stress test:** sends the whole `src/main/java` and the diff with extended thinking at high effort
  and asks for an adversarial audit: login, hop and `GameState` resets, scene-unload despawns, same-tick event
  order, per-frame allocations and leaks.
- **Scorecard:** each audit prints its scorecard to the log and the run summary. With `dry_run: false`, one
  comment on the PR holds both, and it is edited in place on every push instead of adding new comments.

The audit reads `BOT_CONTEXT.md` and `CLAUDE.md` from the PR's **base** branch, so a PR cannot rewrite the
reviewer's instructions. PRs from forks are skipped in the example caller: GitHub gives them no identity token,
and their code should not decide what a paid API is asked.

## Issue triage

Inputs: `issue_number` (defaults to the event's issue), `dry_run` (default true), `maintainer` (how the bot names
you, e.g. `BrettG`), `max_issues_per_day` (default 10), `bug_tip` (optional Markdown added to bug replies),
`app_client_id` (your bot's GitHub App), `model`.

**A new issue.** Claude reads it against the plugin's whole `src/main/java`, its `README.md` and the maintainer's
notes (`.github/BOT_CONTEXT.md`, `CLAUDE.md`), decides what it really is, replies, and gives it one label:

| Label | When | The reply |
|---|---|---|
| `setting` | An existing setting or a known quirk explains it | "This looks like a setting rather than a bug": the section, the setting and what to choose, by the names players see |
| `bug` | The plugin seems to do something wrong | Thanks, what it understood, and what is missing (version, steps, a screenshot) |
| `enhancement` | A request for something new | Thanks, and the idea in a sentence; never a promise |
| `question` | Anything else | An answer if the code or README has one |

A wrong label from the issue form (say `bug` on what is a setting) is swapped; labels the bot does not manage are
left alone. A `setting` answer is followed by **"Did this fix it? Please reply here and let us know whether it
worked."** A `bug` answer is followed by `bug_tip`, if you set one. Every reply ends with a footer such as *"🤖 I'm
BrettG's helper bot, so I can be wrong."* Issues you open yourself are skipped (run it by hand to test on one). It
never answers an issue twice, never closes one on its own judgment, and answers at most `max_issues_per_day` new
issues in any 24 hours, so a burst of spam cannot run through your credits; the rest simply wait for you.

**Did the setting fix it?** Only for issues labeled `setting`, and only from the person who opened the issue:

- They **reply** that it worked (seen within a minute): the bot thanks them and closes the issue as completed.
- They reply that it didn't: the bot puts `bug` back, adds `needs-maintainer`, and says you will take a look.
- No reply: nothing happens. Reactions are not read (GitHub cannot start a workflow on one), which is why the
  answer asks for a reply.

The `setting` and `needs-maintainer` labels are created the first time they are needed.

### The helper bot's own account

Without it, replies come from `github-actions[bot]`. With a GitHub App they come from your bot, with its name, a
"bot" badge and its own avatar, the way `runelite-github-app` answers on the Plugin Hub.

1. GitHub > Settings > Developer settings > GitHub Apps > **New GitHub App**. Name it (unique on GitHub, at most 34
   characters), any homepage URL, untick Webhook **Active**, and under Repository permissions set **Issues: Read
   and write**. "Only on this account".
2. On the app's page: copy the **Client ID**, then **Generate a private key** (a `.pem` file downloads).
3. **Install App** on the plugin repositories it should answer in.
4. In each of those repositories: variable `BOT_APP_CLIENT_ID` = the Client ID, secret `BOT_APP_PRIVATE_KEY` = the
   `.pem` file's text. The example caller already passes both.

## Hub release watcher

`plugins.json` lists the plugins to watch:

```json
[{"hub_name": "cargo-hold-alert", "repo": "Brettgod1355/AFK-Shipwrecks", "display_name": "AFK Shipwrecks",
  "discord": {"role_id": "<role to ping>", "closing_line": "<last line of the post>"}}]
```

`discord` is optional. `hub_name` is the file name under `plugins/` in runelite/plugin-hub, which keeps a plugin's
first name after a rename (AFK Shipwrecks is still `cargo-hold-alert`).

Every run, for each plugin:

1. Reads `https://raw.githubusercontent.com/runelite/plugin-hub/master/plugins/<hub_name>` (no Claude call) and
   checks that its `repository=` is the plugin's repository.
2. Compares its `commit=` with the commit of the plugin's latest GitHub release. Same commit: done.
3. When the pin has moved, collects the commits and the diff since the latest release (or since the previous Hub
   pin when there is no release yet), adds the previous release's notes as an example of the voice to match, and
   asks Claude for player-friendly notes.
4. Live: creates the annotated tag `v<version>` (version from `build.gradle` at the pinned commit) on **exactly**
   the pinned commit and publishes the release `<display_name> <version>` in the same format as the releases made by
   hand: "On the Plugin Hub <date>." (the day the Hub entry changed), then only the sections that apply, in this
   order: ⚠️ Heads-up, 🎯 Accuracy, 🐛 Bug Fixes, ⚙️ Settings and Defaults, ✨ New Features, 🔧 Improvements,
   📚 Wiki and Media, each bullet with a bold lead-in, and the **Full Changelog** compare link.
5. Posts the announcement to Discord in the owner's layout: the role ping, `# 📢 <Plugin> <version> is out!`, a few
   themed `## <emoji> <topic>` sections with an optional intro line, "🧹 Everything else", the closing line, and a
   link to the release.

Safety: a tag that already exists on a different commit is never moved (the run says so and skips); a version
that is already released is never published twice; one plugin's failure does not stop the others.

Testing: **Actions > Hub release watcher > Run workflow** with `dry_run` ticked writes notes for the latest change
even when nothing moved, and prints the release and the Discord post without publishing. Scheduled runs publish
only once the `HUB_WATCHER_LIVE` variable is `true`.

GitHub pauses scheduled workflows in a public repository after 60 days without commits; re-enable it under
Actions if that happens.

## Cost

Rough figures at Claude Opus 5.5 prices ($4 per million input tokens, $20 per million output tokens; thinking is
billed as output); the run log prints the real token counts of every request.

| Run | Sends | Roughly |
|---|---|---|
| Hub compliance | diff + changed files, ~10-30k tokens, high effort | $0.10-0.40 |
| Deep stress test | whole `src/main/java`, ~50-150k tokens, high effort | $0.40-1.50 |
| Issue triage | the plugin's whole source + README, ~20-60k tokens, medium effort | $0.15-0.40 |
| Follow-up reply | the bot's answer + the player's reply, low effort | about $0.01 |
| Release notes | commits + diff since the last release, medium effort | $0.15-0.80 |
| Watcher with nothing new | Plugin Hub file and one GitHub API call per plugin | $0 |

The deep stress test re-runs on every push to a PR. To spend less, drop `synchronize` from the caller's
`pull_request` types (it then runs when the PR is opened or marked ready), or set `run_deep_stress_test: false`
and run it by hand with `pr_number`.

## Model

Everything defaults to `claude-opus-5-5` and can be switched with the `model` input or the `CLAUDE_MODEL`
variable.

- Thinking is always on with Claude Opus 5.5; how much it thinks is set by effort: **high** for both PR audits,
  **medium** for issue triage and release notes.
- **Server-side fallback is on** (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`). If Claude's
  safety classifiers decline a request (an audit that hunts for bugs can occasionally trip one), the API re-runs it
  on the model Anthropic recommends for that kind of decline, in the same call, and only that model's answer is
  used. The log line `served_by=` shows which model answered. A request that every model declines fails the run.
- Older models still work through the same input: Opus 4.6 and Sonnet 4.6 get a fixed 16,000-token thinking
  budget for the stress test instead of effort and fallbacks.

## Development

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pyflakes
.venv/bin/python -m pytest -q tests
```

The tests are offline. They build a throwaway git repository with a PR, stub Claude, fake the GitHub objects, and
run the real Anthropic SDK against a fake HTTP endpoint to check the request it sends.
