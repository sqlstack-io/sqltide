import { base } from '$app/paths';
import { goto } from '$app/navigation';

export async function openRun(invocationId: string): Promise<void> {
	await goto(`${base}/runs/${invocationId}?live=1`);
}
