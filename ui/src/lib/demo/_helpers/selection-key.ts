/** What the Plan page asks sqt for, from a plan request's query or a build request's body. */
export type PlanSelection = {
	selectors: string[];
	changed: boolean;
	includeMissingUpstream: boolean;
	startTime: string | null;
};

/** A plan request's selection, as `GET /api/plan` reads its query. */
export function planSelection(params: URLSearchParams): PlanSelection {
	return {
		selectors: params.getAll('select'),
		changed: params.get('changed') === 'true',
		includeMissingUpstream: params.get('include_missing_upstream') === 'true',
		startTime: params.get('start')
	};
}

/**
 * How prepared.json keys a selection (scripts/prepare-demo.mjs builds the same key): the selectors
 * in any order, or changed models only; whether missing upstream is included; and whether it
 * replays from a time, which the plan fills in.
 */
export function selectionKey(selection: PlanSelection): string {
	return [
		selection.changed ? 'changed' : [...selection.selectors].sort().join(' '),
		selection.includeMissingUpstream ? 'upstream' : '',
		selection.startTime === null ? '' : 'from'
	].join('|');
}
