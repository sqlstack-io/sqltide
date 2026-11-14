/**
 * Records what the UI demo at sqltide.io/demo/ replays.
 *
 * Creates a basic `sqt playground` (ClickHouse in Docker), publishes it, adds and publishes
 * large_orders, stages the completed-orders change, then opens every page of `sqt dev` reachable
 * from the sidebar, opens each deployment's Diff tab and each run's executed SQL, and records each
 * GET /api response the UI asks for. Then it promotes the staged deployment from its page and
 * records the pages again, keeping the responses the promotion changed. `sqt dev` runs on a free
 * port of its own, so a server already running for another project is never recorded.
 *
 * sqt reaches ClickHouse through a proxy that keeps the statements each step writes, so the demo's
 * in-browser engine can replay them. The browser engine refuses two statements and hangs on a third,
 * so they are rewritten here: CREATE OR REPLACE VIEW becomes DROP and CREATE, EXCHANGE TABLES
 * becomes a three-way RENAME, and DROP ... SYNC loses SYNC.
 *
 * Writes src/lib/demo/recordings.json and src/lib/demo/engine-script.json. Run it again after each
 * release.
 *
 *   node scripts/record-demo.mjs
 *
 * Environment:
 *   SQT           sqt executable (default: sqt on PATH)
 *   CHROME_PATH   explicit browser binary
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
const OUTPUT = new URL('../src/lib/demo/recordings.json', import.meta.url);
const ENGINE_OUTPUT = new URL('../src/lib/demo/engine-script.json', import.meta.url);
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
	const child = spawn(SQT, [...args, '--project-dir', project], { stdio: 'inherit' });
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

async function waitForServer(origin, server) {
	for (let attempt = 0; attempt < 120; attempt += 1) {
		if (server.exitCode !== null) throw new Error(`sqt dev exited with status ${server.exitCode}`);
		try {
			if ((await fetch(`${origin}/api/status`)).ok) return;
		} catch {
			// not listening yet
		}
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	throw new Error('sqt dev did not start');
}

/** The pathname and query the UI requested, which is how the demo looks responses up. */
function key(url) {
	const parsed = new URL(url);
	return parsed.pathname + parsed.search;
}

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
 * Forwards sqt's ClickHouse HTTP requests unchanged, and while a step is open keeps the statements
 * that change state and that ClickHouse accepted, in the order it answered them.
 */
