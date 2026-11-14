import { resolveObjectURL } from 'node:buffer';
import { gzipSync } from 'node:zlib';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { downloadEngine } from '$lib/demo/_api/download-engine';
import type { EngineProgress } from '$lib/demo/types';

const wasm: Uint8Array = new TextEncoder().encode('not really wasm, but it round-trips through gzip');

/** Serves a manifest and the gzipped bytes split in two, the way the website build writes them. */
function serve(manifest: unknown, packed: Uint8Array): void {
	const half: number = Math.ceil(packed.length / 2);
	const files: Record<string, BodyInit> = {
		'/engine/manifest.json': JSON.stringify(manifest),
		'/engine/chdb.wasm.gz.0': packed.slice(0, half),
		'/engine/chdb.wasm.gz.1': packed.slice(half)
	};
	vi.stubGlobal(
		'fetch',
		vi.fn(async (url: string) => (url in files ? new Response(files[url]) : new Response('missing', { status: 404 })))
	);
}

function manifestFor(packed: Uint8Array, wasmBytes: number): unknown {
	const half: number = Math.ceil(packed.length / 2);
	return {
		wasmBytes,
		parts: [
			{ name: 'chdb.wasm.gz.0', bytes: half },
			{ name: 'chdb.wasm.gz.1', bytes: packed.length - half }
		]
	};
}

describe('download engine', () => {
	afterEach(() => vi.unstubAllGlobals());

	it('given two gzip parts when downloaded then the joined wasm is served from a blob URL with progress', async () => {
		const packed: Uint8Array = gzipSync(wasm);
		serve(manifestFor(packed, wasm.length), packed);
		const progress: EngineProgress[] = [];

		const url: string = await downloadEngine('/engine/', (step) => progress.push(step));

		const blob: ReturnType<typeof resolveObjectURL> = resolveObjectURL(url);
		expect(blob?.type).toBe('application/wasm');
		expect(new Uint8Array((await blob?.arrayBuffer()) ?? new ArrayBuffer(0))).toEqual(wasm);
		expect(progress.at(0)).toEqual({ phase: 'download', loaded: 0, total: packed.length });
		expect(progress.at(-2)).toEqual({ phase: 'download', loaded: packed.length, total: packed.length });
		expect(progress.at(-1)).toEqual({ phase: 'start' });
		URL.revokeObjectURL(url);
	});

	it('given a manifest that expects more bytes when downloaded then it fails as cut short', async () => {
		const packed: Uint8Array = gzipSync(wasm);
		serve(manifestFor(packed, wasm.length + 1), packed);

		await expect(downloadEngine('/engine/', () => undefined)).rejects.toThrow('engine download was cut short');
	});

	it('given a malformed manifest when downloaded then it fails before fetching any part', async () => {
		serve({ wasmBytes: 1, parts: [] }, gzipSync(wasm));

		await expect(downloadEngine('/engine/', () => undefined)).rejects.toThrow();
		expect(fetch).toHaveBeenCalledOnce();
	});
});
