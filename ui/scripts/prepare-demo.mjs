/**
 * Prepares what the UI demo at sqltide.io/demo/ runs in the browser.
 *
 * The demo runs a real ClickHouse (chdb-wasm) in the page, but not sqt itself, so everything sqt
 * decides is worked out here, by real sqt against a real ClickHouse (`sqt playground`):
 *
 * - The story's states: the playground published with large_orders and the completed-orders change
 *   written but not built (nothing staged), then that change built (staged), promoted, and rolled
 *   back. A build that touches different models leads to its own staged, promoted and rolled-back
 *   states.
 * - In each state, the plan `sqt dev` returns for every selection the Plan page offers, and what
 *   Execute, Promote and Roll back write to ClickHouse there, statement by statement, in order. An
 *   action that leaves the story's states ends in a state the demo has nothing prepared for.
 *
 * sqt reaches ClickHouse through a proxy that keeps the statements each action writes and ClickHouse
 * accepted. The browser engine refuses two statements and hangs on a third, so they are rewritten:
 * CREATE OR REPLACE VIEW becomes DROP and CREATE, EXCHANGE TABLES becomes a three-way RENAME, and
 * DROP ... SYNC loses SYNC. Values that differ on every run become slots the page fills in when it
 * runs the action: deployment IDs ({{d:name}}), UUIDs ({{u:...}}) and timestamps ({{t:...}}), which
 * are kept relative to the action that wrote them.
 *
 * It also records every page of `sqt dev` in the staged state, for browsers that cannot run the
 * engine.
 *
 * Writes src/lib/demo/prepared.json and src/lib/demo/recordings.json. With --check it writes
 * nothing and fails when prepared.json differs from what sqt does now, which CI runs.
 *
 *   node scripts/prepare-demo.mjs [--output-dir DIR | --check]
 *
 * Environment:
 *   SQT           sqt executable (default: sqt on PATH)
 *   CHROME_PATH   explicit browser binary, for the recordings
 */
