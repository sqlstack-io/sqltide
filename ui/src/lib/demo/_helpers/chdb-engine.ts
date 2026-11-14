/**
 * The demo's real ClickHouse: chdb-wasm's multi-threaded build in a worker. It replays the
 * statements sqt wrote for each step of the recorded story, so the tables, materialized views and
 * views behind the UI exist here, then keeps adding orders to the source.
 */
import { downloadEngine } from '../_api/download-engine';
import { createOrderGeneratorResource } from '../_resources/order-generator.resource';
import type { DemoEngine, EngineProgress, EngineScript, OrderGeneratorResource, TableSize } from '../types';

/** What chdb-wasm's module exports that the demo uses. */
type ChdbModule = {
	AsyncChdb: {
		create(options: { moduleUrl: string; wasmUrl: string }): Promise<ChdbClient>;
	};
};
type ChdbClient = {
	query(sql: string, format?: string): Promise<{ text(): string }>;
};

type SizeRow = { name: string; rows: string | number | null; bytes: string | number | null };
type LatestOrderRow = { id: string | number; offset: string | number };

/** Downloads and starts the engine, then replays the story up to the staged change. */
export async function startChdbEngine(
	base: string,
	script: EngineScript,
	onProgress: (progress: EngineProgress) => void
): Promise<DemoEngine> {
	const wasmUrl: string = await downloadEngine(base, onProgress);
	let client: ChdbClient;
	try {
		const chdb: ChdbModule = await import(/* @vite-ignore */ `${base}index.js`);
		client = await chdb.AsyncChdb.create({ moduleUrl: `${base}chdb.mjs`, wasmUrl });
	} finally {
		URL.revokeObjectURL(wasmUrl);
	}

	const { database } = script;
	// The engine deadlocks on overlapping queries, so every caller waits its turn.
	let queue: Promise<unknown> = Promise.resolve();
	let nextOrder: { id: number; offset: number } = { id: 0, offset: 0 };

	function run(sql: string, format: string = 'TabSeparated'): Promise<string> {
		const turn: Promise<string> = queue.then(async () => {
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

	/** One new order: every third one is still pending, so the completed-orders filter shows. */
	async function addOrder(): Promise<void> {
		const { id, offset } = nextOrder;
		nextOrder = { id: id + 1, offset: offset + 1 };
		const status: string = id % 3 === 0 ? 'pending' : 'completed';
		const totalCents: number = 500 + ((id * 1373) % 5500);
		await run(`INSERT INTO ${database}.playground_orders VALUES (${id}, '${status}', ${totalCents}, 0, ${offset}, now64(3))`);
	}

	const generator: OrderGeneratorResource = createOrderGeneratorResource(addOrder);

	const engine: DemoEngine = {
		database,
		run,
		rows,
		async promote(): Promise<void> {
			for (const statement of script.promote) await run(statement);
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

	const total: number = script.steps.reduce((sum, step) => sum + step.statements.length, 0);
	let done: number = 0;
	for (const step of script.steps) {
		for (const statement of step.statements) {
			onProgress({ phase: 'replay', step: step.title, done, total });
			await run(statement);
			done += 1;
		}
	}
	onProgress({ phase: 'replay', step: 'Ready', done, total });
	return engine;
}
