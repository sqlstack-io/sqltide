import type { DemoController, DemoState, EngineProgress, LiveDemo } from '../types';

export function createDemoState(): DemoController {
	const demo: DemoState = $state({ mode: { kind: 'recorded', reason: '' } });

	let chooseRecording: () => void = () => undefined;
	const recordingChosen: Promise<void> = new Promise<void>((resolve) => (chooseRecording = resolve));

	function useRecording(reason: string): void {
		demo.mode = { kind: 'recorded', reason };
		chooseRecording();
	}

	function startEngine(): void {
		demo.mode = { kind: 'real', progress: null, live: null };
	}

	function showProgress(progress: EngineProgress): void {
		if (demo.mode.kind === 'real') demo.mode = { kind: 'real', progress, live: null };
	}

	function engineReady(live: LiveDemo): void {
		if (demo.mode.kind === 'real') demo.mode = { kind: 'real', progress: null, live };
	}

	function engineFailed(error: string): void {
		demo.mode = { kind: 'failed', error };
	}

	return { demo, recordingChosen, useRecording, startEngine, showProgress, engineReady, engineFailed };
}
