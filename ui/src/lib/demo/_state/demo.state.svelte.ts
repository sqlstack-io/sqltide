import type { DemoEngine, EngineProgress } from '../_engine/engine';

/**
 * Where the demo's answers come from. `real`: the engine in this tab, which may still be loading.
 * `failed`: the engine could not start, and the visitor has not yet chosen the recording.
 */
export type DemoMode =
	| { kind: 'recorded'; reason: string }
	| { kind: 'real'; progress: EngineProgress | null; engine: DemoEngine | null }
	| { kind: 'failed'; error: string };

export const demo: { mode: DemoMode; promoted: boolean } = $state({
	mode: { kind: 'recorded', reason: '' },
	promoted: false
});
