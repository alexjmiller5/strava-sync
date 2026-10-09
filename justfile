set shell := ["bash", "-cu"]

default:
    @just --list

test:
    uv run pytest

# All static analysis (read-only, CI-safe)
check:
    uv run ruff check . && uv run ruff format --check .

fmt:
    uv run ruff format . && uv run ruff check --fix .

# Import one Strava bulk export zip; hub URL and token come from .env.tpl (flags: --prune, --timezone Z)
run zip *flags:
    op run --env-file=.env.tpl -- uv run strava-sync import "{{zip}}" {{flags}}
