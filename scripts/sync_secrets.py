"""Create/overwrite a Modal secret from dotenv-formatted stdin.

Reads `op inject` output from a pipe so plaintext secrets never touch disk
(the modal CLI's --from-dotenv rejects process-substitution FIFOs).

Usage: op inject -i .env.tpl | uv run scripts/sync_secrets.py synapse
"""

import sys

import modal


def parse_dotenv(text: str) -> dict[str, str]:
    """Minimal KEY=VALUE parser (comments/blank lines skipped, matched quotes stripped)."""
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"usage: <dotenv on stdin> | {sys.argv[0]} <secret-name>")
    env = parse_dotenv(sys.stdin.read())
    if not env:
        sys.exit("No secrets parsed from stdin")
    # Same call `modal secret create --force` makes (modal/cli/secret.py).
    modal.Secret._create_deployed(sys.argv[1], env, overwrite=True)
    print(f"Synced {len(env)} keys to Modal secret '{sys.argv[1]}'")


if __name__ == "__main__":
    main()
