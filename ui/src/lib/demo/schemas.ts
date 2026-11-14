import { z } from 'zod';

/** The website build's description of the engine's gzip parts. */
export const engineManifestSchema = z.object({
	wasmBytes: z.number().int().positive(),
	parts: z.array(z.object({ name: z.string().min(1), bytes: z.number().int().nonnegative() })).min(1)
});