import { execFileSync, spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { createServer as createHttpServer, request as httpRequest } from 'node:http';
import { tmpdir } from 'node:os';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { gunzipSync, inflateSync } from 'node:zlib';
import { chromium } from 'playwright-core';

const SQT = process.env.SQT ?? 'sqt';
const outputFlag = process.argv.indexOf('--output-dir');
const COMMITTED_DIR = new URL('../src/lib/demo/', import.meta.url);
const CHECK = process.argv.includes('--check');
const OUTPUT_DIR =
	outputFlag === -1 ? COMMITTED_DIR : new URL(`file://${process.argv[outputFlag + 1]}/`);
const MAX_PAGES = 120;
const SKIPPED_PAGES = ['/login', '/destruction'];
const LARGE_ORDERS_SQL = `MODEL (
  engine "MergeTree()",
  order_by ["order_id"]
);

SELECT
  order_id::UInt64 AS order_id,
  total_cents::Int64 AS total_cents,
  _replay_partition::Int32 AS _replay_partition,
  _replay_offset::Int64 AS _replay_offset
FROM __ref("order_report")
WHERE total_cents >= 1000
`;

/** What the Plan page can select: the pipeline and each model, in any combination. */
const SELECTABLE = ['pipeline:pl__orders', 'order_report', 'large_orders'];

/**
 * A start time for "From a time". sqt writes the start into the plan and the SQL as given, so the
 * demo fills in whatever time the visitor picks ({{s:...}}); a second probe checks that the plans
 * differ in nothing else.
 */
const START_PROBES = ['2026-01-01T10:01:00Z', '2026-01-01T10:02:00Z'];

/**
 * Every selection the Plan page offers: each combination of the selectable names, everything, and
 * changed models only, with and without missing upstream, and from a start time where the CLI
 * allows one (with --select).
 */
const SELECTIONS = [
	...Array.from({ length: 2 ** SELECTABLE.length }, (_, mask) =>
		SELECTABLE.filter((_, index) => mask & (1 << index))
	).flatMap((select) =>
		[false, true].flatMap((includeMissingUpstream) =>
			(select.length === 0 ? [null] : [null, START_PROBES[0]]).map((start) => ({
				select,
				changed: false,
				includeMissingUpstream,
				start
			}))
		)
	),
	{ select: [], changed: true, includeMissingUpstream: false, start: null }
];

/** How the demo looks a selection up; ui/src/lib/demo/_helpers/selection-key.ts builds the same key. */
function selectionKey({ select, changed, includeMissingUpstream, start }) {
	return [
		changed ? 'changed' : [...select].sort().join(' '),
		includeMissingUpstream ? 'upstream' : '',
		start === null ? '' : 'from'
	].join('|');
}

/** The query string the Plan page sends for a selection. */
function planQuery({ select, changed, includeMissingUpstream, start }, extra = {}) {
	const params = new URLSearchParams();
	for (const selector of select) params.append('select', selector);
	if (changed) params.set('changed', 'true');
	if (includeMissingUpstream) params.set('include_missing_upstream', 'true');
	if (start !== null) params.set('start', start);
	for (const [name, value] of Object.entries(extra)) params.set(name, value);
	return params.toString();
}

// ── processes and ports ─────────────────────────────────────────────────────────────────────────

function resolveBrowser() {
	const candidates = [
		process.env.CHROME_PATH,
		chromium.executablePath(),
		'/usr/bin/chromium',
		'/usr/bin/google-chrome',
		'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
	];
	const found = candidates.find((candidate) => candidate && existsSync(candidate));
	if (!found) throw new Error('Set CHROME_PATH to an explicit browser binary.');
	return found;
}

/** Runs sqt without blocking, because the capture proxy answers its ClickHouse requests here. */
async function sqt(project, ...args) {
	console.log(`$ sqt ${args.join(' ')}`);
	const child = spawn(SQT, [...args, '--project-dir', project], { stdio: ['ignore', 'ignore', 'inherit'] });
	const status = await new Promise((resolve) => child.on('close', resolve));
	if (status !== 0) throw new Error(`sqt ${args.join(' ')} exited with status ${status}`);
}

/** A loopback port nothing is listening on. */
async function freePort() {
	const probe = createServer();
	await new Promise((resolve) => probe.listen(0, '127.0.0.1', resolve));
	const { port } = probe.address();
	await new Promise((resolve) => probe.close(resolve));
	return port;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForServer(origin, server) {
	for (let attempt = 0; attempt < 120; attempt += 1) {
		if (server.exitCode !== null) throw new Error(`sqt dev exited with status ${server.exitCode}`);
		try {
			if ((await fetch(`${origin}/api/status`)).ok) return;
		} catch {
			// not listening yet
		}
		await sleep(500);
	}
	throw new Error('sqt dev did not start');
}

// ── statements ──────────────────────────────────────────────────────────────────────────────────

const WRITE = /^\s*(CREATE|DROP|INSERT|ALTER|RENAME|EXCHANGE|TRUNCATE|OPTIMIZE)\b/i;

/** The statement as the browser engine runs it. */
function adapt(sql) {
	const view = sql.match(/^\s*CREATE\s+OR\s+REPLACE\s+VIEW\s+([^\s(]+)([\s\S]*)$/i);
	if (view) return [`DROP VIEW IF EXISTS ${view[1]}`, `CREATE VIEW ${view[1]}${view[2]}`];
	const exchange = sql.match(/^\s*EXCHANGE\s+TABLES\s+(\S+)\s+AND\s+([^\s;]+)\s*;?\s*$/i);
	if (exchange) {
		const [, left, right] = exchange;
		const spare = left.endsWith('`') ? `${left.slice(0, -1)}__exchange_spare\`` : `${left}__exchange_spare`;
		return [`RENAME TABLE ${left} TO ${spare}, ${right} TO ${left}, ${spare} TO ${right}`];
	}
	const drop = sql.match(/^(\s*DROP\s+(?:TABLE|VIEW|DICTIONARY)[\s\S]*?)\s+SYNC\s*;?\s*$/i);
	if (drop) return [drop[1]];
	return [sql.trim()];
}

function decode(body, encoding) {
	if (!encoding || encoding === 'identity') return body;
	if (encoding === 'gzip') return gunzipSync(body);
	if (encoding === 'deflate') return inflateSync(body);
	throw new Error(`Cannot read a ClickHouse request body encoded as ${encoding}`);
}

/**
 * Forwards sqt's ClickHouse HTTP requests unchanged. While an action is open it keeps the
 * statements that change state and that ClickHouse accepted, in the order it answered them; a
 * write outside an action is an error, because the demo would never run it.
 */
async function startCaptureProxy(target) {
	const capture = { statements: null, error: null };
	const server = createHttpServer(async (incoming, outgoing) => {
		const chunks = [];
		for await (const chunk of incoming) chunks.push(chunk);
		const body = Buffer.concat(chunks);
		const statements = [];
		const url = new URL(incoming.url, 'http://clickhouse');
		try {
			const text = decode(body, incoming.headers['content-encoding']).toString('utf8');
			const sql = [url.searchParams.get('query'), text].filter(Boolean).join('\n');
			if (WRITE.test(sql)) {
				if (capture.statements === null) throw new Error(`sqt wrote outside an action: ${sql.slice(0, 200)}`);
				if ([...url.searchParams.keys()].some((name) => name.startsWith('param_'))) {
					throw new Error(`sqt sent a bound parameter, which the engine cannot replay: ${url}`);
				}
				statements.push(...adapt(sql));
			}
		} catch (error) {
			capture.error ??= error;
		}
		const open = capture.statements;
		const forwarded = httpRequest(
			{ ...target, path: incoming.url, method: incoming.method, headers: incoming.headers },
			(response) => {
				// Keep only what ClickHouse accepted; a refused statement would fail every replay.
				if (response.statusCode === 200) open?.push(...statements);
				outgoing.writeHead(response.statusCode, response.headers);
				response.pipe(outgoing);
			}
		);
		forwarded.on('error', (error) => outgoing.destroy(error));
		forwarded.end(body);
	});
	const port = await freePort();
	await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve));
	return { capture, port, close: () => server.close() };
}

// ── slots ───────────────────────────────────────────────────────────────────────────────────────

const DEPLOYMENT_ID = /(?<![0-9A-Za-z])(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z_([0-9a-f]{6})(?![0-9a-f])/g;
const UUID = /(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])/g;
const TIMESTAMP = /(?<!\d)(\d{4}-\d{2}-\d{2})([ T])(\d{2}:\d{2}:\d{2})(\.\d{1,6})?(?!\d)/g;
/** sqt's version, which the demo fills in from preparedWith, so a release is not drift. */
const VERSION_TEXT = execFileSync(SQT, ['--version'], { encoding: 'utf8' }).trim().replace(/^sqt /, '');
const VERSION = new RegExp(`(?<![\\d.])${VERSION_TEXT.replace(/\./g, '\\.')}(?![\\d.])`, 'g');
/** Timestamps this long before the first action are part of the example data, not of a run. */
const DATA_SLACK_MS = 86_400_000;

/** The deployment each role creates, by the name the demo binds it to. */
const CREATED_DEPLOYMENT = {
	publish1: 'v1',
	publish2: 'v2',
	build: 'built',
	extra: 'extra',
	plan: 'planned'
};

const deploymentTime = (match) =>
	Date.UTC(+match[1], +match[2] - 1, +match[3], +match[4], +match[5], +match[6]);

/**
 * Replaces the values that differ on every run with slots. `scope` holds what earlier roles bound
 * (deployment IDs and UUIDs by value) and when each role ran; `role` is the role this text belongs
 * to, which binds new deployment IDs and UUIDs.
 */
function slot(text, scope, role) {
	const local = scope.local;
	let slotted = text.replace(VERSION, '{{v}}').replace(DEPLOYMENT_ID, (...match) => {
		const value = match[0];
		let name = scope.deployments.get(value);
		if (name === undefined) {
			name = CREATED_DEPLOYMENT[role.name];
			if (name === undefined) throw new Error(`${role.name} created deployment ${value}, which has no name`);
			if ([...scope.deployments.values()].includes(name) && !local.created.has(value)) {
				throw new Error(`${role.name} created a second deployment, ${value}`);
			}
			scope.deployments.set(value, name);
			local.created.set(value, { name, offset: deploymentTime(match) - role.start });
		}
		return `{{d:${name}}}`;
	});
	slotted = slotted.replace(UUID, (value) => {
		const known = scope.uuids.get(value);
		if (known !== undefined) return `{{u:${known}}}`;
		const name = `${role.name}.${local.uuids.size}`;
		local.uuids.set(value, name);
		scope.uuids.set(value, name);
		return `{{u:${name}}}`;
	});
	return slotted.replace(TIMESTAMP, (whole, day, separator, time, fraction = '') => {
		const at = Date.parse(`${day}T${time}${fraction}Z`);
		const format = `${separator === 'T' ? 'T' : 's'}${fraction.length === 0 ? 0 : fraction.length - 1}`;
		if (role.startTime !== undefined && at === Date.parse(role.startTime)) return `{{s:${format}}}`;
		if (at < scope.firstStart - DATA_SLACK_MS) return whole;
		if (fraction === '' || /^\.0+$/.test(fraction)) {
			for (const [value, name] of scope.deployments) {
				const match = [...value.matchAll(DEPLOYMENT_ID)][0];
				if (deploymentTime(match) === at) return `{{dt:${name}:${format}}}`;
			}
		}
		const owner = [...scope.roles]
			.reverse()
			.find((candidate) => at >= candidate.start - 1500 && at <= candidate.end + 1500);
		if (owner === undefined) throw new Error(`${role.name}: no action ran at ${whole}`);
		// The k-th time the role wrote; the action keeps how long after the role's start it was.
		if (!scope.times.has(owner.name)) scope.times.set(owner.name, new Map());
		const ordinals = scope.times.get(owner.name);
		const offset = at - owner.start;
		if (!ordinals.has(offset)) {
			ordinals.set(offset, ordinals.size);
			local.times.push([owner.name, ordinals.get(offset), offset]);
		}
		return `{{t:${owner.name}:${ordinals.get(offset)}:${format}}}`;
	});
}

/** Slots each string in a JSON value. */
function slotJson(value, scope, role) {
	return JSON.parse(slot(JSON.stringify(value), scope, role));
}

/**
 * A prepared value without what differs on every run, so drift is judged on the rest: which run
 * each time belongs to and when it fell, durations and timings, fingerprints and publication IDs
 * that hash them, and the random name of the mutation lock's release table.
 */
function stripOffsets(value) {
	return JSON.parse(
		JSON.stringify(value, (key, item) => (key === 'times' ? [] : item))
			// A run's time can fall on a deployment's second exactly and become {{dt}} instead.
			.replace(/\{\{(t:[^:}]+:\d+|dt:[^:}]+):([sT]\d)\}\}/g, '{{time:$2}}')
			.replace(/(offset|[a-zA-Z]+Ms|_ms)(\\*"|\\*')?(\s*[:=]\s*)-?\d+/g, '$1$2$3 0')
			.replace(/(\{\{time:s3\}\}', )\d+/g, '$1 0')
			.replace(/[0-9a-f]{64}/g, 'H')
			.replace(/\b\d{19,20}_H/g, 'P_H')
			.replace(/_release_[0-9a-f]{16}/g, '_release_R')
	);
}

function childScope(scope, roleName = null) {
	const times = new Map([...scope.times].map(([name, ordinals]) => [name, new Map(ordinals)]));
	if (roleName !== null) times.set(roleName, new Map());
	return {
		deployments: new Map(scope.deployments),
		uuids: new Map(scope.uuids),
		times,
		roles: [...scope.roles],
		firstStart: scope.firstStart,
		local: newLocal()
	};
}

const newLocal = () => ({ created: new Map(), uuids: new Map(), times: [] });

/** What an action or step binds when it runs: the deployments it creates and when its times fall. */
const bindings = (local) => ({ creates: [...local.created.values()], times: local.times });

// ── the playground ──────────────────────────────────────────────────────────────────────────────

const workspace = mkdtempSync(join(tmpdir(), 'sqltide-ui-demo-'));
// Named as `sqt playground` names a visitor's project, which sqt records as its identity. Docker
// gets a name of its own, so a playground already running here is left alone.
const project = join(workspace, 'sqltide-playground');
process.env.COMPOSE_PROJECT_NAME = `sqltide-demo-${process.pid}`;
console.log(`$ sqt playground ${project} --no-start`);
execFileSync(SQT, ['playground', project, '--no-start'], { stdio: 'inherit' });
const clickhousePort = await freePort();
const composePath = join(project, 'compose.yaml');
writeFileSync(composePath, readFileSync(composePath, 'utf8').replace(/127\.0\.0\.1:\d+:8123/, `127.0.0.1:${clickhousePort}:8123`));
const settingsPath = join(project, 'sqltide_project.toml');
const settings = readFileSync(settingsPath, 'utf8').replace(/^port = \d+/m, `port = ${clickhousePort}`);
const database = settings.match(/^database = "([^"]+)"/m)[1];
const credentials = {
	user: settings.match(/^username = "([^"]+)"/m)[1],
	password: settings.match(/^password = "([^"]+)"/m)[1]
};
writeFileSync(settingsPath, settings);
execFileSync(SQT, ['playground', 'up', '--project-dir', project], { stdio: 'inherit' });

