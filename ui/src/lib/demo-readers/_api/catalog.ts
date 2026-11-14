/** The catalog snapshot and managed-table inspection of one database, from system tables. */
import { VIEW_ENGINE } from '../constants.ts';
import type { Query } from '../types.ts';
import { directBindingName, sourceRelationNames, targetRelationName } from '../_helpers/catalog-sql.ts';
import type { Catalog, CatalogColumn, CatalogRelation, ManagedTableState } from '../_helpers/evidence-types.ts';
import { candidateLogicalName } from '../_helpers/naming.ts';
import { quoteString } from '../_helpers/values.ts';

type TableRow = { name: string; engine: string; create_table_query: string; as_select: string };
type ColumnRow = { table: string; name: string; type: string; default_expression: string | null };

/** One catalog snapshot of `database`, relations ordered by name. */
export async function loadCatalog(query: Query, database: string): Promise<Catalog> {
	const quoted: string = quoteString(database);
	const tables: TableRow[] = await query<TableRow>(
		`SELECT name, engine, create_table_query, as_select FROM system.tables WHERE database = ${quoted} ORDER BY name`
	);
	const columnRows: ColumnRow[] = await query<ColumnRow>(
		`SELECT table, name, type, default_expression FROM system.columns WHERE database = ${quoted} ORDER BY table, position`
	);
	const columns: Map<string, CatalogColumn[]> = new Map();
	for (const row of columnRows) {
		const list: CatalogColumn[] = columns.get(row.table) ?? [];
		list.push({
			name: row.name,
			type: row.type,
			defaultExpression: row.default_expression === null || row.default_expression === '' ? null : row.default_expression
		});
		columns.set(row.table, list);
	}
	const relations: CatalogRelation[] = tables.map((row) => ({
		name: row.name,
		engine: row.engine,
		definitionSql: row.create_table_query,
		columns: columns.get(row.name) ?? [],
		sourceRelationNames: row.as_select.trim() === '' ? [] : sourceRelationNames(row.as_select),
		targetRelationName: targetRelationName(row.create_table_query),
		stableBindingName: directBindingName(row.engine, row.as_select)
	}));
	return { database, relations };
}

/** Stable logical views and deployment-suffixed model candidates of `database`. */
export async function inspectManagedTableState(query: Query, database: string): Promise<ManagedTableState> {
	const quoted: string = quoteString(database);
	const views: { name: string; as_select: string }[] = await query(
		`SELECT name, as_select FROM system.tables WHERE database = ${quoted} AND engine = 'View' ORDER BY name`
	);
	const tables: { name: string }[] = await query(
		`SELECT name FROM system.tables WHERE database = ${quoted} AND engine != 'View' ORDER BY name`
	);
	const state: ManagedTableState = { activeBindings: [], physicalCandidates: [] };
	for (const view of views) {
		const logical: string | null = candidateLogicalName(view.name);
		if (logical !== null) {
			state.physicalCandidates.push({ database, logicalName: logical, physicalName: view.name, objectType: 'view' });
			continue;
		}
		const physical: string | null = directBindingName(VIEW_ENGINE, view.as_select);
		if (physical !== null) state.activeBindings.push({ database, logicalName: view.name, physicalName: physical });
	}
	for (const table of tables) {
		const logical: string | null = candidateLogicalName(table.name);
		if (logical !== null) {
			state.physicalCandidates.push({ database, logicalName: logical, physicalName: table.name, objectType: 'table' });
		}
	}
	return state;
}
