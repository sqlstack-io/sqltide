/** Conversions that reproduce how the Python dev server turns warehouse values into payloads. */

/** `int(str(value))`: 64-bit integers arrive from `JSONEachRow` as strings. */
export function toInt(value: unknown): number {
	const parsed: number = Math.trunc(Number(value));
	if (value === null || value === undefined || Number.isNaN(parsed)) {
		throw new Error(`expected an integer, got ${String(value)}`);
	}
	return parsed;
}

/** `str(value)` for the scalar values a row or event payload holds. */
export function pyStr(value: unknown): string {
	if (value === null || value === undefined) return 'None';
	if (value === true) return 'True';
	if (value === false) return 'False';
	if (typeof value === 'object') return JSON.stringify(value);
	return String(value);
}

/** `mapping.get(key)`: the stored value, or `null` when the key is absent. */
export function get(mapping: { [key: string]: unknown }, key: string): unknown {
	return Object.hasOwn(mapping, key) ? mapping[key] : null;
}

/** `mapping.get(key, fallback)`: only an absent key takes the fallback. */
export function getOr(mapping: { [key: string]: unknown }, key: string, fallback: unknown): unknown {
	return Object.hasOwn(mapping, key) ? mapping[key] : fallback;
}

/**
 * `str(datetime)` of a `DateTime64` value the Python driver decoded: microseconds when there is a
 * fraction, none at all when the fraction is zero.
 */
export function pythonDatetimeText(value: string): string {
	const [whole, fraction = ''] = value.split('.');
	const micros: string = fraction.padEnd(6, '0').slice(0, 6);
	return /^0*$/.test(micros) ? whole : `${whole}.${micros}`;
}

/** One ClickHouse string literal, as `quote_clickhouse_sql_string` writes it. */
export function quoteString(value: string): string {
	return `'${value.replaceAll('\\', '\\\\').replaceAll("'", "''")}'`;
}

/** The body of a single-quoted literal, as the run readers escape it. */
export function sqlLiteral(value: string): string {
	return value.replaceAll('\\', '\\\\').replaceAll("'", "\\'");
}

/** Microseconds since the epoch of a naive warehouse timestamp read as UTC; `null` if unparseable. */
export function timestampMicros(value: string): number | null {
	const match: RegExpExecArray | null =
		/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$/.exec(value.replace(/Z$/, ''));
	if (match === null) return null;
	const [, year, month, day, hour, minute, second = '0', fraction = ''] = match;
	const millis: number = Date.UTC(
		Number(year),
		Number(month) - 1,
		Number(day),
		Number(hour),
		Number(minute),
		Number(second)
	);
	return millis * 1000 + Number(fraction.padEnd(6, '0'));
}

/** `round(value, 3)`. */
export function round3(value: number): number {
	return Math.round(value * 1000) / 1000;
}

/** Python's code-point string ordering, which `<` matches for the ASCII names here. */
export function compareText(left: string, right: string): number {
	return left < right ? -1 : left > right ? 1 : 0;
}

export function sortedUnique(values: Iterable<string>): string[] {
	return [...new Set(values)].sort(compareText);
}

/** Parse one JSON object payload; anything else is `{}`, as the run readers treat it. */
export function parsedObject(text: unknown): { [key: string]: unknown } {
	try {
		const parsed: unknown = JSON.parse(pyStr(text));
		return parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)
			? (parsed as { [key: string]: unknown })
			: {};
	} catch {
		return {};
	}
}
