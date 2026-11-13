# SQLTide playground: new version, same query

This small orders project shows how SQLTide changes a model without changing what consumers read
until you promote the new version. `sqt playground` started ClickHouse in Docker on your machine
and loaded three orders. Run every command from this directory.

The project reads one table, `playground_orders`, and builds one model,
`pipelines/pl__orders/order_report.sql`. Consumers query its stable name, `tbl__order_report`.

## 1. Publish the first version

```sh
sqt build
sqt deployment promote --latest
sqt playground query consumer.sql
```

`sqt build` shows its plan and asks you to confirm. It then builds the model as a new deployment
that consumers cannot see yet. Promoting it points `tbl__order_report` at it. The query returns
orders 1, 2 and 3.

## 2. Change the model

```sh
cp revisions/completed_orders.sql pipelines/pl__orders/order_report.sql
sqt plan
```

The new version keeps only completed orders. `sqt plan` shows what a build would change and writes
nothing: it rebuilds `order_report` from the source table.

## 3. Build the new version beside the published one

```sh
sqt build
sqt playground query consumer.sql --compare
```

`--compare` runs the same query against what consumers read and against the new, unpublished
version. Consumers still get orders 1, 2 and 3; the new version returns orders 1 and 3.

## 4. Add orders while both versions run

```sh
sqt playground query live.sql
sqt playground query consumer.sql --compare
```

Both versions receive the two new orders. Consumers get orders 1 to 5; the new version gets 1, 3
and 4.

## 5. Promote

```sh
sqt deployment promote --latest
sqt playground query consumer.sql
```

The same query now returns orders 1, 3 and 4. The first version stays in ClickHouse, so
`sqt deployment rollback --previous` can switch back to it. Promotion switches each stable name
on its own, not a whole graph at once.

## 6. Stop

```sh
sqt playground down
```

This stops ClickHouse and keeps its data; `sqt playground up` starts it again. To delete the data,
run `sqt playground down --volumes`. To start over, create a new playground in a new directory.

The credentials in `sqltide_project.toml` are public values for this disposable container only.
The project ignores `SQLTIDE_CLICKHOUSE_*` variables, so it never connects anywhere else.
