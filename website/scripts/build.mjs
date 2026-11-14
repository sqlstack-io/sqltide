import { copyFile, cp, mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { gzipSync } from 'node:zlib';
import { redirects } from '../src/navigation.mjs';

const root = new URL('../', import.meta.url);
const output = new URL('dist/', root);
const review = process.argv.includes('--review');
execFileSync('npm', ['exec', '--', 'astro', 'build'], { cwd: root, stdio: 'inherit' });
// Astro's meta refresh drops queries and fragments. Keep its ordinary fallback.
for (const [from, to] of Object.entries(redirects)) {
  const file = new URL(`${from.replace(/^\//, '').replace(/\/$/, '')}/index.html`, output);
  const redirect = await readFile(file, 'utf8');
  await writeFile(file, redirect.replace('<meta http-equiv=', `<script>location.replace(${JSON.stringify(to)} + location.search + location.hash)</script><meta http-equiv=`));
}

// The UI demo at /demo/: the package's UI, built to answer its API from recorded responses, or, on
// desktop browsers that can run it, from a real ClickHouse engine (chdb-wasm) in the page that runs
// the SQL sqt prepared. Each recorded page gets its own copy of the app shell, so a reload or shared
// link opens that page.
const ui = new URL('../ui/', root);
const chdb = new URL('node_modules/chdb-wasm/', root);
const { version: engineVersion } = JSON.parse(await readFile(new URL('package.json', chdb), 'utf8'));
// The engine's files are cached for good, so their path names the exact bytes: gzip output can
// differ between builds of the same version, and a half-cached old part must never join a new one.
const wasm = await readFile(new URL('dist/chdb.wasm', chdb));
const packed = gzipSync(wasm, { level: 9 });
// The engine's worker hands Emscripten the downloaded wasm as bytes (wasmBinary), and Firefox then
// never finishes starting it; streaming the same blob URL starts in about a second in every browser.
const workerSource = await readFile(new URL('dist/worker.js', chdb), 'utf8');
const wasmAsBytes = `        const bytes = await fetchWithProgress(payload.wasmUrl, id);
        if (bytes)
            moduleOpts.wasmBinary = bytes;`;
if (!workerSource.includes(wasmAsBytes)) throw new Error(`chdb-wasm ${engineVersion}'s worker no longer loads the wasm as bytes; recheck the Firefox start.`);
const worker = workerSource.replace(
  wasmAsBytes,
  `        moduleOpts.locateFile = (path, prefix) => (path.endsWith('.wasm') ? payload.wasmUrl : prefix + path);`,
);
const engineHash = createHash('sha256').update(packed).update(worker).digest('hex').slice(0, 12);
const engineBase = `/demo/engine/${engineVersion}-${engineHash}/`;
if (!existsSync(new URL('node_modules/', ui))) {
  // The UI's lockfile needs the npm it pins, as `make ui-install` uses.
  const { packageManager } = JSON.parse(await readFile(new URL('package.json', ui), 'utf8'));
  execFileSync('npx', ['--yes', packageManager, 'ci'], { cwd: ui, stdio: 'inherit' });
}
execFileSync('npm', ['run', 'build'], {
  cwd: ui,
  stdio: 'inherit',
  env: { ...process.env, SQLTIDE_UI_BASE: '/demo', VITE_SQLTIDE_DEMO: '1', VITE_SQLTIDE_DEMO_ENGINE: engineBase },
});
const demo = new URL('demo/', output);
await cp(new URL('build/', ui), demo, { recursive: true });
// The engine's module files, and its multi-threaded wasm gzipped in two parts: Pages serves files of
// at most 25 MiB, and the page joins and gunzips them.
const engine = new URL(engineBase.slice(1), output);
await mkdir(engine, { recursive: true });
for (const file of ['index.js', 'async.js', 'bindings.js', 'platform.js', 'protocol.js', 'status.js', 'chdb.mjs']) {
  await copyFile(new URL(`dist/${file}`, chdb), new URL(file, engine));
}
await writeFile(new URL('worker.js', engine), worker);
await copyFile(new URL('LICENSE', chdb), new URL('LICENSE', engine));
const half = Math.ceil(packed.length / 2);
const parts = [packed.subarray(0, half), packed.subarray(half)];
for (const [index, part] of parts.entries()) await writeFile(new URL(`chdb.wasm.gz.${index}`, engine), part);
await writeFile(new URL('manifest.json', engine), JSON.stringify({
  version: engineVersion,
  wasmBytes: wasm.length,
  parts: parts.map((part, index) => ({ name: `chdb.wasm.gz.${index}`, bytes: part.length })),
}));
const { pages } = JSON.parse(await readFile(new URL('src/lib/demo/recordings.json', ui), 'utf8'));
for (const path of new Set(pages.map((page) => new URL(page, 'http://demo').pathname))) {
  if (path === '/') continue;
  const directory = new URL(`${path.slice(1)}/`, demo);
  await mkdir(directory, { recursive: true });
  await copyFile(new URL('index.html', demo), new URL('index.html', directory));
}
// Pages the engine creates, such as a new run, have no copy; Pages answers them with the nearest
// 404.html, which is the app shell too.
await copyFile(new URL('index.html', demo), new URL('404.html', demo));

// Keep unselected alternatives out of production. The selected hero lives in the homepage.
const home = await readFile(new URL('index.html', output), 'utf8');
for (const concept of review ? ['sql-plan', 'ingestion-replay'] : []) {
  const showcase = await readFile(new URL(`src/concepts/${concept}.html`, root), 'utf8');
  const directory = new URL(`concepts/${concept}/`, output);
  await mkdir(directory, { recursive: true });
  await writeFile(new URL('index.html', directory), home
    .replace('<link rel="canonical" href="https://sqltide.io/">', '')
    .replace(/<figure class="showcase virtual-showcase"[\s\S]*?<\/figure>/, showcase));
}

if (review) {
  for (const file of await readdir(output, { recursive: true })) {
    if (!file.endsWith('.html')) continue;
    const path = new URL(file, output);
    const html = await readFile(path, 'utf8');
    await writeFile(path, html.replace('</head>', '<meta name="robots" content="noindex, nofollow"></head>'));
  }
  await writeFile(new URL('robots.txt', output), 'User-agent: *\nDisallow: /\n');
}
