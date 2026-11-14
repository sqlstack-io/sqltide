import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';

import { ORDER_EVERY_MS } from '$lib/demo/constants';
import { createOrderGeneratorResource } from '$lib/demo/_resources/order-generator.resource';
import type { OrderGeneratorResource } from '$lib/demo/types';

describe('order generator resource', () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => vi.useRealTimers());

	it('given a started generator when time passes and it stops then it adds an order each interval until stopped', () => {
		const addOrder: MockInstance<() => Promise<void>> & (() => Promise<void>) = vi
			.fn<() => Promise<void>>()
			.mockResolvedValue(undefined);
		const generator: OrderGeneratorResource = createOrderGeneratorResource(addOrder);

		generator.start();
		generator.start();
		vi.advanceTimersByTime(ORDER_EVERY_MS * 2);
		const runningWhileStarted: boolean = generator.running;
		generator.stop();
		vi.advanceTimersByTime(ORDER_EVERY_MS * 3);

		expect(addOrder).toHaveBeenCalledTimes(2);
		expect([runningWhileStarted, generator.running]).toEqual([true, false]);
	});
});
