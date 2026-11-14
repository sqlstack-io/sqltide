import type { z } from 'zod';
import { engineManifestSchema } from '../schemas';
import type { EngineProgress } from '../types';

/**
 * Downloads the engine's gzip parts side by side, joins and gunzips them, and returns a blob URL of
 * the wasm for the engine's worker to load.
 */
export async function downloadEngine(base: string, onProgress: (progress: EngineProgress) => void): Promise<string> {
	const manifestResponse: Response = await fetch(`${base}manifest.json`);
	if (!manifestResponse.ok) throw new Error(`engine manifest: HTTP ${manifestResponse.status}`);
	const manifest: z.infer<typeof engineManifestSchema> = engineManifestSchema.parse(await manifestResponse.json());
	const total: number = manifest.parts.reduce((sum, part) => sum + part.bytes, 0);
	let loaded: number = 0;
	onProgress({ phase: 'download', loaded, total });
	const parts: Blob[] = await Promise.all(
		manifest.parts.map(async ({ name }) => {
			const response: Response = await fetch(`${base}${name}`);
			if (!response.ok || response.body === null) throw new Error(`engine download ${name}: HTTP ${response.status}`);
			const chunks: Uint8Array<ArrayBuffer>[] = [];
			const reader: ReadableStreamDefaultReader<Uint8Array<ArrayBuffer>> = response.body.getReader();
			for (;;) {
				const { done, value } = await reader.read();
				if (done) break;
				chunks.push(value);
				loaded += value.length;
				onProgress({ phase: 'download', loaded, total });
			}
			return new Blob(chunks);
		})
	);
	onProgress({ phase: 'start' });
	const wasm: Blob = await new Response(
		new Blob(parts).stream().pipeThrough(new DecompressionStream('gzip'))
	).blob();
	if (wasm.size !== manifest.wasmBytes) {
		throw new Error(`engine download was cut short: ${wasm.size} of ${manifest.wasmBytes} bytes`);
	}
	return URL.createObjectURL(new Blob([wasm], { type: 'application/wasm' }));
}
