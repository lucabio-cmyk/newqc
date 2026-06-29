# Database migrations (Alembic)

Schema changes for the cloud Postgres database are managed with
[Alembic](https://alembic.sqlalchemy.org/). The canonical initial schema lives
in [`../postgres/init.sql`](../postgres/init.sql); the **baseline** migration
(`0001_baseline`) simply executes it, and every change after that is a normal,
reversible Alembic revision.

## Configuration

The database URL is read at runtime (no secrets in files), in this order:

1. `ALEMBIC_DATABASE_URL`
2. `DATABASE_URL` (the same variable the app uses)
3. `sqlalchemy.url` in `alembic.ini` (a harmless local default)

An async URL (`postgresql+asyncpg://…`) is automatically normalised to a sync
driver, so you can reuse the app's `DATABASE_URL`. Install the sync driver:

```bash
pip install alembic "psycopg[binary]"
```

All commands run from the `qconnect-ai-cloud/` directory (where `alembic.ini`
lives).

## Common commands

```bash
# Show revision history / current DB revision
alembic history --verbose
alembic current

# Apply all migrations to a fresh database
alembic upgrade head

# Preview the SQL without touching a database (offline mode)
alembic upgrade head --sql

# Roll back one revision
alembic downgrade -1

# Create a new revision (hand-edit the generated file)
alembic revision -m "add foo to bar"
```

## Existing databases

If a database was already initialised directly from `init.sql` (e.g. via the
docker-compose `init.sql` mount or `scripts/init_db.sh`), tell Alembic the
baseline is already applied instead of re-running it:

```bash
alembic stamp 0001_baseline
alembic upgrade head   # applies 0002 and later
```

## Authoring guidelines

- Keep each revision small and **reversible** — always implement `downgrade()`.
- Prefer explicit `op.*` calls; use `op.execute()` for Postgres-specific DDL
  (partial indexes, extensions, functions/triggers) as `0002` does.
- Migrations must not import application code — the env is intentionally
  ORM-free (`target_metadata = None`).
