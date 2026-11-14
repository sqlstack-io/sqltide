import adapter from '@sveltejs/adapter-static';

// The UI demo on the website is served under a path, such as /demo; the package serves the UI at
// the root.
const base = process.env.SQLTIDE_UI_BASE ?? '';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	compilerOptions: {
		// Force runes mode for the project, except for libraries. Can be removed in svelte 6.
		runes: ({ filename }) => (filename.split(/[/\\]/).includes('node_modules') ? undefined : true)
	},
	kit: {
		alias: {
			'$ui-kit': './src/ui-kit'
		},
		paths: { base },
		version: {
			pollInterval: 60_000
		},
		// SPA mode: static build, client-side routing, data fetched from the
		// dev server API at /api. No Node server in production —
		// the built assets are served by the installed package.
		adapter: adapter({
			fallback: 'index.html',
			pages: 'build',
			assets: 'build',
			precompress: false
		})
	}
};

export default config;
