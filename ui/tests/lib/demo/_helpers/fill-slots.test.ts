import { describe, expect, it } from 'vitest';

import { deploymentTime, fillSlots, startRole } from '$lib/demo/_helpers/fill-slots';
import type { SlotValues } from '$lib/demo/types';

const emptyValues = (): SlotValues => ({ deployments: {}, uuids: {}, roleStarts: {}, times: {}, version: '1.2.3', startTime: null });
const START: number = Date.UTC(2026, 10, 14, 8, 23, 10, 359);

describe('fill slots', () => {
	it('given a role that created a deployment when started then its ID names the second sqt named it', () => {
		const values: SlotValues = emptyValues();

		startRole(values, 'build', START, { creates: [{ name: 'built', offset: 1700 }], times: [] });

		const id: string = fillSlots('{{d:built}}', values);
		expect(id).toMatch(/^20261114T082312Z_[0-9a-f]{6}$/);
		expect(fillSlots('{{dt:built:s3}} {{dt:built:T0}}', values)).toBe('2026-11-14 08:23:12.000 2026-11-14T08:23:12');
	});

	it('given deployments created within one second when started then their IDs still sort in the order they were created', () => {
		const values: SlotValues = emptyValues();

		startRole(values, 'publish1', START, { creates: [{ name: 'v1', offset: 100 }], times: [] });
		startRole(values, 'publish2', START + 300, { creates: [{ name: 'v2', offset: 100 }], times: [] });

		const [v1, v2] = [fillSlots('{{d:v1}}', values), fillSlots('{{d:v2}}', values)];
		expect(v2.slice(0, 16)).toBe('20261114T082311Z');
		expect([v1, v2].sort()).toEqual([v1, v2]);
	});

	it('given a deployment the page already named when the role starts then it keeps that ID and its time', () => {
		const values: SlotValues = emptyValues();

		startRole(values, 'build', START, { creates: [{ name: 'built', offset: 0 }], times: [] }, {
			built: '20261114T070000Z_abcdef'
		});

		expect(fillSlots('{{d:built}} at {{dt:built:s0}}', values)).toBe('20261114T070000Z_abcdef at 2026-11-14 07:00:00');
	});

	it('given times of this role and an earlier one when filled then each falls its offset after its role started', () => {
		const values: SlotValues = emptyValues();
		startRole(values, 'publish', START - 60_000, { creates: [], times: [['publish', 0, 5]] });

		startRole(values, 'build', START, { creates: [], times: [['build', 0, 250], ['build', 1, 1250], ['publish', 1, 900]] });

		expect(fillSlots('{{t:build:1:s3}}|{{t:build:0:s6}}|{{t:publish:1:s3}}|{{t:publish:0:T0}}', values)).toBe(
			'2026-11-14 08:23:11.609|2026-11-14 08:23:10.609000|2026-11-14 08:22:11.259|2026-11-14T08:22:10'
		);
	});

	it('given a role started again when filled then it gets new UUIDs but keeps the same one within a run', () => {
		const values: SlotValues = emptyValues();
		startRole(values, 'build', START, { creates: [], times: [] });
		const first: string = fillSlots('{{u:build.0}} {{u:build.0}}', values);

		startRole(values, 'build', START + 1000, { creates: [], times: [] });
		const second: string = fillSlots('{{u:build.0}}', values);

		const [a, b] = first.split(' ');
		expect(a).toBe(b);
		expect(second).not.toBe(a);
	});

	it('given a start time when filled then {{s}} is that time in the requested format', () => {
		const values: SlotValues = { ...emptyValues(), startTime: Date.parse('2026-01-01T10:01:00Z') };

		expect(fillSlots("WHERE ts >= '{{s:s3}}' --start-time {{s:T0}}Z", values)).toBe(
			"WHERE ts >= '2026-01-01 10:01:00.000' --start-time 2026-01-01T10:01:00Z"
		);
	});

	it('given the version slot when filled then it is the sqt version that prepared the demo', () => {
		expect(fillSlots("tool_version = '{{v}}'", emptyValues())).toBe("tool_version = '1.2.3'");
	});

	it('given a slot nothing bound when filled then it fails instead of writing a wrong value', () => {
		expect(() => fillSlots('{{d:missing}}', emptyValues())).toThrow('{{d:missing}}');
		expect(() => fillSlots('{{t:build:0:s3}}', emptyValues())).toThrow('{{t:build:0:s3}}');
		expect(() => startRole(emptyValues(), 'promote', START, { creates: [], times: [['build', 0, 1]] })).toThrow('build');
	});

	it('given IDs sqt writes and others when parsed then only deployment IDs have a time', () => {
		expect(deploymentTime('20261114T082312Z_0a1b2c')).toBe(Date.UTC(2026, 10, 14, 8, 23, 12));
		expect(deploymentTime('latest')).toBeNull();
	});
});
