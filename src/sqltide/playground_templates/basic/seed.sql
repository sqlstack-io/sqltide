CREATE TABLE sqltide_playground.playground_orders
(
    order_id UInt64,
    status String,
    total_cents Int64,
    _replay_partition Int32,
    _replay_offset Int64,
    _replay_timestamp DateTime64(3)
)
ENGINE = MergeTree()
ORDER BY (_replay_partition, _replay_offset);

INSERT INTO sqltide_playground.playground_orders VALUES
    (1, 'completed', 2500, 0, 1, '2026-01-01 10:00:00'),
    (2, 'pending',   1800, 0, 2, '2026-01-01 10:01:00'),
    (3, 'completed', 4200, 0, 3, '2026-01-01 10:02:00');
