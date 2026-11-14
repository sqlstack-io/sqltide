import { startChdbEngine } from '../_helpers/chdb-engine';
import { installDemoFetch as install } from '../_api/demo-fetch';
import { getDemoInstance } from '../_helpers/demo-instance.svelte';
import { engineSupport } from '../_helpers/engine-support';
import { createStoryResource } from '../_resources/story.resource';
import type { DemoController, DemoEngine, DemoStory, EngineSupport, LiveDemo, PreparedDemo } from '../types';

/**
 * Answers the UI's API for the UI demo build: from a real engine in this tab where the browser can
 * run it, otherwise from the recorded responses, with a note that says why.
 */
export function installDemoFetch(): void {
	const base: string | undefined = import.meta.env.VITE_SQLTIDE_DEMO_ENGINE;
	const controller: DemoController = getDemoInstance();
	const support: EngineSupport = base === undefined ? { real: false, reason: '' } : engineSupport();
	if (!support.real || base === undefined) {
		controller.useRecording(support.real ? '' : support.reason);
		install();
		return;
	}
	controller.startEngine();
	const started: Promise<LiveDemo> = (async () => {
		const { default: prepared } = (await import('../prepared.json')) as unknown as { default: PreparedDemo };
		const engine: DemoEngine = await startChdbEngine(base, prepared.database, controller.showProgress);
		const story: DemoStory = createStoryResource(prepared, engine.run);
		await story.replaySetup((step: string, done: number, total: number) =>
			controller.showProgress({ phase: 'replay', step, done, total })
		);
		await engine.startOrders();
		return { engine, story };
	})();
	started.then(controller.engineReady, (error: unknown) => {
		console.error('The demo engine could not start:', error);
		controller.engineFailed(error instanceof Error ? error.message : String(error));
	});
	const settled: Promise<LiveDemo | null> = started.catch(async () => {
		await controller.recordingChosen;
		return null;
	});
	install({ active: () => controller.demo.mode.kind !== 'recorded', whenReady: () => settled });
}
