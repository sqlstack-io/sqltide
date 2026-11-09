import { describe, expect, it } from 'vitest';

import type { Project } from '$lib/domain/types';

import { deadLetterSummary } from '$lib/domain/_helpers/derive';

function project(deadLetters: unknown): Project {
	return { warehouseHealth: deadLetters === undefined ? undefined : { deadLetters } } as Project;
}

const model = {
	modelName: 'orders',
	rowCount: 3,
	latestFailedColumns: ['order_id'],
	latestPayload: '{}',
	latestQuarantinedAt: '2026-11-09 10:00:00.000'
};

describe('deadLetterSummary', () => {
	it('given a project without quarantine models when summarizing then it shows nothing', () => {
		expect(deadLetterSummary(project([])).state).toBe('none');
		expect(deadLetterSummary(project(undefined)).state).toBe('none');
	});

	it('given a failed read when summarizing then it reports unavailable instead of zero', () => {
		expect(deadLetterSummary(project(null))).toMatchObject({ state: 'unavailable', total: 0 });
	});

	it('given dead letters when summarizing then it totals rows across models', () => {
		expect(deadLetterSummary(project([model, { ...model, modelName: 'items', rowCount: 2 }]))).toMatchObject({
			state: 'present',
			total: 5
		});
	});
});
