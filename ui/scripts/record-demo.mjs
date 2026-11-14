/**
 * Records the API responses the UI demo at sqltide.io/demo/ replays.
 *
 * Creates a basic `sqt playground` (ClickHouse in Docker), publishes it, adds and publishes
 * large_orders, stages the completed-orders change, then opens every page of `sqt dev` reachable
 * from the sidebar, opens each deployment's Diff tab and each run's executed SQL, and records each
 * GET /api response the UI asks for. `sqt dev` runs on a free port of its own, so a server already
 * running for another project is never recorded. Writes src/lib/demo/recordings.json. Run it again
 * after each release.
 *
 *   node scripts/record-demo.mjs
 *
 * Environment:
 *   SQT           sqt executable (default: sqt on PATH)
 *   CHROME_PATH   explicit browser binary
 */
import { execFileSync, spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { createServer } from 'node:net';
import { join } from 'node:path';
import { chromium } from 'playwright-core';

const SQT = process.env.SQT ?? 'sqt';
const OUTPUT = new URL('../src/lib/demo/recordings.json', import.meta.url);
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

function sqt(project, ...args) {
	console.log(`$ sqt ${args.join(' ')}`);
	execFileSync(SQT, [...args, '--project-dir', project], { stdio: 'inherit' });
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

const workspace = mkdtempSync(join(tmpdir(), 'sqltide-ui-demo-'));
const project = join(workspace, 'sqltide-playground');
console.log(`$ sqt playground ${project}`);
execFileSync(SQT, ['playground', project], { stdio: 'inherit' });
let server;
try {
	sqt(project, 'build', '--auto-approve');
	sqt(project, 'deployment', 'promote', '--latest');
	writeFileSync(join(project, 'pipelines/pl__orders/large_orders.sql'), LARGE_ORDERS_SQL);
	sqt(project, 'build', '--auto-approve');
	sqt(project, 'deployment', 'promote', '--latest');
	writeFileSync(
		join(project, 'pipelines/pl__orders/order_report.sql'),
		readFileSync(join(project, 'revisions/completed_orders.sql'))
	);
	sqt(project, 'build', '--auto-approve');
	sqt(project, 'deployment', 'audit', '--latest');

	const origin = `http://127.0.0.1:${await freePort()}`;
	server = spawn(SQT, ['dev', '--project-dir', project, '--ui-port', new URL(origin).port], {
		stdio: ['ignore', 'ignore', 'inherit']
	});
	await waitForServer(origin, server);

	const browser = await chromium.launch({ executablePath: resolveBrowser(), headless: true });
	const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
	const responses = {};
	page.on('response', async (response) => {
		const request = response.request();
		if (request.method() !== 'GET' || !new URL(request.url()).pathname.startsWith('/api/')) return;
		try {
			responses[key(request.url())] = { status: response.status(), body: await response.json() };
		} catch {
			// a response the page abandoned before it finished
		}
	});

	const pages = [];
	const queue = ['/'];
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
	await browser.close();

	const version = execFileSync(SQT, ['--version'], { encoding: 'utf8' }).trim();
	writeFileSync(
		OUTPUT,
		`${JSON.stringify({ recordedWith: version, pages, responses }, null, '\t')}\n`
	);
	console.log(`recorded ${Object.keys(responses).length} responses from ${pages.length} pages`);
} finally {
	server?.kill();
	execFileSync(SQT, ['playground', 'down', '--volumes', '--project-dir', project], {
		stdio: 'inherit'
	});
}
