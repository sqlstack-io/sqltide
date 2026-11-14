#!/usr/bin/env node
/**
 * Parity check for ui/src/lib/demo-readers: drives the basic playground through a deployment story
 * with the repository's `sqt`, and after every step compares what `sqt dev` returns for the
 * deployment, run and state endpoints with what the TypeScript readers build from the same
 * ClickHouse. Exits non-zero on any difference outside the explicit allowances below.
 *
 *   node ui/scripts/check-demo-readers.mjs
 */
import { spawn } from 'node:child_process';
import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { readDeployment } from '../src/lib/demo-readers/main/read-deployment.ts';
import { readDeploymentDiff } from '../src/lib/demo-readers/main/read-deployment-diff.ts';
import { readDeployments } from '../src/lib/demo-readers/main/read-deployments.ts';
import { readRunEvents } from '../src/lib/demo-readers/main/read-run-events.ts';
import { readRunStatement } from '../src/lib/demo-readers/main/read-run-statement.ts';
import { readRuns } from '../src/lib/demo-readers/main/read-runs.ts';
import { readState } from '../src/lib/demo-readers/main/read-state.ts';

const REPO = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const SQT = join(REPO, '.venv/bin/sqt');
const TEMPLATE_PORT = '18124';

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

/**
 * Differences allowed because the value depends on when it was read, not on warehouse contents.
 * `*` matches one path segment.
 */
const TIME_DEPENDENT = {
	'GET /api/state': [
		'capturedAt', // the warehouse clock at each read
		'warehouseHealth.measuredAt', // equals capturedAt
		'warehouseHealth.uptimeSeconds', // server uptime advances between reads
		'warehouseHealth.collectionDurationMs', // how long the probes took
		'warehouseHealth.activity.activeQueries', // queries in flight at that instant
		'warehouseHealth.activity.activeMerges', // background merges at that instant
		// Background merges between the two reads change part counts and compressed sizes; tables are
		// compared by name (see byName below) because the list is the top 10 by bytes on disk.
		'warehouseHealth.tables.*.activeParts',
		'warehouseHealth.tables.*.bytesOnDisk',
		'models.*.lagSeconds', // age of the newest row relative to now
		'models.*.activity.*', // writes inside the last 120s, a window ending now
		'sources.*.lastArrivalSeconds', // age of the newest event relative to now
		'sources.*.freshness', // derived from that age
		'sources.*.throughput.buckets', // buckets of a window ending now
		'sources.*.rowsPerSecond' // derived from those buckets
	],
	'GET /api/runs': ['*.lastSignalAgeSeconds', '*.durationMs'], // only active runs: ages from now
	'GET /api/runs/{id}/events': [
		'lastSignalAgeSeconds', // only active runs: age from now
		'statementProgress.observedAt',
		'statementProgress.elapsedSeconds',
		'statementProgress.readRowsPerSecond',
		'statementProgress.readBytesPerSecond'
	]
};

/** Warehouse-health fields a browser engine cannot measure; the readers leave them empty without these warnings. */
const BROWSER_WARNINGS = [
	'Disk capacity is unavailable.',
	'Main-path inode metrics are unavailable.',
	'Meaningful memory context is unavailable.'
];
function asBrowserHealth(health) {
	if (health === null) return null;
	return {
		...health,
		availability: 'partial',
		status: 'unknown',
		disks: [],
		inodes: { total: null, free: null, status: 'unknown' },
		memory: null,
		warnings: health.warnings.filter((warning) => !BROWSER_WARNINGS.includes(warning))
	};
}

const TABLE_LIMIT = 10;
/**
 * Key both health table lists by name. Merges can move a table across the top-10 cutoff between the
 * two reads, so when both lists are full, a table only one side lists is dropped and counted.
 */
function byName(expected, actual) {
	const names = (health) => new Set((health?.tables ?? []).map((table) => table.name));
	const full = expected?.tables?.length === TABLE_LIMIT && actual?.tables?.length === TABLE_LIMIT;
	const [left, right] = [names(expected), names(actual)];
	const keep = (name) => !full || (left.has(name) && right.has(name));
	const keyed = (health) =>
		health?.tables === undefined
			? health
			: { ...health, tables: Object.fromEntries(health.tables.filter((table) => keep(table.name)).map((table) => [table.name, table])) };
	const dropped = [...left, ...right].filter((name) => !keep(name)).length;
	const key = 'GET /api/state warehouseHealth.tables membership at the top-10 cutoff';
	if (dropped > 0) report.allowed.set(key, (report.allowed.get(key) ?? 0) + dropped);
	return [keyed(expected), keyed(actual)];
}

