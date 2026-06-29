# Contributing to QConnect-AI

Thanks for contributing! This repository is a **monorepo** hosting three projects
(`qconnect-ai-cloud`, `qconnect-ai-edge`, `qconnect-ai-shared`).

## Development setup

```bash
python3 -m venv .venv && source .venv/bin/activate

# Shared library (install first — it's the wire contract)
pip install -e qconnect-ai-shared[dev]

# Cloud backend
pip install -r qconnect-ai-cloud/requirements.txt

# Edge services
pip install -r qconnect-ai-edge/requirements.txt
```

## Branching

- Feature work happens on `claude/...` or `feature/...` branches.
- Never push directly to `main`.
- Keep PRs scoped to a single project where possible; cross-cutting changes
  to the wire contract live in `qconnect-ai-shared` and must update both
  consumers.

## Code style

- Python 3.11+ (CI targets 3.11 and 3.12).
- `black` (line length 100) and `ruff` for linting.
- Full type hints; `mypy` for the shared library.
- Detailed docstrings on public functions and classes.

```bash
black .
ruff check .
```

## Tests

Each project ships its own `tests/`. Run everything with:

```bash
make test
```

or per project, e.g. `pytest qconnect-ai-shared/tests`.

All unit tests must pass **without** a live database or network — engines are
pure functions and external calls degrade gracefully.

## Commit messages

Use clear, imperative subject lines (e.g. `cloud: add Westgard R-4S rule`).

## Compliance reminder

This codebase targets ISO 15189 / GDPR-aware design. Never commit real patient
data, credentials, or `.env` files. Use `.env.example` for documentation.
