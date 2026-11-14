/** A deployment an action creates: its slot name, and when after the action started sqt named it. */
export type PreparedDeployment = { name: string; offset: number };

/** What a step, action or plan binds when it runs: the deployments it creates and its times. */
export type PreparedBinding = {
	creates: PreparedDeployment[];
	/** Each time a role wrote: the role, its ordinal in {{t:role:ordinal:format}}, and how long after the role's start it fell. */
	times: [owner: string, ordinal: number, offset: number][];
};

/** One step of the story's history, replayed when the engine starts; statements index PreparedDemo.statements. */
export type PreparedStep = PreparedBinding & {
	role: string;
	title: string;
	statements: number[];
};

/** What Execute, Promote or Roll back does in one state, as sqt did it (see scripts/prepare-demo.mjs). */
export type PreparedAction = PreparedBinding & {
	role: string;
	response: DemoAnswer;
	/** Indexes into PreparedDemo.statements, in the order sqt ran them. */
	statements: number[];
	/** The state the action leads to, or null for one the demo has nothing prepared for. */
	next: string | null;
};

/** An API answer: a status and a JSON body. */
export type DemoAnswer = { status: number; body: unknown };

export type PreparedState = {
	/**
	 * `sqt dev`'s answer to GET /api/plan, by selection key (see selectionKey) and whether it
	 * counted replay rows, with the deployment the plan proposes.
	 */
	plans: Record<string, DemoAnswer & PreparedBinding>;
	/** By `build <selection key>`, `promote <deployment>` or `rollback <deployment>`. */
	actions: Record<string, PreparedAction>;
};

/** src/lib/demo/prepared.json. */
export type PreparedDemo = {
	preparedWith: string;
	database: string;
	start: string;
	setup: PreparedStep[];
	states: Record<string, PreparedState>;
	/** Every distinct statement, written once, because most builds share most of their SQL. */
	statements: string[];
};

/** What the slots in prepared text resolve to in this tab. */
export type SlotValues = {
	/** Deployment IDs by slot name, and the time each was created. */
	deployments: Record<string, { id: string; at: number }>;
	uuids: Record<string, string>;
	/** When each role last started, in epoch milliseconds. */
	roleStarts: Record<string, number>;
	/** The times each role wrote, by ordinal, in epoch milliseconds. */
	times: Record<string, number[]>;
	/** The sqt version that prepared the demo ({{v}}). */
	version: string;
	/** The "From a time" start of the plan or build being filled ({{s:...}}), or null. */
	startTime: number | null;
};

/** What the Plan page's Execute posts to /api/build. */
export type BuildRequest = {
	selectors: string[];
	changed?: boolean;
	includeMissingUpstream?: boolean;
	startTime: string | null;
	deploymentId?: string;
	confirmations: string[];
};

/** The story's state machine on the engine (see createStoryResource). */
export type DemoStory = {
	/** Drops the story's database and replays its history, so the story starts over. */
	startOver(): Promise<void>;
	/** Replays the story's history up to its first state, reporting each statement. */
	replaySetup(onStep: (title: string, done: number, total: number) => void): Promise<void>;
	/** sqt's plan for a GET /api/plan query in the current state. */
	plan(params: URLSearchParams): DemoAnswer;
	/** Starts the prepared build for a selection and answers at once, as `sqt dev` does. */
	build(request: BuildRequest): Promise<DemoAnswer>;
	/** Runs a prepared promotion or rollback, then answers. */
	deploymentAction(kind: 'promote' | 'rollback', deploymentId: string): Promise<DemoAnswer>;
	/** The body of GET /api/build/current: the last build, and whether it still runs. */
	buildFeed(): unknown;
	/** The current state's name, or null once an action left the prepared states. */
	readonly stateName: string | null;
};

export type EngineProgress =
	| { phase: 'download'; loaded: number; total: number }
	| { phase: 'start' }
	| { phase: 'replay'; step: string; done: number; total: number };

export type TableSize = { rows: number; bytes: number };

/** Whether this browser runs the demo's real engine, and if not, the note that says why. */
export type EngineSupport = { real: true } | { real: false; reason: string };

/** The demo's real ClickHouse: chdb-wasm in a worker, adding orders to the source. */
export type DemoEngine = {
	readonly database: string;
	/** Runs one statement; queries run one at a time, in the order they were asked for. */
	run(sql: string, format?: string): Promise<string>;
	/** Rows of a query, as objects. */
	rows<Row>(sql: string): Promise<Row[]>;
	/** Each table's rows and bytes, as ClickHouse reports them. */
	tableSizes(): Promise<Map<string, TableSize>>;
	/** Adds an order to the source table every few seconds, as a stream would. */
	startOrders(): Promise<void>;
	stopOrders(): void;
	readonly addingOrders: boolean;
};

/** The engine with the story replayed on it. */
export type LiveDemo = { engine: DemoEngine; story: DemoStory };

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
	| { kind: 'real'; progress: EngineProgress | null; live: LiveDemo | null }
	| { kind: 'failed'; error: string };

export type DemoState = { mode: DemoMode };

export type DemoController = {
	readonly demo: DemoState;
	/** Settles once the demo answers from the recording. */
	readonly recordingChosen: Promise<void>;
	/** Answers from the recording, with the note that says why (empty when there is none). */
	useRecording(reason: string): void;
	/** The real engine starts loading. */
	startEngine(): void;
	/** How far the engine got, unless the visitor switched to the recording meanwhile. */
	showProgress(progress: EngineProgress): void;
	/** The engine is ready, unless the visitor switched to the recording while it loaded. */
	engineReady(live: LiveDemo): void;
	engineFailed(error: string): void;
};

/** The real engine as the API sees it. */
export type DemoEngineLink = {
	/** False once the visitor has switched to the recording. */
	active(): boolean;
	/**
	 * Waits for the engine and the story. When the engine cannot start, waits for the visitor to
	 * choose the recording, then gives null.
	 */
	whenReady(): Promise<LiveDemo | null>;
};