/** Runs SQL on the playground's ClickHouse directly, not through the proxy. */
async function clickhouse(sql) {
	const response = await fetch(`http://127.0.0.1:${clickhousePort}/`, {
		method: 'POST',
		headers: {
			'X-ClickHouse-User': credentials.user,
			'X-ClickHouse-Key': credentials.password
		},
		body: sql
	});
	const text = await response.text();
	if (!response.ok) throw new Error(`ClickHouse refused: ${sql.slice(0, 200)}\n${text}`);
	return text;
}

const proxy = await startCaptureProxy({ host: '127.0.0.1', port: clickhousePort });
writeFileSync(settingsPath, settings.replace(/^port = \d+/m, `port = ${proxy.port}`));

/** Runs `work` as one role and returns the statements it wrote, as ClickHouse ran them. */
async function capture(name, work, startTime = null) {
	proxy.capture.statements = [];
	const role = { name, start: Date.now(), end: 0, ...(startTime === null ? {} : { startTime }) };
	let result;
	try {
		result = await work();
	} finally {
		role.end = Date.now();
		const statements = proxy.capture.statements;
		proxy.capture.statements = null;
		if (proxy.capture.error) throw proxy.capture.error;
		role.statements = statements;
	}
	return { role, result };
}

/** Puts ClickHouse back in a state by dropping the database and replaying how it got there. */
let current = null;
async function reset(state) {
	if (current === state) return;
	// One clickhouse-client call replays hundreds of statements far faster than one request each.
	const script = [`DROP DATABASE IF EXISTS ${database} SYNC`, ...state.concrete]
		.map((statement) => `${statement.replace(/;\s*$/, '')};\n`)
		.join('');
	execFileSync(
		'docker',
		['compose', '--project-directory', project, 'exec', '-T', 'clickhouse', 'clickhouse-client',
			'--user', credentials.user, '--password', credentials.password, '--multiquery'],
		{ input: script, stdio: ['pipe', 'ignore', 'inherit'] }
	);
	current = state;
}

