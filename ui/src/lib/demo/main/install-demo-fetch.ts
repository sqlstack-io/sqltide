import { installDemoFetch as install, type DemoEngineLink } from '../_api/demo-fetch';
import { DemoEngine, type EngineScript } from '../_engine/engine';
import { engineSupport, type EngineSupport } from '../_engine/support';
import { demo } from '../_state/demo.state.svelte';

/** How often the engine adds an order to the source table. */
const ORDER_EVERY_MS: number = 4000;

/**
 * Answers the UI's API for the UI demo build: from a real engine in this tab where the browser can
 * run it, otherwise from the recorded responses, with a note that says why.
 */
export function installDemoFetch(): void {
	const base: string | undefined = import.meta.env.VITE_SQLTIDE_DEMO_ENGINE;
	const support: EngineSupport =
		base === undefined ? { real: false, reason: '' } : engineSupport();
	if (!support.real || base === undefined) {
		demo.mode = { kind: 'recorded', reason: support.real ? '' : support.reason };
		install();
		return;
	}
	demo.mode = { kind: 'real', progress: null, engine: null };
	const started: Promise<DemoEngine> = (async () => {
		const { default: script } = (await import('../engine-script.json')) as { default: EngineScript };
		const engine: DemoEngine = await DemoEngine.start(base, script, (progress) => {
			if (demo.mode.kind === 'real') demo.mode = { kind: 'real', progress, engine: null };
		});
		await engine.startOrders(ORDER_EVERY_MS);
		return engine;
	})();
	started.then(
		(engine) => {
			if (demo.mode.kind === 'real') demo.mode = { kind: 'real', progress: null, engine };
		},
		(error: unknown) => {
			console.error('The demo engine could not start:', error);
			demo.mode = { kind: 'failed', error: error instanceof Error ? error.message : String(error) };
		}
	);
	const link: DemoEngineLink = {
		active: () => demo.mode.kind !== 'recorded',
		ready: () => (demo.mode.kind === 'real' ? demo.mode.engine : null),
		whenReady: () => started,
		promoted: () => demo.promoted,
		markPromoted: () => {
			demo.promoted = true;
		}
	};
	install(link);
}
