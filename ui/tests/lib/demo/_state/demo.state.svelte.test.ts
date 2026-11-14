import { describe, expect, it } from 'vitest';

import { createDemoState } from '$lib/demo/_state/demo.state.svelte';
import type { DemoController, DemoEngine } from '$lib/demo/types';

const engine: DemoEngine = {} as DemoEngine;

describe('demo state', () => {
	it('given a loading engine when it reports progress and becomes ready then the mode follows it', () => {
		const controller: DemoController = createDemoState();

		controller.startEngine();
		controller.showProgress({ phase: 'start' });
		const loading: unknown = structuredClone($state.snapshot(controller.demo.mode));
		controller.engineReady(engine);

		expect(loading).toEqual({ kind: 'real', progress: { phase: 'start' }, engine: null });
		expect(controller.demo.mode).toEqual({ kind: 'real', progress: null, engine });
	});

	it('given the visitor chose the recording when the engine reports later then the recording stays', () => {
		const controller: DemoController = createDemoState();
		controller.startEngine();

		controller.useRecording('The real engine couldn’t start here, so this is the recording.');
		controller.showProgress({ phase: 'start' });
		controller.engineReady(engine);

		expect(controller.demo.mode).toEqual({
			kind: 'recorded',
			reason: 'The real engine couldn’t start here, so this is the recording.'
		});
	});

	it('given a failed engine when promoted then the error and the promotion are both kept', () => {
		const controller: DemoController = createDemoState();
		controller.startEngine();

		controller.engineFailed('out of memory');
		controller.markPromoted();

		expect(controller.demo).toEqual({ mode: { kind: 'failed', error: 'out of memory' }, promoted: true });
	});
});
