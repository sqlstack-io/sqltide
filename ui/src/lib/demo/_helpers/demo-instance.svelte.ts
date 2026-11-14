import { createDemoState } from '../_state/demo.state.svelte';
import type { DemoController } from '../types';

let instance: DemoController | undefined;

export function getDemoInstance(): DemoController {
	instance ??= createDemoState();
	return instance;
}
