import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

/** Runs scripts/demo-engine.check.ts, apart from the unit tests; see that file. */
export default defineConfig({
	plugins: [sveltekit()],
	test: { include: ['scripts/demo-engine.check.ts'], testTimeout: 3_600_000 }
});
