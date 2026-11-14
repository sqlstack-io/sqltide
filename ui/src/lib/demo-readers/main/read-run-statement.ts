import { tableExists } from '../_api/warehouse-probes.ts';
import { pyStr, sqlLiteral } from '../_helpers/values.ts';
import type { Query, RunStatementPayload } from '../types.ts';

const RUN_STATEMENTS_TABLE: string = '_sqltide_run_statements';

type StatementRow = {
	step_id: string;
	phase: string;
	intent: string;
	sql: string;
	sql_sha256: string;
	workflow_sha256: string;
};

/** `GET /api/runs/{id}/statements/{seq}`: one recorded SQL statement of a run. */
export async function readRunStatement(
	query: Query,
	database: string,
	invocationId: string,
	statementSequence: number
): Promise<RunStatementPayload> {
	if (!(await tableExists(query, database, RUN_STATEMENTS_TABLE))) return { found: false };
	const [row]: StatementRow[] = await query<StatementRow>(
		'SELECT step_id, phase, intent, sql, toString(sql_sha256) AS sql_sha256, ' +
			`toString(workflow_sha256) AS workflow_sha256 FROM \`${database}\`.\`${RUN_STATEMENTS_TABLE}\` FINAL ` +
			`WHERE invocation_id = '${sqlLiteral(invocationId)}' AND statement_sequence = ${Math.trunc(statementSequence)} LIMIT 1`
	);
	if (row === undefined) return { found: false };
	return {
		found: true,
		invocationId,
		statementSequence,
		stepId: pyStr(row.step_id),
		phase: pyStr(row.phase),
		intent: pyStr(row.intent),
		sql: pyStr(row.sql),
		sqlSha256: pyStr(row.sql_sha256),
		workflowSha256: pyStr(row.workflow_sha256)
	};
}
