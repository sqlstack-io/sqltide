import { describe, expect, it } from 'vitest';

import { createDemoState } from '$lib/demo/_state/demo.state.svelte';
import type { DemoController, LiveDemo } from '$lib/demo/types';

const live: LiveDemo = {} as LiveDemo;

describe('demo state', () => {
	it('given a loading engine when it reports progress and becomes ready then the mode follows it', () => {
		const controller: DemoController = createDemoState();

		controller.startEngine();
		controller.showProgress({ phase: 'start' });
		const loading: unknown = structuredClone($state.snapshot(controller.demo.mode));
		controller.engineReady(live);

		expect(loading).toEqual({ kind: 'real', progress: { phase: 'start' }, live: null });
		expect(controller.demo.mode).toEqual({ kind: 'real', progress: null, live });
	});

	it('given the visitor chose the recording when the engine reports later then the recording stays', () => {
		const controller: DemoController = createDemoState();
		controller.startEngine();

		controller.useRecording('The real engine couldn’t start here, so this is the recording.');
		controller.showProgress({ phase: 'start' });
		controller.engineReady(live);

		expect(controller.demo.mode).toEqual({
			kind: 'recorded',
			reason: 'The real engine couldn’t start here, so this is the recording.'
		});
	});

	it('given a failed engine when the visitor chooses the recording then the requests waiting on it can go on', async () => {
		const controller: DemoController = createDemoState();
		controller.startEngine();
		controller.engineFailed('out of memory');
		let settled: boolean = false;
		void controller.recordingChosen.then(() => (settled = true));
		await Promise.resolve();
		const before: boolean = settled;

		controller.useRecording('The real engine couldn’t start here, so this is the recording.');
		await controller.recordingChosen;

		expect([before, settled]).toEqual([false, true]);
	});
});