async function startCaptureProxy(target) {
	const capture = { step: null, error: null };
	const server = createHttpServer(async (incoming, outgoing) => {
		const chunks = [];
		for await (const chunk of incoming) chunks.push(chunk);
		const body = Buffer.concat(chunks);
		const statements = [];
		if (capture.step !== null) {
			const url = new URL(incoming.url, 'http://clickhouse');
			try {
				if ([...url.searchParams.keys()].some((name) => name.startsWith('param_'))) {
					throw new Error(`sqt sent a bound parameter, which the engine cannot replay: ${url}`);
				}
				const text = decode(body, incoming.headers['content-encoding']).toString('utf8');
				const sql = [url.searchParams.get('query'), text].filter(Boolean).join('\n');
				if (WRITE.test(sql)) statements.push(...adapt(sql));
			} catch (error) {
				capture.error ??= error;
			}
		}
		const forwarded = httpRequest(
			{ ...target, path: incoming.url, method: incoming.method, headers: incoming.headers },
			(response) => {
				// Keep only what ClickHouse accepted; a refused statement would fail every replay.
				if (response.statusCode === 200) capture.step?.statements.push(...statements);
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

const workspace = mkdtempSync(join(tmpdir(), 'sqltide-ui-demo-'));
const project = join(workspace, 'sqltide-playground');
console.log(`$ sqt playground ${project}`);
execFileSync(SQT, ['playground', project], { stdio: 'inherit' });
const settingsPath = join(project, 'sqltide_project.toml');
const settings = readFileSync(settingsPath, 'utf8');
const clickhouse = {
	host: settings.match(/^host = "([^"]+)"/m)[1],
	port: Number(settings.match(/^port = (\d+)/m)[1])
};
const database = settings.match(/^database = "([^"]+)"/m)[1];
const proxy = await startCaptureProxy(clickhouse);
writeFileSync(settingsPath, settings.replace(/^port = \d+/m, `port = ${proxy.port}`));
const seed = readFileSync(join(project, 'seed.sql'), 'utf8')
	.split(';')
	.map((statement) => statement.trim())
	.filter(Boolean);
const steps = [
	{ title: 'Create the orders source', statements: [`CREATE DATABASE IF NOT EXISTS ${database}`, ...seed] }
];
async function step(title, run) {
	proxy.capture.step = { title, statements: [] };
	await run();
	if (proxy.capture.error) throw proxy.capture.error;
	steps.push(proxy.capture.step);
	proxy.capture.step = null;
}
let server;
try {
	await step('Publish the orders pipeline', async () => {
		await sqt(project, 'build', '--auto-approve');
		await sqt(project, 'deployment', 'promote', '--latest');
	});
	await step('Publish large_orders', async () => {
		writeFileSync(join(project, 'pipelines/pl__orders/large_orders.sql'), LARGE_ORDERS_SQL);
		await sqt(project, 'build', '--auto-approve');
		await sqt(project, 'deployment', 'promote', '--latest');
	});
	await step('Stage the completed-orders change', async () => {
		writeFileSync(
			join(project, 'pipelines/pl__orders/order_report.sql'),
			readFileSync(join(project, 'revisions/completed_orders.sql'))
		);
		await sqt(project, 'build', '--auto-approve');
		await sqt(project, 'deployment', 'audit', '--latest');
	});

	const origin = `http://127.0.0.1:${await freePort()}`;
	server = spawn(SQT, ['dev', '--project-dir', project, '--ui-port', new URL(origin).port], {
		stdio: ['ignore', 'ignore', 'inherit']
	});
	await waitForServer(origin, server);

	const browser = await chromium.launch({ executablePath: resolveBrowser(), headless: true });
	const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
	const staged = await crawl(page, origin);

	// Promote the staged deployment the way a visitor does, and keep what the promotion wrote.
	const { deployments } = staged.responses['/api/deployments'].body;
	const { deploymentId } = deployments.find((deployment) => deployment.state === 'staged');
	await page.goto(`${origin}/deployments/${deploymentId}`, { waitUntil: 'networkidle' });
	proxy.capture.step = { title: 'Promote', statements: [] };
	const [promoted] = await Promise.all([
		page.waitForResponse((response) => response.url().endsWith(`/api/deployments/${deploymentId}/promote`)),
		page.getByRole('button', { name: 'Promote', exact: true }).click()
	]);
	const promotion = { deploymentId, response: { status: promoted.status(), body: await promoted.json() } };
	for (let attempt = 0; ; attempt += 1) {
		const { deployments: now } = await (await fetch(`${origin}/api/deployments`)).json();
		if (now.find((deployment) => deployment.deploymentId === deploymentId).state === 'active') break;
		if (attempt === 120) throw new Error(`${deploymentId} did not become active`);
		await new Promise((resolve) => setTimeout(resolve, 500));
	}
	if (proxy.capture.error) throw proxy.capture.error;
	const promoteStatements = proxy.capture.step.statements;
	proxy.capture.step = null;
	// The UI opens the promotion's run when it finishes, so that page comes first.
	const after = await crawl(page, origin, [`/runs/${promotion.response.body.invocationId}`, '/']);
	await browser.close();

	// After the promotion the demo answers from these first, so keep only what changed.
	promotion.pages = after.pages.filter((path) => !staged.pages.includes(path));
	promotion.responses = Object.fromEntries(
		Object.entries(after.responses).filter(
			([path, response]) => JSON.stringify(response) !== JSON.stringify(staged.responses[path])
		)
	);
	const version = execFileSync(SQT, ['--version'], { encoding: 'utf8' }).trim();
	writeFileSync(
		OUTPUT,
		`${JSON.stringify({ recordedWith: version, ...staged, promotion }, null, '\t')}\n`
	);
	writeFileSync(
		ENGINE_OUTPUT,
		`${JSON.stringify({ recordedWith: version, database, steps, promote: promoteStatements }, null, '\t')}\n`
	);
	console.log(
		`recorded ${Object.keys(staged.responses).length} responses from ${staged.pages.length} pages, ` +
			`${Object.keys(promotion.responses).length} changed by the promotion, and ` +
			`${steps.reduce((total, { statements }) => total + statements.length, promoteStatements.length)} statements`
	);
} finally {
	server?.kill();
	proxy.close();
	writeFileSync(settingsPath, settings);
	execFileSync(SQT, ['playground', 'down', '--volumes', '--project-dir', project], {
		stdio: 'inherit'
	});
}
