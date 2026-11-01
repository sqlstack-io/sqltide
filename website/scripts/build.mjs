import { mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
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
