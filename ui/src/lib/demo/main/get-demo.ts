import { getDemoInstance } from '../_helpers/demo-instance.svelte';
import type { DemoController } from '../types';

/** Where the UI demo's answers come from now, and the actions that change it. */
export function getDemo(): DemoController {
	return getDemoInstance();
}
