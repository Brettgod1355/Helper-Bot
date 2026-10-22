# Notes for the bot

Copy to `.github/BOT_CONTEXT.md` in a plugin repository and edit. The PR audit and the issue triager add this
file to Claude's instructions. The PR audit reads it from the PR's base branch, so a PR cannot change it for its
own review.

## Known quirks players report

- **"I get no notification."** RuneLite hides notifications while the client window is focused unless
  *RuneLite > Notifications > Send notifications when focused* is on (it is off by default). The plugin sends the
  notification either way; RuneLite decides whether it shows.

## What the plugin does not do, on purpose

- Anything the Plugin Hub rules or Jagex's third-party client rules forbid (automation, extra boss-mechanic
  indicators, menu changes that save clicks). Explain politely that it is not allowed, not that it is planned.
