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

/** Whether this browser runs the demo's real engine, and if not, the note that says why. */
export type EngineSupport = { real: true } | { real: false; reason: string };

/**
 * The demo's real ClickHouse: chdb-wasm in a worker, holding the tables sqt wrote for the recorded
 * story and adding orders to the source.
 */
export type DemoEngine = {
	readonly database: string;
	/** Runs one statement; queries run one at a time, in the order they were asked for. */
	run(sql: string, format?: string): Promise<string>;
	/** Rows of a query, as objects. */
	rows<Row>(sql: string): Promise<Row[]>;
	/** Runs the statements sqt wrote when the staged deployment was promoted. */
	promote(): Promise<void>;
	/** Each table's rows and bytes, as ClickHouse reports them. */
	tableSizes(): Promise<Map<string, TableSize>>;
	/** Adds an order to the source table every few seconds, as a stream would. */
	startOrders(): Promise<void>;
	stopOrders(): void;
	readonly addingOrders: boolean;
};

/** Adds an order on a timer until stopped. */
export type OrderGeneratorResource = {
	start(): void;
	stop(): void;
	readonly running: boolean;
};

/**
 * Where the demo's answers come from. `real`: the engine in this tab, which may still be loading.
 * `failed`: the engine could not start, and the visitor has not yet chosen the recording.
 */
export type DemoMode =
	| { kind: 'recorded'; reason: string }
	| { kind: 'real'; progress: EngineProgress | null; engine: DemoEngine | null }
	| { kind: 'failed'; error: string };

export type DemoState = { mode: DemoMode; promoted: boolean };

export type DemoController = {
	readonly demo: DemoState;
	/** Answers from the recording, with the note that says why (empty when there is none). */
	useRecording(reason: string): void;
	/** The real engine starts loading. */
	startEngine(): void;
	/** How far the engine got, unless the visitor switched to the recording meanwhile. */
	showProgress(progress: EngineProgress): void;
	/** The engine is ready, unless the visitor switched to the recording while it loaded. */
	engineReady(engine: DemoEngine): void;
	engineFailed(error: string): void;
	markPromoted(): void;
};

/**
 * The real engine as the API sees it: null while it loads, or in the recorded demo. `promote` runs
 * the promotion's statements, then the API answers from the recording made after it.
 */
export type DemoEngineLink = {
	/** False once the visitor has switched to the recording. */
	active(): boolean;
	ready(): Pick<DemoEngine, 'promote' | 'tableSizes'> | null;
	/** Waits for the engine; rejects when it cannot start. */
	whenReady(): Promise<Pick<DemoEngine, 'promote'>>;
	promoted(): boolean;
	markPromoted(): void;
};