let server;
let origin;
async function api(path, init) {
	const response = await fetch(origin + path, init);
	return { status: response.status, body: await response.json().catch(() => null) };
}

const seed = readFileSync(join(project, 'seed.sql'), 'utf8')
	.split(';')
	.map((statement) => statement.trim())
	.filter(Boolean);

try {
	// The story's history, which the demo replays when the page loads.
	await clickhouse(`DROP DATABASE IF EXISTS ${database} SYNC`);
	const scope = {
		deployments: new Map(),
		uuids: new Map(),
		roles: [],
		times: new Map(),
		firstStart: Date.now(),
		local: newLocal()
	};
	const setup = [];
	const concrete = [];
	async function setupStep(name, title, work) {
		const { role } = await capture(name, work);
		scope.roles.push(role);
		scope.local = newLocal();
		scope.times.set(name, new Map());
		setup.push({
			role: name,
			title,
			statements: role.statements.map((statement) => slot(statement, scope, role)),
			...bindings(scope.local)
		});
		concrete.push(...role.statements);
	}
	await setupStep('seed', 'Create the orders source', async () => {
		for (const statement of [`CREATE DATABASE IF NOT EXISTS ${database}`, ...seed]) {
			proxy.capture.statements.push(statement);
			await clickhouse(statement);
		}
	});
	await setupStep('publish1', 'Publish the orders pipeline', async () => {
		await sqt(project, 'build', '--auto-approve');
		await sqt(project, 'deployment', 'promote', '--latest');
	});
	await setupStep('publish2', 'Publish large_orders', async () => {
		writeFileSync(join(project, 'pipelines/pl__orders/large_orders.sql'), LARGE_ORDERS_SQL);
		await sqt(project, 'build', '--auto-approve');
		await sqt(project, 'deployment', 'promote', '--latest');
	});
	// The change the story stages, written but not built yet.
	writeFileSync(
		join(project, 'pipelines/pl__orders/order_report.sql'),
		readFileSync(join(project, 'revisions/completed_orders.sql'))
	);

	origin = `http://127.0.0.1:${await freePort()}`;
	server = spawn(SQT, ['dev', '--project-dir', project, '--ui-port', new URL(origin).port], {
		stdio: ['ignore', 'ignore', 'inherit']
	});
	await waitForServer(origin, server);

	/**
	 * States the demo prepares everything for. Each has the ClickHouse statements that lead to it,
	 * the scope its slots resolve in, and the role names its chain actions take.
	 */
	const prepared = {};
	const visited = {};
	const start = { name: 'nothing-staged', concrete: [...concrete], scope: childScope(scope) };
	start.scope.local = null;
	const queue = [start];
	current = start;
	const chainRoles = { 'nothing-staged': 'build', staged: 'promote', promoted: 'rollback', 'rolled-back': 'rollforward' };

	/** Which story state an action leads to from `state`, or null for one nothing is prepared in. */
	function chainNext(state, kind, target, group) {
		const stage = state.name.split('/')[0];
		const suffix = state.name.split('/')[1];
		if (stage === 'nothing-staged' && kind === 'build') return `staged/${group}`;
		if (stage === 'staged' && kind === 'promote' && target === 'built') return `promoted/${suffix}`;
		if (stage === 'promoted' && kind === 'rollback' && target === 'v2') return `rolled-back/${suffix}`;
		if (stage === 'rolled-back' && kind === 'rollback' && target === 'built') return `promoted/${suffix}`;
		return null;
	}

	while (queue.length > 0) {
		const state = queue.shift();
		if (prepared[state.name]) continue;
		visited[state.name] = state;
		console.log(`\n== ${state.name}`);
		await reset(state);
		const entry = { plans: {}, actions: {} };
		prepared[state.name] = entry;

		// Plans: every selection, with and without exact replay counts.
		const plans = [];
		async function preparePlan(selection, counts, start = selection.start) {
			const role = { name: 'plan', start: Date.now(), end: 0, ...(start === null ? {} : { startTime: start }) };
			const response = await api(`/api/plan?${planQuery({ ...selection, start }, counts ? { counts: 'true' } : {})}`);
			role.end = Date.now();
			const planScope = childScope(state.scope, role.name);
			planScope.roles.push(role);
			const slotted = slotJson(response, planScope, role);
			return { ...slotted, ...bindings(planScope.local), raw: response };
		}
		for (const selection of SELECTIONS) {
			for (const counts of [false, true]) {
				const { raw, ...plan } = await preparePlan(selection, counts);
				entry.plans[`${selectionKey(selection)}|${counts ? 'counts' : ''}`] = plan;
				if (!counts) plans.push({ selection, response: raw });
				if (selection.start !== null && !counts) {
					const { raw: _, ...probe } = await preparePlan(selection, counts, START_PROBES[1]);
					const [left, right] = [plan, probe].map((value) => JSON.stringify(stripOffsets(value)));
					if (left !== right) {
						let at = 0;
						while (left[at] === right[at]) at += 1;
						throw new Error(
							`The plan for ${selectionKey(selection)} depends on the start time beyond the time itself:\n` +
								`  ${left.slice(at - 120, at + 120)}\n  ${right.slice(at - 120, at + 120)}`
						);
					}
				}
			}
		}

		// The deployments a visitor can promote or roll back to here.
		const { body: listing } = await api('/api/deployments');
		const actions = [];
		for (const { selection, response } of plans) {
			if (response.status !== 200) {
				console.log(`   plan ${selectionKey(selection)}: ${response.status} ${JSON.stringify(response.body)}`);
				continue;
			}
			if (process.env.PREPARE_DEBUG) console.log(`   plan ${selectionKey(selection)}: ${JSON.stringify(response.body).slice(0, 1500)}`);
			const group = response.body.phases.flatMap((phase) => phase.modelNames).sort().join('+') || 'no-models';
			actions.push({ kind: 'build', key: `build ${selectionKey(selection)}`, selection, group });
		}
		for (const deployment of listing.deployments) {
			const name = state.scope.deployments.get(deployment.deploymentId);
			if (deployment.state === 'staged') actions.push({ kind: 'promote', key: `promote ${name}`, deployment, target: name });
			if (deployment.state === 'superseded' && deployment.missingRelationNames.length === 0) {
				actions.push({ kind: 'rollback', key: `rollback ${name}`, deployment, target: name });
			}
		}

		for (const action of actions) {
			const next = chainNext(state, action.kind, action.target, action.group);
			const roleName = next === null ? 'extra' : chainRoles[state.name.split('/')[0]];
			await reset(state);
			const { role, result } = await capture(roleName, async () => {
				if (action.kind === 'build') {
					// As the Plan page does: plan, then execute that plan's deployment.
					const { selection } = action;
					const planned = await api(`/api/plan?${planQuery(selection)}`);
					const started = await api('/api/build', {
						method: 'POST',
						headers: { 'content-type': 'application/json' },
						body: JSON.stringify({
							selectors: selection.select,
							...(selection.changed ? { changed: true } : {}),
							...(selection.includeMissingUpstream ? { includeMissingUpstream: true } : {}),
							startTime: selection.start,
							deploymentId: planned.body.deploymentId,
							confirmations: []
						})
					});
					if (started.status !== 200) throw new Error(`build ${action.key}: ${JSON.stringify(started)}`);
					for (let attempt = 0; ; attempt += 1) {
						const feed = await api('/api/build/current?after=0');
						if (!feed.body.running && feed.body.exitCode !== null) {
							if (feed.body.exitCode !== 0) throw new Error(`build ${action.key} failed: ${feed.body.stderr}`);
							break;
						}
						if (attempt === 600) throw new Error(`build ${action.key} did not finish`);
						await sleep(250);
					}
					return started;
				}
				const id = encodeURIComponent(action.deployment.deploymentId);
				const response = await api(`/api/deployments/${id}/${action.kind}`, { method: 'POST' });
				if (response.status !== 200) throw new Error(`${action.key}: ${JSON.stringify(response)}`);
				return response;
			}, action.selection?.start ?? null);
			current = null;
			console.log(`   ${action.key}: ${role.statements.length} statements -> ${next ?? 'unprepared'}`);
			const actionScope = childScope(state.scope, roleName);
			actionScope.roles.push(role);
			const statements = role.statements.map((statement) => slot(statement, actionScope, role));
			entry.actions[action.key] = {
				role: roleName,
				response: slotJson(result, actionScope, role),
				statements,
				...bindings(actionScope.local),
				next
			};
			if (next !== null && !prepared[next] && !queue.some((queued) => queued.name === next)) {
				actionScope.local = null;
				queue.push({ name: next, concrete: [...state.concrete, ...role.statements], scope: actionScope });
			}
		}
	}

	const version = VERSION_TEXT;
	const preparedDemo = { preparedWith: version, database, start: start.name, setup, states: prepared };
	const written = `${JSON.stringify(pool(preparedDemo), null, '\t')}\n`;
	const actionCount = Object.values(prepared).reduce((total, state) => total + Object.keys(state.actions).length, 0);
	console.log(`prepared ${Object.keys(prepared).length} states and ${actionCount} actions`);
	if (CHECK) {
		// Drift is any change but the version and the slot offsets, which differ on every run.
		const comparable = ({ preparedWith: _, ...rest }) => JSON.stringify(stripOffsets(rest), null, '\t');
		const committed = unpool(JSON.parse(readFileSync(new URL('prepared.json', COMMITTED_DIR), 'utf8')));
		if (comparable(committed) !== comparable(preparedDemo)) {
			const fresh = new URL('prepared.json', `file://${mkdtempSync(join(tmpdir(), 'sqltide-ui-demo-check-'))}/`);
			writeFileSync(fresh, written);
			console.error(
				`src/lib/demo/prepared.json no longer matches what sqt does (fresh copy: ${fresh.pathname}).\n` +
					'Run `node scripts/prepare-demo.mjs` in ui/ and commit the result.'
			);
			process.exitCode = 1;
		} else {
			console.log('src/lib/demo/prepared.json matches what sqt does.');
		}
	} else {
		writeFileSync(new URL('prepared.json', OUTPUT_DIR), written);
		// What browsers that cannot run the engine see: every page in the first staged state.
		const stagedState = Object.keys(prepared).find((name) => name.startsWith('staged/'));
		console.log(`\n== recording every page in ${stagedState}`);
		await reset(visited[stagedState]);
		const browser = await chromium.launch({ executablePath: resolveBrowser(), headless: true });
		const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
		const recorded = await crawl(page, origin);
		await browser.close();
		writeFileSync(
			new URL('recordings.json', OUTPUT_DIR),
			`${JSON.stringify({ recordedWith: version, ...recorded }, null, '\t')}\n`
		);
		console.log(`recorded ${Object.keys(recorded.responses).length} responses from ${recorded.pages.length} pages`);
	}
} finally {
	server?.kill();
	proxy.close();
	execFileSync(SQT, ['playground', 'down', '--volumes', '--project-dir', project], { stdio: 'inherit' });
}

