export type PollingResource = {
	start(): void;
	stop(): void;
};

/**
 * How often the live state refreshes. The UI demo reads its engine in the page, which a new order
 * changes every few seconds, so its pages follow it more closely than `sqt dev`'s held snapshot.
 */
const LIVE_STATE_POLL_MS: number = import.meta.env.VITE_SQLTIDE_DEMO === '1' ? 5_000 : 30_000;

export function createPollingResource(refresh: () => Promise<void>): PollingResource {
	let started: boolean = false;
	let timer: ReturnType<typeof setInterval> | null = null;

	return {
		start(): void {
			if (started || typeof document === 'undefined') return;
			started = true;
			timer = setInterval(() => {
				if (!document.hidden) void refresh();
			}, LIVE_STATE_POLL_MS);
			void timer;
		},
		stop(): void {
			if (timer !== null) clearInterval(timer);
			timer = null;
			started = false;
		}
	};
}
