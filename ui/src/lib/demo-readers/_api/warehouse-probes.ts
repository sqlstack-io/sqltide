/** Small warehouse probes the run and state readers share. */
import type { Query } from '../types.ts';
import { pyStr, quoteString, toInt } from '../_helpers/values.ts';

export async function tableExists(query: Query, database: string, table: string): Promise<boolean> {
	const rows: { present: unknown }[] = await query(
		`SELECT count() AS present FROM system.tables WHERE database = ${quoteString(database)} AND name = ${quoteString(table)}`
	);
	return rows.length > 0 && toInt(rows[0].present) > 0;
}

/** The warehouse's current time as an exact UTC `DateTime64(3)` string. */
export async function captureWarehouseTimestamp(query: Query): Promise<string> {
	const rows: { warehouse_timestamp: string }[] = await query(
		"SELECT toString(now64(3, 'UTC'), 'UTC') AS warehouse_timestamp"
	);
	if (rows.length === 0) throw new Error('ClickHouse returned no warehouse timestamp');
	return pyStr(rows[0].warehouse_timestamp);
}
