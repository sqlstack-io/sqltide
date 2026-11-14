/**
 * The demo's real ClickHouse: chdb-wasm's multi-threaded build in a worker. It replays the
 * statements sqt wrote for each step of the recorded story, so the tables, materialized views and
 * views behind the UI exist here, then keeps adding orders to the source.
 */

/** What chdb-wasm's module exports that the demo uses. */
type ChdbModule = {
	AsyncChdb: {
		create(options: { moduleUrl: string; wasmUrl: string }): Promise<ChdbClient>;
	};
};
type ChdbClient = {
	query(sql: string, format?: string): Promise<{ text(): string }>;
	terminate(): Promise<void>;
};

/** The statements sqt wrote, as src/lib/demo/engine-script.json holds them. */
export type EngineScript = {
	database: string;
	steps: { title: string; statements: string[] }[];
	promote: string[];
};

export type EngineProgress =
	| { phase: 'download'; loaded: number; total: number }
	| { phase: 'start' }
	| { phase: 'replay'; step: string; done: number; total: number };

export type TableSize = { rows: number; bytes: number };

type Manifest = { wasmBytes: number; parts: { name: string; bytes: number }[] };

async function download(base: string, onProgress: (progress: EngineProgress) => void): Promise<string> {
	const manifestResponse: Response = await fetch(`${base}manifest.json`);
	if (!manifestResponse.ok) throw new Error(`engine manifest: HTTP ${manifestResponse.status}`);
	const manifest: Manifest = await manifestResponse.json();
	const total: number = manifest.parts.reduce((sum, part) => sum + part.bytes, 0);
	let loaded: number = 0;
	onProgress({ phase: 'download', loaded, total });
	const parts: Blob[] = await Promise.all(
		manifest.parts.map(async ({ name }) => {
			const response: Response = await fetch(`${base}${name}`);
			if (!response.ok || response.body === null) throw new Error(`engine download ${name}: HTTP ${response.status}`);
			const chunks: Uint8Array<ArrayBuffer>[] = [];
			const reader = response.body.getReader();
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

export class DemoEngine {
	private queue: Promise<unknown> = Promise.resolve();
	private generator: ReturnType<typeof setInterval> | null = null;
	private nextOrder: { id: number; offset: number } = { id: 0, offset: 0 };

	private constructor(
		private readonly client: ChdbClient,
		private readonly script: EngineScript
	) {}

	/** Downloads and starts the engine, then replays the story up to the staged change. */
	static async start(
		base: string,
		script: EngineScript,
		onProgress: (progress: EngineProgress) => void
	): Promise<DemoEngine> {
		const wasmUrl: string = await download(base, onProgress);
		let client: ChdbClient;
		try {
			const chdb: ChdbModule = await import(/* @vite-ignore */ `${base}index.js`);
			client = await chdb.AsyncChdb.create({ moduleUrl: `${base}chdb.mjs`, wasmUrl });
		} finally {
			URL.revokeObjectURL(wasmUrl);
		}
		const engine: DemoEngine = new DemoEngine(client, script);
		const total: number = script.steps.reduce((sum, step) => sum + step.statements.length, 0);
		let done: number = 0;
		for (const step of script.steps) {
			for (const statement of step.statements) {
				onProgress({ phase: 'replay', step: step.title, done, total });
				await engine.run(statement);
				done += 1;
			}
		}
		onProgress({ phase: 'replay', step: 'Ready', done, total });
		return engine;
	}

	/**
	 * Runs one statement. The engine runs one query at a time and deadlocks on overlapping ones, so
	 * every caller waits its turn.
	 */
	run(sql: string, format: string = 'TabSeparated'): Promise<string> {
		const turn: Promise<string> = this.queue.then(async () => {
			try {
				return (await this.client.query(sql, format)).text();
			} catch (error) {
				throw new Error(`${error instanceof Error ? error.message : String(error)}\n  in: ${sql.slice(0, 300)}`);
			}
		});
		this.queue = turn.catch(() => undefined);
		return turn;
	}

	/** Rows of a query, as objects. */
	async rows<Row>(sql: string): Promise<Row[]> {
		const text: string = await this.run(sql, 'JSONEachRow');
		return text
			.split('\n')
			.filter((line) => line.trim() !== '')
			.map((line) => JSON.parse(line) as Row);
	}

	get database(): string {
		return this.script.database;
	}

	/** Runs the statements sqt wrote when the staged deployment was promoted. */
	async promote(): Promise<void> {
		for (const statement of this.script.promote) await this.run(statement);
	}

	/** Each table's rows and bytes, as ClickHouse reports them. */
	async tableSizes(): Promise<Map<string, TableSize>> {
		const rows = await this.rows<{ name: string; rows: string | number | null; bytes: string | number | null }>(
			`SELECT name, total_rows AS rows, total_bytes AS bytes FROM system.tables WHERE database = '${this.database}'`
		);
		return new Map(rows.map((row) => [row.name, { rows: Number(row.rows ?? 0), bytes: Number(row.bytes ?? 0) }]));
	}

	/** Adds an order to the source table every few seconds, as a stream would. */
	async startOrders(everyMs: number): Promise<void> {
		const [latest] = await this.rows<{ id: string | number; offset: string | number }>(
			`SELECT max(order_id) AS id, max(_replay_offset) AS offset FROM ${this.database}.playground_orders`
		);
		this.nextOrder = { id: Number(latest?.id ?? 0) + 1, offset: Number(latest?.offset ?? 0) + 1 };
		this.stopOrders();
		this.generator = setInterval(() => void this.addOrder(), everyMs);
	}

	stopOrders(): void {
		if (this.generator !== null) clearInterval(this.generator);
		this.generator = null;
	}

	get addingOrders(): boolean {
		return this.generator !== null;
	}

	/** One new order: every third one is still pending, so the completed-orders filter shows. */
	async addOrder(): Promise<void> {
		const { id, offset } = this.nextOrder;
		this.nextOrder = { id: id + 1, offset: offset + 1 };
		const status: string = id % 3 === 0 ? 'pending' : 'completed';
		const totalCents: number = 500 + ((id * 1373) % 5500);
		await this.run(
			`INSERT INTO ${this.database}.playground_orders VALUES (${id}, '${status}', ${totalCents}, 0, ${offset}, now64(3))`
		);
	}
}
