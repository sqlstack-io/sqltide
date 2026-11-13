# SQLTide streaming playground: new version, same stream

This orders project shows how SQLTide changes models on live Kafka data without changing what
consumers read until you promote the new version. `sqt playground --template streaming` started
Redpanda, ClickHouse and an order producer in Docker on your machine. The producer writes two new
orders every three seconds. Run every command from this directory.

```text
commerce_event_stream (Kafka source, 7-day retained landing table)
  -> commerce_events    typed envelope
  -> order_events       validated lifecycle with region labels
  -> order_event_facts  append-only additive facts       (version B adds refunded revenue)
  -> commerce_kpis      view that deduplicates replay    (version B adds net_revenue)
```

`sqt` reaches the broker as `redpanda.localhost`. Most systems resolve `*.localhost` to your own
machine; if `ping -c 1 redpanda.localhost` fails, add `127.0.0.1 redpanda.localhost` to
`/etc/hosts`.

## 1. Publish the first version

```sh
sqt build
sqt deployment promote --latest
sqt test
sqt playground query consumer.sql
```

`sqt build` shows its plan and asks you to confirm. It then creates the Kafka landing tables,
reads the topic from the beginning and builds the four models. On a fresh playground it can print
`Could not reset committed offsets ... NOT_COORDINATOR_FOR_CONSUMER`; the consumer group does not
exist yet, so there is nothing to reset. Promoting creates the stable names consumers read, such
as `view__commerce_kpis`. `sqt test` runs the project's four SQL tests.

Run the query again after a few seconds: `orders` and `gross_revenue` grow with each batch.

## 2. Change the models

```sh
cp revisions/order_event_facts.sql revisions/commerce_kpis.sql pipelines/pl__order_events/
sqt plan
```

Version B adds a `net_revenue` column. The plan rebuilds `order_event_facts` and `commerce_kpis`
from the retained landing table, so the new version covers events that arrived before it existed.
`sqt plan --verbose` shows the new columns.

## 3. Build the new version beside the published one

```sh
sqt build
sqt deployment diff --latest
sqt playground query consumer.sql --compare
sqt playground query net_revenue.sql --candidate
```

The build replays every retained event into version B, and both versions keep receiving the live
stream. `--compare` shows the consumer query against version A, which consumers still read, and
against version B: their order counts match for the same events. Only version B has
`net_revenue`, so `--candidate` runs that query against B alone.

## 4. Promote

```sh
sqt deployment promote --latest
sqt playground query net_revenue.sql
```

The stable view now reads version B, so the plain query has `net_revenue`. Version A stays in
ClickHouse and keeps receiving events, so `sqt deployment rollback --previous` can switch back to
it. Promotion switches each stable name on its own, not the whole graph at once.

## 5. Optional: watch an audit warning recover in the local UI

```sh
sqt dev
```

Open `http://127.0.0.1:8000` for the lineage, Kafka lag per partition, deployments and audit
results. The `orders_no_future_events` audit runs every ten seconds, and `sensors/quality.py`
prints its changes in this terminal. From a second terminal in this directory, send one order
dated 30 seconds in the future:

```sh
docker compose run --rm --no-deps producer python -u /app/producer/fake_orders_producer.py inject-future
```

`sqt dev` prints a `WARNING`, then `RECOVERED` once the clock passes that order. Stop it with
Ctrl-C.

## 6. Stop

```sh
sqt playground down
```

This stops every service and keeps the data; `sqt playground up` starts them again. To delete the
data, run `sqt playground down --volumes`. To start over, create a new playground in a new
directory.

The credentials in `sqltide_project.toml` are public values for these disposable containers only.
The project ignores `SQLTIDE_CLICKHOUSE_*` variables, so it never connects anywhere else.
