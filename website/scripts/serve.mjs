// Serves the built site for the tests with the headers public/_headers gives each path on Pages, so the
// UI demo runs cross-origin isolated here too.
//
//   node scripts/serve.mjs dist 4174
import { createReadStream, readFileSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, normalize, sep } from 'node:path';

const [root, port] = [process.argv[2] ?? 'dist', Number(process.argv[3] ?? 4174)];
const types = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.txt': 'text/plain; charset=utf-8',
  '.xml': 'application/xml',
  '.woff2': 'font/woff2',
};

// Each rule is a path pattern, where `*` matches the rest of a path, and the headers under it.
const rules = [];
for (const line of readFileSync(join(root, '_headers'), 'utf8').split('\n')) {
  if (!line.trim() || line.trim().startsWith('#')) continue;
  if (!/^\s/.test(line)) {
    const pattern = new RegExp(`^${line.trim().replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*')}$`);
    rules.push({ pattern, headers: {} });
  } else {
    const [name, ...value] = line.trim().split(':');
    const headers = rules.at(-1).headers;
    // Pages joins values that several rules give one header.
    headers[name] = headers[name] ? `${headers[name]}, ${value.join(':').trim()}` : value.join(':').trim();
  }
}

createServer((request, response) => {
  const url = new URL(request.url, 'http://site');
  try {
    const path = decodeURIComponent(url.pathname);
    let file = normalize(join(root, path));
    if (!file.startsWith(normalize(root) + sep)) return response.writeHead(403).end();
    if (statSync(file).isDirectory()) {
      if (!path.endsWith('/')) return response.writeHead(301, { Location: `${url.pathname}/${url.search}` }).end();
      file = join(file, 'index.html');
    }
    const { size } = statSync(file);
    const headers = { 'Content-Type': types[extname(file)] ?? 'application/octet-stream', 'Content-Length': size };
    for (const rule of rules.filter((rule) => rule.pattern.test(path))) {
      for (const [name, value] of Object.entries(rule.headers)) {
        headers[name] = headers[name] ? `${headers[name]}, ${value}` : value;
      }
    }
    response.writeHead(200, headers);
    if (request.method === 'HEAD') return response.end();
    createReadStream(file).pipe(response);
  } catch {
    // Like Pages, answer with the nearest 404.html above the path, such as the UI demo's app shell.
    const parts = url.pathname.split('/').slice(1, -1);
    for (let depth = parts.length; depth >= 0; depth -= 1) {
      const page = normalize(join(root, ...parts.slice(0, depth), '404.html'));
      if (!page.startsWith(normalize(root) + sep)) break;
      try {
        const body = readFileSync(page);
        const headers = { 'Content-Type': types['.html'] };
        for (const rule of rules.filter((rule) => rule.pattern.test(url.pathname))) Object.assign(headers, rule.headers);
        return response.writeHead(404, headers).end(body);
      } catch {
        // no 404.html at this level
      }
    }
    response.writeHead(404, { 'Content-Type': 'text/plain' }).end('Not found');
  }
}).listen(port, '127.0.0.1');
