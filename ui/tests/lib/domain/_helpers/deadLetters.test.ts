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
	it('shows nothing when the project has no quarantine models', () => {
		expect(deadLetterSummary(project([])).state).toBe('none');
		expect(deadLetterSummary(project(undefined)).state).toBe('none');
	});

	it('reports unavailable instead of zero when the read failed', () => {
		expect(deadLetterSummary(project(null))).toMatchObject({ state: 'unavailable', total: 0 });
	});

	it('totals rows across models', () => {
		expect(deadLetterSummary(project([model, { ...model, modelName: 'items', rowCount: 2 }]))).toMatchObject({
			state: 'present',
			total: 5
		});
	});
});