/**
 * prepared.json with each distinct statement written once: steps and actions list indexes into
 * `statements`, because most builds share most of their SQL.
 */
function pool(demo) {
	const statements = [];
	const index = new Map();
	const intern = (statement) => {
		if (!index.has(statement)) {
			index.set(statement, statements.length);
			statements.push(statement);
		}
		return index.get(statement);
	};
	const pooled = (step) => ({ ...step, statements: step.statements.map(intern) });
	return {
		...demo,
		setup: demo.setup.map(pooled),
		states: Object.fromEntries(
			Object.entries(demo.states).map(([name, state]) => [
				name,
				{ ...state, actions: Object.fromEntries(Object.entries(state.actions).map(([key, action]) => [key, pooled(action)])) }
			])
		),
		statements
	};
}

/** The inverse of pool. */
function unpool({ statements, ...demo }) {
	const expanded = (step) => ({ ...step, statements: step.statements.map((at) => statements[at]) });
	return {
		...demo,
		setup: demo.setup.map(expanded),
		states: Object.fromEntries(
			Object.entries(demo.states).map(([name, state]) => [
				name,
				{ ...state, actions: Object.fromEntries(Object.entries(state.actions).map(([key, action]) => [key, expanded(action)])) }
			])
		)
	};
}

/** The pathname and query the UI requested, which is how the demo looks responses up. */
function key(url) {
	const parsed = new URL(url);
	return parsed.pathname + parsed.search;
}

