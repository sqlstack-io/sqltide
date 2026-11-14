import { ORDER_EVERY_MS } from '../constants';
import type { OrderGeneratorResource } from '../types';

export function createOrderGeneratorResource(addOrder: () => Promise<void>): OrderGeneratorResource {
	let timer: ReturnType<typeof setInterval> | null = null;

	function start(): void {
		stop();
		timer = setInterval(() => void addOrder(), ORDER_EVERY_MS);
	}

	function stop(): void {
		if (timer !== null) clearInterval(timer);
		timer = null;
	}

	return {
		start,
		stop,
		get running(): boolean {
			return timer !== null;
		}
	};
}
