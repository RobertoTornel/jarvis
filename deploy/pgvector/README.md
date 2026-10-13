# claire-pg — pgvector PostgreSQL 17 (deploy/pgvector)

Reproduces the running `claire-pg` container:

| | |
|---|---|
| Container | `claire-pg` |
| Image | `pgvector/pgvector:pg17` (PostgreSQL **17.11**, pgvector **0.8.7**) |
| Port | `127.0.0.1:5432 -> 5432` |
| Volume | named volume `claire-pgdata17` -> `/var/lib/postgresql/data` |
| Restart | `unless-stopped` |
| Healthcheck | `pg_isready -U claire -d claire`, interval 5s, timeout 5s, retries 12 |
| Databases | `claire`, `claire_test` (both with the `vector` extension) |

The `POSTGRES_PASSWORD` is **not** stored in this repo. It lives in
`~/.config/claire/pg.env` as `CLAIRE_PG_PASSWORD` (plus `CLAIRE_PG_DSN`,
`CLAIRE_PG_TEST_DSN`, `CLAIRE_PG_USER`, `CLAIRE_PG_DB`, `CLAIRE_PG_TEST_DB`,
`CLAIRE_PG_HOST`, `CLAIRE_PG_PORT`). The compose file reads it from the
environment as `${CLAIRE_PG_PASSWORD:?...}`, so you must source that file first.

## Start

```bash
set -a; . ~/.config/claire/pg.env; set +a
docker compose -f deploy/pgvector/docker-compose.yml up -d
```

Then run the idempotent setup (creates `claire_test` if missing, enables
`vector` in both databases, never drops anything):

```bash
deploy/pgvector/setup.sh
```

## Stop

```bash
docker stop claire-pg                 # stop only; data volume is kept
# or, with compose:
set -a; . ~/.config/claire/pg.env; set +a
docker compose -f deploy/pgvector/docker-compose.yml stop
```

Remove the container but keep the data volume:

```bash
docker rm -f claire-pg
# or: docker compose -f deploy/pgvector/docker-compose.yml down
# (compose down does NOT delete the named volume unless you pass -v)
```

## Connect (psql)

Exact one-liner to open a psql shell using `CLAIRE_PG_DSN` from `pg.env`:

```bash
set -a; . ~/.config/claire/pg.env; set +a; docker exec -it claire-pg psql "$CLAIRE_PG_DSN"
```

Same thing for the test database:

```bash
set -a; . ~/.config/claire/pg.env; set +a; docker exec -it claire-pg psql "$CLAIRE_PG_TEST_DSN"
```

From Python/any client, just use `$CLAIRE_PG_DSN` / `$CLAIRE_PG_TEST_DSN`
(after sourcing `pg.env`).

## Inspect

```bash
docker ps --filter name=claire-pg                 # status + published port
docker inspect claire-pg --format '{{json .State.Health}}' | jq   # healthcheck detail
docker logs -f claire-pg                          # server logs
docker exec claire-pg psql -U claire -d claire -c '\dx'            # installed extensions
docker exec claire-pg psql -U claire -d claire -c '\l'             # databases
docker volume inspect claire-pgdata17             # data volume
```

## Reset from scratch (destructive!)

This **deletes all data** in the `claire-pgdata17` volume and recreates empty
databases. Only do this deliberately:

```bash
set -a; . ~/.config/claire/pg.env; set +a
docker compose -f deploy/pgvector/docker-compose.yml down -v   # -v removes the named volume
docker compose -f deploy/pgvector/docker-compose.yml up -d
deploy/pgvector/setup.sh
```

If you only want to throw away the container (keeping the data), omit `-v`.

## ⚠️ PostgreSQL major versions cannot share a data volume

A data directory initialized by one PostgreSQL **major** version (e.g. 17) can
only be opened by that same major version. If you ever bump the image to a new
major (`pg18`, …), you **cannot** reuse the `claire-pgdata17` volume — the new
server will refuse to start. A major-version upgrade requires a dump/restore
(or `pg_upgrade`):

```bash
docker exec claire-pg pg_dumpall -U claire --clean --if-exists > backup.sql
# ...switch image/volume, start the new major, then:
docker exec -i claire-new psql -U claire -d claire < backup.sql
```

Minor upgrades (17.x -> 17.y) are fine in place; only major bumps need this.
