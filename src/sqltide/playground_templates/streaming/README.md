# SQLTide streaming playground: new version, same stream

This orders project shows **plan → build candidate → inspect → promote** on live Kafka data.
A producer writes order lifecycle events to a three-partition Redpanda topic. ClickHouse lands
them through its Kafka engine, and four SQL models turn them into daily commerce KPIs.

Version A is published first. Version B adds a `net_revenue` column. SQLTide builds B beside A,
replays B from the history ClickHouse already retains, and keeps both versions receiving new
events. Consumers keep querying `view__commerce_kpis`; promotion changes which version that name
reads.

```text
commerce_event_stream (Kafka source, 7-day retained landing table)
  -> commerce_events    typed envelope
  -> order_events       validated lifecycle with region labels
  -> order_event_facts  append-only additive facts       (B adds refunded revenue)
  -> commerce_kpis      view that deduplicates replay    (B adds net_revenue)
```

## What you need

- An installed SQLTide (`sqt`) and its supported Python version (3.12 or later).
- **Offline:** `sqt playground` and `sqt compile` need no warehouse or Docker.
- **Everything else runs locally with Docker:** Docker Engine/Desktop with Docker Compose v2.
  The first start downloads Redpanda, ClickHouse 25.8 and a Python base image for the producer.
  Docker is not installed or started by `sqt playground`.
- Free loopback ports **18125** (ClickHouse HTTP) and **19092** (Kafka). Every service binds to
  `127.0.0.1`; stop any other streaming playground first.
- The host name `redpanda.localhost` must resolve to `127.0.0.1`. Most Linux and macOS systems
  resolve `*.localhost` to loopback already; check with `ping -c 1 redpanda.localhost`. If it does
  not resolve, add `127.0.0.1 redpanda.localhost` to `/etc/hosts`. ClickHouse, inside Compose,
  and `sqt`, on your machine, both use that one broker address.

The local-only credentials are public demo values, not secrets. Do not point this project at an
existing warehouse or reuse these credentials elsewhere.

**Run every command below from the generated project directory, in one POSIX shell** (for
example, Bash or Zsh). `sqt_live`, `ch`, `A`, and `B` are shell-local definitions, not installed
commands or persistent settings.

## 1. Read and compile offline

```sh
sqt compile
```

Read `sources/order_events.yml`, the models in `pipelines/pl__order_events/`, the SQL tests in
`tests/`, the audits in `audits/`, and the generated `target/` artifacts. Compilation resolves SQL
and DDL; it does not create tables or read Kafka.

## 2. Start Redpanda, ClickHouse and the producer

```sh
docker compose up -d --build --wait
```

This creates the topic `source.order_events.live` with three partitions and seven-day retention,
then starts the producer: two new orders every three seconds, each moving through `created`,
`paid`, `shipped`, and `delivered`, or ending as `cancelled` or `refunded`.

These shell functions keep the following commands short. Explicit connection flags prevent
inherited `SQLTIDE_CLICKHOUSE_*` variables from redirecting writes elsewhere:

```sh
sqt_live() {
  sqt "$@" --project-dir . --target local --host 127.0.0.1 --port 18125 \
    --username playground --password sqltide_playground --database sqltide_streaming
}
ch() {
  docker compose exec -T clickhouse clickhouse-client \
    --user playground --password sqltide_playground "$@"
}
A=20260101T100000Z_a
B=20260101T110000Z_b
docker compose exec -T redpanda rpk topic describe source.order_events.live --print-partitions
```

**Leave both fixed identifiers `A` and `B` exactly as shown.** `candidate.sql` contains B's exact
physical view name.

The topic lists three partitions whose high watermarks grow while the producer runs.

## 3. Establish version A

```sh
sqt_live plan --deployment-id "$A"
sqt_live build --deployment-id "$A"
sqt_live deployment promote "$A"
sqt_live test
ch --multiquery < consumer.sql
```

