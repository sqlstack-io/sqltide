/**
 * Catalog facts read from ClickHouse's own SQL text. The server parses each relation's SQL with
 * its SQL analyzer; ClickHouse normalizes `create_table_query` and `as_select` and qualifies every
 * relation name, so these patterns recover the same facts from them.
 */
import { VIEW_ENGINE } from '../constants.ts';

const IDENTIFIER: string = '(?:`(?:[^`]|``)+`|[A-Za-z_][A-Za-z0-9_$]*)';
const QUALIFIED: string = `${IDENTIFIER}(?:\\s*\\.\\s*${IDENTIFIER})?`;
const DIRECT_BINDING: RegExp = new RegExp(`^\\s*SELECT\\s+\\*\\s+FROM\\s+(${QUALIFIED})\\s*;?\\s*$`, 'i');
const MATERIALIZED_VIEW_TARGET: RegExp = new RegExp(
	`^\\s*CREATE\\s+MATERIALIZED\\s+VIEW\\s+[\\s\\S]*?\\sTO\\s+(${QUALIFIED})`,
	'i'
);
const READ_RELATION: RegExp = new RegExp(`\\b(?:FROM|JOIN)\\s+(${QUALIFIED})(\\s*\\()?`, 'gi');
const CTE_NAME: RegExp = new RegExp(`(${IDENTIFIER})\\s+AS\\s*\\(`, 'gi');

function unquoted(identifier: string): string {
	return identifier.startsWith('`') ? identifier.slice(1, -1).replaceAll('``', '`') : identifier;
}

/** The unqualified relation name of `db.name` or `name`. */
function relationName(qualified: string): string {
	const parts: string[] = qualified.match(new RegExp(IDENTIFIER, 'g')) ?? [qualified];
	return unquoted(parts[parts.length - 1]);
}

/** The relation a view binds directly, when its query is exactly `SELECT * FROM relation`. */
export function directBindingName(engine: string, asSelect: string): string | null {
	if (engine !== VIEW_ENGINE) return null;
	const match: RegExpExecArray | null = DIRECT_BINDING.exec(asSelect);
	return match === null ? null : relationName(match[1]);
}

/** The `TO` relation of a materialized view. */
export function targetRelationName(createTableQuery: string): string | null {
	const match: RegExpExecArray | null = MATERIALIZED_VIEW_TARGET.exec(createTableQuery);
	return match === null ? null : relationName(match[1]);
}

/** Distinct relation names a query reads, in order; CTE names and table functions excluded. */
export function sourceRelationNames(asSelect: string): string[] {
	const code: string = asSelect.replace(/'(?:[^'\\]|\\.|'')*'/g, "''");
	const ctes: Set<string> = new Set([...code.matchAll(CTE_NAME)].map((match) => unquoted(match[1])));
	const names: string[] = [];
	for (const match of code.matchAll(READ_RELATION)) {
		if (match[2] !== undefined) continue;
		const name: string = relationName(match[1]);
		if (!ctes.has(name) && !names.includes(name)) names.push(name);
	}
	return names;
}
