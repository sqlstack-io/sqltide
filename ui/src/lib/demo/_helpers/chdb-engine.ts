/**
 * The demo's real ClickHouse: chdb-wasm's multi-threaded build in a worker, which runs the
 * statements sqt wrote for the story and keeps adding orders to the source.
 */
import { downloadEngine } from '../_api/download-engine';
import { createOrderGeneratorResource } from '../_resources/order-generator.resource';
import type { DemoEngine, EngineProgress, OrderGeneratorResource, TableSize } from '../types';

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

type SizeRow = { name: string; rows: string | number | null; bytes: string | number | null };
type LatestOrderRow = { id: string | number; offset: string | number };

/** Downloads and starts the engine, with an empty `database`. */
export async function startChdbEngine(
	base: string,
	database: string,
	onProgress: (progress: EngineProgress) => void
): Promise<DemoEngine> {
	/**
	 * A new engine in its own worker. The browser gives each worker a fixed pool of threads, which
	 * the default asynchronous file reads and one pipeline thread per core can use up ("Cannot
	 * schedule a task"); the demo's tables are small, so it reads synchronously on two threads.
	 */
	async function connect(progress: (progress: EngineProgress) => void): Promise<ChdbClient> {
		const wasmUrl: string = await downloadEngine(base, progress);
		let created: ChdbClient;
		try {
			const chdb: ChdbModule = await import(/* @vite-ignore */ `${base}index.js`);
			created = await chdb.AsyncChdb.create({ moduleUrl: `${base}chdb.mjs`, wasmUrl });
		} finally {
			URL.revokeObjectURL(wasmUrl);
		}
		try {
			await created.query('SET max_threads = 2');
			await created.query(`SET local_filesystem_read_method = 'pread'`);
		} catch (error) {
			await created.terminate();
			throw error;
		}
		return created;
	}
	// Null while a restart has stopped the old engine and the new one has not started.
	let client: ChdbClient | null = await connect(onProgress);

	// The engine deadlocks on overlapping queries, so every caller waits its turn.
	let queue: Promise<unknown> = Promise.resolve();
	let nextOrder: { id: number; offset: number } = { id: 0, offset: 0 };

	function run(sql: string, format: string = 'TabSeparated'): Promise<string> {
		const turn: Promise<string> = queue.then(async () => {
			// A query sent to a stopped engine would wait forever, so it fails here instead.
			if (client === null) throw new Error('The engine stopped while starting over. Use Start over again, or reload the page.');
			try {
				return (await client.query(sql, format)).text();
			} catch (error) {
				throw new Error(`${error instanceof Error ? error.message : String(error)}\n  in: ${sql.slice(0, 300)}`);
			}
		});
		queue = turn.catch(() => undefined);
		return turn;
	}

	async function rows<Row>(sql: string): Promise<Row[]> {
		const text: string = await run(sql, 'JSONEachRow');
		return text
			.split('\n')
			.filter((line) => line.trim() !== '')
			.map((line) => JSON.parse(line) as Row);
	}

	/**
	 * One new order: every third one is still pending, so the completed-orders filter shows. While
	 * an action switches tables a view can be missing for a moment, so a refused order is tried
	 * again on the next tick with the same ID.
	 */
	async function addOrder(): Promise<void> {
		const { id, offset } = nextOrder;
		const status: string = id % 3 === 0 ? 'pending' : 'completed';
		const totalCents: number = 500 + ((id * 1373) % 5500);
		try {
			await run(`INSERT INTO ${database}.playground_orders VALUES (${id}, '${status}', ${totalCents}, 0, ${offset}, now64(3))`);
		} catch {
			return;
		}
		if (nextOrder.id === id) nextOrder = { id: id + 1, offset: offset + 1 };
	}

	const generator: OrderGeneratorResource = createOrderGeneratorResource(addOrder);

	const engine: DemoEngine = {
		database,
		run,
		rows,
		restart(): Promise<void> {
			// Dropping the database instead runs out of the worker's threads, and dropped tables
			// stay in memory for minutes; a new worker starts empty from the cached download.
			const turn: Promise<void> = queue.then(async () => {
				const stopping: ChdbClient | null = client;
				client = null;
				await stopping?.terminate();
				client = await connect(() => undefined);
			});
			queue = turn.catch(() => undefined);
			return turn;
		},
		async tableSizes(): Promise<Map<string, TableSize>> {
			const sizes: SizeRow[] = await rows<SizeRow>(
				`SELECT name, total_rows AS rows, total_bytes AS bytes FROM system.tables WHERE database = '${database}'`
			);
			return new Map(sizes.map((row) => [row.name, { rows: Number(row.rows ?? 0), bytes: Number(row.bytes ?? 0) }]));
		},
		async startOrders(): Promise<void> {
			const [latest]: LatestOrderRow[] = await rows<LatestOrderRow>(
				`SELECT max(order_id) AS id, max(_replay_offset) AS offset FROM ${database}.playground_orders`
			);
			nextOrder = { id: Number(latest?.id ?? 0) + 1, offset: Number(latest?.offset ?? 0) + 1 };
			generator.start();
		},
		stopOrders: generator.stop,
		get addingOrders(): boolean {
			return generator.running;
		}
	};

	return engine;
}
