"""Sign-in check: prove this run can reach Claude, without spending anything.

Exchanges the run's GitHub identity token through the federation rule in federation.json, then lists the models
the token can see (a free call; no message is sent). Fails unless the run used federation, so a leftover
ANTHROPIC_API_KEY cannot make it pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402


def check() -> list[str]:
    """The IDs of the models the federated token can see. Raises when federation is not what this run would use."""
    import anthropic

    method = common.auth_method()
    if method != "federation":
        raise SystemExit(f"auth={method}: this run would not use federation ({common.NO_CLAUDE}).")
    settings = common.federation_settings()
    print(f"auth=federation rule={settings['federation_rule_id']} "
          f"service_account={settings['service_account_id']} workspace={settings['workspace_id'] or 'from the rule'}")
    client = anthropic.Anthropic(max_retries=2, **common.client_kwargs())
    return [model.id for model in client.models.list(limit=20)]


def main() -> None:
    models = check()
    if not models:
        raise SystemExit("Signed in, but the token sees no models.")
    print(f"Signed in through federation. The token sees {len(models)} models, e.g. {', '.join(models[:5])}.")


if __name__ == "__main__":
    main()