/** Records every page reachable from `start` and each GET /api response the pages ask for. */
async function crawl(page, origin, start = ['/']) {
	const responses = {};
	const record = async (response) => {
		const request = response.request();
		if (request.method() !== 'GET' || !new URL(request.url()).pathname.startsWith('/api/')) return;
		try {
			responses[key(request.url())] = { status: response.status(), body: await response.json() };
		} catch {
			// a response the page abandoned before it finished
		}
	};
	page.on('response', record);
	const pages = [];
	const queue = [...start];
	while (queue.length > 0 && pages.length < MAX_PAGES) {
		const path = queue.shift();
		if (pages.includes(path)) continue;
		pages.push(path);
		await page.goto(origin + path, { waitUntil: 'networkidle' });
		await page.waitForTimeout(1500);
		// Views a page loads on demand: a deployment's Diff tab and a run's executed SQL.
		const views = page.locator('button[data-statement-sequence]');
		for (let index = 0; index < (await views.count()); index += 1) await views.nth(index).click();
		const diff = page.getByRole('button', { name: 'Diff', exact: true });
		if ((await diff.count()) > 0) await diff.click();
		await page.waitForLoadState('networkidle');
		await page.waitForTimeout(500);
		const links = await page.$$eval('a[href^="/"]', (anchors) =>
			anchors.map((anchor) => new URL(anchor.href).pathname + new URL(anchor.href).search)
		);
		for (const link of links) {
			if (SKIPPED_PAGES.some((skipped) => link.startsWith(skipped))) continue;
			if (!pages.includes(link) && !queue.includes(link)) queue.push(link);
		}
	}
	page.off('response', record);
	return { pages, responses };
}
