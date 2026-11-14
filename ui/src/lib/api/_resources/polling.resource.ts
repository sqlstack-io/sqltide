export type PollingResource = {
	start(): void;
	stop(): void;
};

export function createPollingResource(refresh: () => Promise<void>): PollingResource {
	let started: boolean = false;
	let timer: ReturnType<typeof setInterval> | null = null;

	return {
		start(): void {
			if (started || typeof document === 'undefined') return;
			started = true;
			timer = setInterval(() => {
				if (!document.hidden) void refresh();
			}, 30_000);
			void timer;
		},
		stop(): void {
			if (timer !== null) clearInterval(timer);
			timer = null;
			started = false;
		}
	};
}
