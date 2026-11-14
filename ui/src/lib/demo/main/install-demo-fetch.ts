import { startChdbEngine } from '../_helpers/chdb-engine';
import { installDemoFetch as install } from '../_api/demo-fetch';
import { getDemoInstance } from '../_helpers/demo-instance.svelte';
import { engineSupport } from '../_helpers/engine-support';
import type { DemoController, DemoEngine, DemoEngineLink, EngineScript, EngineSupport } from '../types';

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
	const started: Promise<DemoEngine> = (async () => {
		const { default: script } = (await import('../engine-script.json')) as { default: EngineScript };
		const engine: DemoEngine = await startChdbEngine(base, script, controller.showProgress);
		await engine.startOrders();
		return engine;
	})();
	started.then(controller.engineReady, (error: unknown) => {
		console.error('The demo engine could not start:', error);
		controller.engineFailed(error instanceof Error ? error.message : String(error));
	});
	const link: DemoEngineLink = {
		active: () => controller.demo.mode.kind !== 'recorded',
		ready: () => (controller.demo.mode.kind === 'real' ? controller.demo.mode.engine : null),
		whenReady: () => started,
		promoted: () => controller.demo.promoted,
		markPromoted: controller.markPromoted
	};
	install(link);
}