function freePort() {
	return new Promise((done, fail) => {
		const server = createServer();
		server.on('error', fail);
		server.listen(0, '127.0.0.1', () => {
			const { port } = server.address();
			server.close(() => done(port));
		});
	});
}

function run(command, args, cwd) {
	return new Promise((done, fail) => {
		const child = spawn(command, args, { cwd, stdio: ['ignore', 'pipe', 'pipe'] });
		let output = '';
		child.stdout.on('data', (chunk) => (output += chunk));
		child.stderr.on('data', (chunk) => (output += chunk));
		child.on('error', fail);
		child.on('close', (code) =>
			code === 0 ? done(output) : fail(new Error(`${command} ${args.join(' ')} exited ${code}\n${output}`))
		);
	});
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

function pathText(path) {
	return path.map((segment) => (typeof segment === 'number' ? `[${segment}]` : `.${segment}`)).join('').replace(/^\./, '');
}

function matches(pattern, path) {
	const parts = pattern.split('.');
	const segments = path.map(String);
	return parts.length === segments.length && parts.every((part, index) => part === '*' || part === segments[index]);
}

/** Every leaf path where `expected` and `actual` differ; object key order is ignored. */
function differences(expected, actual, path = []) {
	if (Array.isArray(expected) && Array.isArray(actual)) {
		const found = [];
		for (let index = 0; index < Math.max(expected.length, actual.length); index += 1) {
			if (index >= expected.length || index >= actual.length) {
				found.push({ path: [...path, index], expected: expected[index], actual: actual[index] });
			} else found.push(...differences(expected[index], actual[index], [...path, index]));
		}
		return found;
	}
	const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
	if (isObject(expected) && isObject(actual)) {
		const keys = [...new Set([...Object.keys(expected), ...Object.keys(actual)])].sort();
		return keys.flatMap((key) =>
			!(key in expected) || !(key in actual)
				? [{ path: [...path, key], expected: key in expected ? expected[key] : '<absent>', actual: key in actual ? actual[key] : '<absent>' }]
				: differences(expected[key], actual[key], [...path, key])
		);
	}
	return Object.is(expected, actual) || JSON.stringify(expected) === JSON.stringify(actual)
		? []
		: [{ path, expected, actual }];
}

const report = { comparisons: 0, failures: [], allowed: new Map() };

function compare(step, label, kind, expected, actual) {
	report.comparisons += 1;
	const allowances = TIME_DEPENDENT[kind] ?? [];
	for (const difference of differences(expected, actual)) {
		const pattern = allowances.find((candidate) => matches(candidate, difference.path));
		if (pattern !== undefined) {
			report.allowed.set(`${kind} ${pattern}`, (report.allowed.get(`${kind} ${pattern}`) ?? 0) + 1);
			continue;
		}
		report.failures.push(
			`[${step}] ${label} :: ${pathText(difference.path) || '<root>'}\n` +
				`    sqt dev : ${JSON.stringify(difference.expected)?.slice(0, 400)}\n` +
				`    readers : ${JSON.stringify(difference.actual)?.slice(0, 400)}`
		);
	}
}

/** Run a reader, turning a thrown refusal into the `{status, detail}` the server answers with. */
async function outcome(read) {
	try {
		return { status: 200, body: await read() };
	} catch (error) {
		return { status: 'error', detail: error instanceof Error ? error.message : String(error) };
	}
}

async function main() {
	const workspace = mkdtempSync(join(tmpdir(), 'sqltide-reader-parity-'));
	const project = join(workspace, 'pg');
	const clickhousePort = String(await freePort());
	const uiPort = await freePort();
	let dev = null;
	let started = false;
	try {
		await run(SQT, ['playground', project, '--no-start'], workspace);
		for (const file of ['compose.yaml', 'sqltide_project.toml']) {
			const path = join(project, file);
			writeFileSync(path, readFileSync(path, 'utf8').replaceAll(TEMPLATE_PORT, clickhousePort));
		}
		const toml = readFileSync(join(project, 'sqltide_project.toml'), 'utf8');
		const setting = (name) => new RegExp(`^${name}\\s*=\\s*"([^"]*)"`, 'm').exec(toml)[1];
		const database = setting('database');
		const authorization = `Basic ${Buffer.from(`${setting('username')}:${setting('password')}`).toString('base64')}`;
		started = true;
		await run(SQT, ['playground', 'up', '--project-dir', project], project);

		const query = async (sql) => {
			const response = await fetch(
				`http://127.0.0.1:${clickhousePort}/?database=${encodeURIComponent(database)}&default_format=JSONEachRow`,
				{ method: 'POST', body: sql, headers: { authorization } }
			);
			const text = await response.text();
			if (!response.ok) throw new Error(`${text.trim()}\n  in: ${sql.slice(0, 300)}`);
			return text.split('\n').filter((line) => line.trim() !== '').map((line) => JSON.parse(line));
		};
		const api = async (path, method = 'GET') => {
			const response = await fetch(`http://127.0.0.1:${uiPort}${path}`, { method });
			const text = await response.text();
			const body = text === '' ? null : JSON.parse(text);
			return response.ok ? { status: 200, body } : { status: response.status, detail: body?.detail ?? text };
		};
		const sqt = (...args) => run(SQT, [...args, '--project-dir', project], project);
		const activeDeployment = async () =>
			JSON.parse(await sqt('deployment', 'list', '--json')).deployments.find((item) => item.state === 'active')
				?.deployment_id;

		async function startDev() {
			dev = spawn(SQT, ['dev', '--project-dir', project, '--ui-port', String(uiPort)], {
				cwd: project,
				stdio: ['ignore', 'pipe', 'pipe']
			});
			let log = '';
			dev.stdout.on('data', (chunk) => (log += chunk));
			dev.stderr.on('data', (chunk) => (log += chunk));
			let last = 'no response';
			for (let attempt = 0; attempt < 120; attempt += 1) {
				await sleep(500);
				if (dev.exitCode !== null) throw new Error(`sqt dev exited ${dev.exitCode}\n${log}`);
				try {
					const state = await api('/api/state');
					if (state.status === 200) return;
					last = JSON.stringify(state).slice(0, 500);
				} catch (error) {
					last = String(error?.cause ?? error); // not listening yet
				}
			}
			throw new Error(`sqt dev did not become ready; last /api/state answer: ${last}\n${log}`);
		}

		async function stopDev() {
			if (dev === null || dev.exitCode !== null) return;
			const exited = new Promise((done) => dev.once('close', done));
			dev.kill('SIGINT');
			const timer = setTimeout(() => dev.kill('SIGKILL'), 10000);
			await exited;
			clearTimeout(timer);
			dev = null;
		}

		async function compareEndpoints(step) {
			await startDev();
			try {
				const deployments = await api('/api/deployments');
				compare(step, 'GET /api/deployments', 'GET /api/deployments', deployments, await outcome(() => readDeployments(query, database)));
				const ids = deployments.body.deployments.map((item) => item.deploymentId);
				for (const [index, id] of [...ids, '20000101T000000Z_missing'].entries()) {
					const detail = await api(`/api/deployments/${id}`);
					const mine = await outcome(() => readDeployment(query, database, id));
					compare(
						step,
						`GET /api/deployments/${id}`,
						'GET /api/deployments/{id}',
						detail.status === 404 ? { status: 404 } : detail,
						mine.status === 200 && mine.body === null ? { status: 404 } : mine
					);
					const against = [null, ...(index + 1 < ids.length ? [ids[index + 1]] : [])];
					for (const other of against) {
						const path = `/api/deployments/${id}/diff${other === null ? '' : `?against=${other}`}`;
						const server = await api(path);
						const readers = await outcome(() => readDeploymentDiff(query, database, id, other));
						compare(
							step,
							`GET ${path}`,
							'GET /api/deployments/{id}/diff',
							server.status === 200 ? server : { status: 'error', detail: server.detail },
							readers
						);
					}
				}
				const runs = await api('/api/runs');
				compare(step, 'GET /api/runs', 'GET /api/runs', runs, await outcome(() => readRuns(query, database)));
				for (const runItem of runs.body) {
					const id = runItem.invocationId;
					const events = await api(`/api/runs/${id}/events?after=0`);
					const sequences = events.body.events.map((event) => event.sequence);
					for (const after of [0, sequences[Math.floor(sequences.length / 2)] ?? 0]) {
						const path = `/api/runs/${id}/events?after=${after}`;
						compare(step, `GET ${path}`, 'GET /api/runs/{id}/events', await api(path), await outcome(() => readRunEvents(query, database, id, after)));
					}
					const statements = [
						...new Set(events.body.events.map((event) => event.statementSequence).filter((value) => typeof value === 'number'))
					].slice(0, 6);
					for (const sequence of [...statements, 99999]) {
						const path = `/api/runs/${id}/statements/${sequence}`;
						compare(step, `GET ${path}`, 'GET /api/runs/{id}/statements/{seq}', await api(path), await outcome(() => readRunStatement(query, database, id, sequence)));
					}
				}
				await api('/api/warehouse/refresh', 'POST');
				const state = await api('/api/state');
				const definitions = await api('/api/definitions');
				const mine = await outcome(() => readState(query, database, state.body, definitions.body));
				if (mine.status !== 200) {
					compare(step, 'GET /api/state', 'GET /api/state', state, mine);
				} else {
					const [serverHealth, readerHealth] = byName(asBrowserHealth(state.body.warehouseHealth), mine.body.warehouseHealth);
					compare(
						step,
						'GET /api/state',
						'GET /api/state',
						{ ...state.body, warehouseHealth: serverHealth },
						{ ...mine.body, warehouseHealth: readerHealth }
					);
				}
				console.log(
					`[${step}] compared ${ids.length} deployments, ${runs.body.length} runs, state; ` +
						`${report.failures.length} unexpected differences so far`
				);
			} finally {
				await stopDev();
			}
		}

		await sqt('build', '--auto-approve');
		await sqt('deployment', 'promote', '--latest');
		await compareEndpoints('1 initial build promoted');

		writeFileSync(join(project, 'pipelines/pl__orders/large_orders.sql'), LARGE_ORDERS_SQL);
		await sqt('build', '--auto-approve');
		await sqt('deployment', 'promote', '--latest');
		const second = await activeDeployment();
		await compareEndpoints('2 large_orders added and promoted');

		copyFileSync(join(project, 'revisions/completed_orders.sql'), join(project, 'pipelines/pl__orders/order_report.sql'));
		await sqt('build', '--auto-approve');
		await sqt('deployment', 'audit', '--latest');
		await compareEndpoints('3 completed-orders change staged and audited');

		await sqt('deployment', 'promote', '--latest');
		await compareEndpoints('4 completed-orders change promoted');

		if (second === undefined) throw new Error('could not read the second build deployment ID');
		await sqt('deployment', 'rollback', second, '--auto-approve');
		await compareEndpoints('5 rolled back to the second build');

		// After the rollback large_orders already matches the active deployment, so this build plans
		// nothing and records an empty deployment that the inventory classifies as physical_missing;
		// `promote --latest` refuses it. Compare that state, then change large_orders and promote a new
		// build. That build is a full one: after a rollback, `build --select large_orders` stops with
		// "Population root 'tbl__order_report' has no feeding view".
		await sqt('build', '--auto-approve', '--select', 'large_orders');
		const refusal = await sqt('deployment', 'promote', '--latest').then(
			() => null,
			(error) => error.message
		);
		if (refusal === null || !refusal.includes('physical_missing, not staged')) {
			throw new Error(`expected promote --latest to refuse the empty deployment, got: ${refusal ?? 'success'}`);
		}
		await compareEndpoints('6 large_orders selected with nothing to rebuild (promote refused)');

		writeFileSync(
			join(project, 'pipelines/pl__orders/large_orders.sql'),
			LARGE_ORDERS_SQL.replace('total_cents >= 1000', 'total_cents >= 2000')
		);
		await sqt('build', '--auto-approve');
		await sqt('deployment', 'promote', '--latest');
		await compareEndpoints('7 changed large_orders built and promoted after the rollback');
	} finally {
		if (dev !== null && dev.exitCode === null) dev.kill('SIGKILL');
		if (started) await run(SQT, ['playground', 'down', '--volumes', '--project-dir', project], project).catch((error) => console.error(error.message));
		rmSync(workspace, { recursive: true, force: true });
	}

	console.log(`\n${report.comparisons} payload comparisons.`);
	console.log('Allowed time-dependent differences that occurred:');
	if (report.allowed.size === 0) console.log('  none');
	for (const [pattern, count] of report.allowed) console.log(`  ${pattern}: ${count}`);
	console.log('Browser-unavailable warehouseHealth fields (expected empty): availability, status, disks, inodes, memory and their warnings.');
	if (report.failures.length > 0) {
		console.error(`\n${report.failures.length} unexpected differences:\n${report.failures.join('\n')}`);
		process.exit(1);
	}
	console.log('\nParity check passed.');
}

await main();