Review the build confirmation before approving it. The build creates the Kafka landing tables,
starts consuming from the beginning of the topic, and builds A's four models. On a fresh
playground it can print `Could not reset committed offsets ... NOT_COORDINATOR_FOR_CONSUMER`:
the consumer group does not exist yet, so there is nothing to reset. The first promotion creates the
stable names consumers read, such as `view__commerce_kpis`. Promotion writes immediately; it has
no confirmation prompt. `sqt test` runs the four SQL tests against ClickHouse.

Run `ch --multiquery < consumer.sql` a few times. `orders` and `gross_revenue` grow with every
batch the producer sends.

## 4. Plan and build version B without changing the consumer

```sh
cp revisions/order_event_facts.sql revisions/commerce_kpis.sql pipelines/pl__order_events/
sqt_live plan --deployment-id "$B"
sqt_live build --deployment-id "$B"
sqt_live deployment show "$B"
sqt_live deployment diff "$B"
ch --multiquery < consumer.sql
```

The plan replays one subtree from the retained landing table `raw__commerce_event_stream`;
`sqt_live plan --verbose --deployment-id "$B"` shows the new columns. Plan makes no warehouse
changes. Build creates B's own copy of all four models and replays every retained event into
them; the Kafka landing tables are shared and keep consuming. It does not publish B: the consumer
still reads A and has no `net_revenue` column. `deployment diff` reports `order_event_facts` and
`commerce_kpis` as changed, with the same row counts as A.

## 5. Inspect the candidate while events keep arriving

```sh
ch --multiquery < candidate.sql
ch --multiquery < consumer.sql
```

`candidate.sql` queries B's view directly. Its `orders` and `gross_revenue` match the consumer's
for the same events, including events that arrived before B existed, and it adds `net_revenue`:
gross revenue minus refunds. Run both queries again a few seconds later: A and B both keep
receiving the live stream while B remains unpublished. The two can differ by the events that
landed between the two queries.

## 6. Promote: new version, same name

```sh
sqt_live deployment promote "$B"
ch --multiquery < consumer.sql
ch --query 'SELECT round(sum(net_revenue), 2) FROM sqltide_streaming.view__commerce_kpis'
```

The unchanged `consumer.sql` now reads B, and the stable view now has `net_revenue`. A's physical
relations remain as a rollback point and keep receiving events; `sqt_live janitor` lists them as
retained, and `sqt_live deployment rollback --previous` would bind the stable names to A again. Promotion switches stable bindings one relation at a time; it is
atomic per relation, **not across the graph**. This example makes no zero-downtime,
graph-atomic, automatic-rollback, or production-safety guarantee.

## 7. Optional: watch a warning recover in the local UI

```sh
sqt_live dev
```

Open `http://127.0.0.1:8000` to see the lineage, Kafka lag per partition, retained messages, the
deployments, and audit results. The `orders_no_future_events` audit runs every ten seconds, and
`sensors/quality.py` prints its transitions in this terminal through the local-only
`ConsoleNotifier` in `providers/console.py`; no webhook is configured or sent.

From a second terminal in this directory, inject one order 30 seconds in the future:

```sh
docker compose run --rm --no-deps producer python -u /app/producer/fake_orders_producer.py inject-future
```

The `sqt dev` terminal prints a `WARNING` with one sample row, then `RECOVERED` once the clock
passes the event's timestamp. Stop `sqt dev` with Ctrl-C.

## Stop or start over

```sh
# Stop this playground, retaining its data for inspection:
docker compose down
```

**The supported restart path is a fresh, uniquely named generated directory.** Stop the old
playground with `docker compose down` first to free its ports, then generate a new playground
under a new name, for example `sqt playground --template streaming sqltide-streaming-playground-2`,
enter its directory, and follow the steps from the start. The Compose project and its data volumes
are named after the directory, so a new name starts with empty data.
Do not repeat the walkthrough in place: its models have changed and its deployment IDs already
exist.

To delete **this Compose project's demo data**, run `docker compose down --volumes` from this
directory. That deletion is irreversible; it is cleanup, not the walkthrough's restart procedure.
Never reset Kafka offsets or delete another project's volumes.
