# SQLTide playground: new version, same query

This small orders project demonstrates **plan → build candidate → inspect → promote**.
Version A includes every order. Version B includes only completed orders. Consumers
keep querying `tbl__order_report`; promotion changes which version that name reads.

## What you need

- An installed SQLTide (`sqt`) and its supported Python version (3.12 or later).
- **Offline:** `sqt playground` and `sqt compile` need no warehouse or Docker.
- **Real execution:** a running ClickHouse server. The setup below uses Docker
  Engine/Desktop with Docker Compose v2 and downloads ClickHouse 25.8 on first use.
  Docker is not installed or started by `sqt playground`.
- No Kafka, external dataset, warehouse credentials, or additional Python packages.
  The source is a seeded ClickHouse `stream_table`, not a Kafka ingestion demo.

Use a fresh generated directory and run the steps once, in order. The Compose
project/volume is isolated by the directory's name; use a unique directory name for
each playground. The local-only credentials are public demo values, not secrets.
Do not point this project at an existing warehouse or reuse these credentials elsewhere.

**Run every command below from the generated project directory, in one POSIX shell**
(for example, Bash or Zsh). Keep that shell open throughout the walkthrough:
`sqt_live`, `ch`, `A`, and `B` are shell-local definitions, not installed commands
or persistent settings. These command blocks are not PowerShell or Command Prompt syntax.

## 1. Read and compile offline

```sh
sqt compile
```

Read `pipelines/pl__orders/order_report.sql`, `sources/orders.yml`, and the generated
`target/` artifacts. Compilation resolves SQL and DDL; it **does not** create tables,
seed data, run queries, or prove a deployment is ready. Even `sqt plan` needs ClickHouse
because it compares your project with live warehouse state.

## 2. Start the disposable warehouse

```sh
docker compose up -d --wait
```

Port 18124 must be available on your machine. If it is occupied, stop here or change
both `compose.yaml` and the connection below to another unused port. Do not stop an
unrelated service. Initialization runs `seed.sql` only on a fresh data volume.

These shell functions keep the following commands short. Explicit connection flags
prevent inherited `SQLTIDE_CLICKHOUSE_*` variables from redirecting writes elsewhere.
Run them in the same POSIX shell as the rest of this walkthrough:

```sh
sqt_live() {
  sqt "$@" --project-dir . --target local --host 127.0.0.1 --port 18124 \
    --username playground --password sqltide_playground --database sqltide_playground
}
ch() {
  docker compose exec -T clickhouse clickhouse-client \
    --user playground --password sqltide_playground "$@"
}
A=20260101T100000Z_a
B=20260101T110000Z_b
ch --query 'SELECT count() FROM sqltide_playground.playground_orders'
```

**Leave both fixed identifiers `A` and `B` exactly as shown.** `candidate.sql`
contains B's exact physical table name; changing the identifiers breaks the
walkthrough's query/deployment pairing. They use SQLTide's required
timestamp-and-suffix format and identify the builds, not the event-time range to replay.

Expected: **3** source rows. `seed.sql` is synthetic, append-only input with canonical
replay partition/offset columns. This avoids mapped replay-column audit limitations.

## 3. Establish version A

```sh
sqt_live plan --select order_report --deployment-id "$A"
sqt_live build --select order_report --deployment-id "$A"
sqt_live deployment show "$A"
sqt_live deployment promote "$A"
ch --multiquery < consumer.sql
```

Review the build confirmation before approving it. Building creates a candidate;
the first promotion establishes the stable consumer view. Before that promotion,
`consumer.sql` cannot run because the stable view does not exist yet.
Promotion writes immediately; it has no confirmation prompt.

Expected consumer rows: **1, 2, 3**, including pending order 2.

## 4. Plan and build version B without changing the consumer

The replacement SQL has the same columns plus `WHERE status = 'completed'`:

```sh
cp revisions/completed_orders.sql pipelines/pl__orders/order_report.sql
sqt_live plan --select order_report --full-refresh --deployment-id "$B"
ch --multiquery < consumer.sql
sqt_live build --select order_report --full-refresh --deployment-id "$B"
sqt_live deployment show "$B"
sqt_live deployment diff "$B"
ch --multiquery < consumer.sql
```

Plan makes no warehouse modifications. Build creates isolated physical relations
and replays the input into them; it does not publish B. Both consumer queries must
still return **1, 2, 3**. `--full-refresh` is intentional: the published 0.60.0
filter-only virtual plan can fail without it. Do not remove it from this example.

## 5. Inspect the candidate and watch live input

`deployment show "$B"` lists its physical relations. Because we chose the
deployment ID `20260101T110000Z_b`, its table is `tbl__order_report__20260101T110000Z_b`.
`candidate.sql` queries that table directly, rather than the stable consumer view:

```sh
ch --multiquery < candidate.sql
```

Expected candidate rows: **1, 3**. This is a real query of B, not a readiness claim
inferred from compilation or a fabricated audit result.

Insert the next two input rows **once**:

```sh
ch --multiquery < live.sql
ch --multiquery < consumer.sql
ch --multiquery < candidate.sql
```

The unchanged consumer now returns **1, 2, 3, 4, 5**.
B returns **1, 3, 4**, excluding both pending orders. Both versions receive new
inserts while B remains unpublished. Repeating `live.sql` duplicates data; it is
not an upsert or an exactly-once ingestion demonstration.

## 6. Promote: new version, same query

```sh
sqt_live deployment promote "$B"
ch --multiquery < consumer.sql
ch --query 'SELECT count() FROM sqltide_playground.playground_orders'
ch --query "SELECT order_id FROM sqltide_playground.tbl__order_report__${A} ORDER BY order_id"
```

The **same `consumer.sql`** now returns **1, 3, 4**. The source still has **5** rows.
A's physical table remains available for inspection and still contains all five
orders. Promotion switches stable bindings; it does not rewrite the source or delete A.
Publication is atomic per
relation, **not across an entire graph**. This one-model example makes no
zero-downtime, graph-atomic, automatic-rollback, or production-safety guarantee.

This walkthrough uses show/diff and direct candidate queries, not
`deployment audit`. Published 0.60.0 audit can fail with mapped replay source columns;
this example uses canonical columns but does not claim that all audit paths work.

## Stop or start over

```sh
# Stop this playground, retaining its data for inspection:
docker compose down
```

**The supported restart path is a fresh, uniquely named generated directory.**
Stop the old playground with `docker compose down` first to free its port, then
generate a new playground, enter its directory, and follow the steps from the start.
Do not repeat the walkthrough in place: its model has changed, its deployment IDs
already exist, and its data may still be present.

If you deliberately want to delete **this Compose project's demo data**, run
`docker compose down --volumes` from this directory. That deletion is irreversible
and does not restore the original model; it is cleanup, not the walkthrough's
restart procedure. Never reset Kafka offsets or delete another project's volumes.
