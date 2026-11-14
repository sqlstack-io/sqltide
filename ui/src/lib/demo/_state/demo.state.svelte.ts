import type { DemoController, DemoEngine, DemoState, EngineProgress } from '../types';

export function createDemoState(): DemoController {
	const demo: DemoState = $state({ mode: { kind: 'recorded', reason: '' }, promoted: false });

	function useRecording(reason: string): void {
		demo.mode = { kind: 'recorded', reason };
	}

	function startEngine(): void {
		demo.mode = { kind: 'real', progress: null, engine: null };
	}

	function showProgress(progress: EngineProgress): void {
		if (demo.mode.kind === 'real') demo.mode = { kind: 'real', progress, engine: null };
	}

	function engineReady(engine: DemoEngine): void {
		if (demo.mode.kind === 'real') demo.mode = { kind: 'real', progress: null, engine };
	}

	function engineFailed(error: string): void {
		demo.mode = { kind: 'failed', error };
	}

	function markPromoted(): void {
		demo.promoted = true;
	}

	return { demo, useRecording, startEngine, showProgress, engineReady, engineFailed, markPromoted };
}
