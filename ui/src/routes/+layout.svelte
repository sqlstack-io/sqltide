<script lang="ts">
	import './layout.css';
	import { goto } from '$app/navigation';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import { getAuth } from '$lib/auth/main/get-auth';
	import AppSidebar from '$lib/presentation/components/app-sidebar.svelte';
	import AppVersionNotice from '$lib/presentation/components/app-version-notice.svelte';
	import CompileErrorScreen from '$lib/presentation/components/compile-error-screen.svelte';
	import DemoBanner from '$lib/demo/components/demo-banner.svelte';
	import EnginePanel from '$lib/demo/components/engine-panel.svelte';
	import { getDemo } from '$lib/demo/main/get-demo';
	import type { DemoState } from '$lib/demo/types';
	import { getApp } from '$lib/api/main/project/get-app';
	import { initializeBootstrap } from '$lib/api/main/project/initialize-bootstrap';

	let { children } = $props();
	const app = getApp();
	const auth = getAuth();
	const demo: DemoState = getDemo().demo;

	$effect(() => {
		if (auth.phase === 'unauthenticated' && page.route.id !== '/login') {
			void goto(`${base}/login`);
		} else if (auth.phase === 'authenticated' && page.route.id === '/login') {
			void goto(`${base}/`);
		}
	});

	onMount(async () => {
		await initializeBootstrap();
	});
</script>

<svelte:head>
	{#if app.phase !== 'ready'}
		<title>SQLTide</title>
	{/if}
</svelte:head>

{#if auth.phase === 'unauthenticated' && page.route.id !== '/login'}
	<div class="text-muted-foreground grid h-screen place-items-center font-mono text-[13px]">
		redirecting to sign in…
	</div>
{:else if page.route.id === '/login'}
	{@render children()}
{:else if auth.phase === 'error'}
	<div class="grid h-screen place-items-center p-6 text-center">
		<div>
			<div class="font-display text-[17px] font-semibold">Authentication unavailable</div>
			<div class="text-muted-foreground mt-2 max-w-lg font-mono text-[11px]">{auth.error}</div>
		</div>
	</div>
{:else if auth.phase === 'loading'}
	<div class="text-muted-foreground grid h-screen place-items-center font-mono text-[13px]">authenticating…</div>
{:else if app.phase === 'ready'}
	<div class="flex h-screen flex-col overflow-hidden">
		{#if import.meta.env.VITE_SQLTIDE_DEMO === '1'}
			<DemoBanner />
		{/if}
		<div class="flex min-h-0 flex-1 overflow-hidden">
			<AppSidebar />
			<div class="flex min-w-0 flex-1 flex-col">
				{@render children()}
			</div>
			{#if import.meta.env.VITE_SQLTIDE_DEMO === '1' && demo.mode.kind === 'real' && demo.mode.live !== null}
				<EnginePanel live={demo.mode.live} onrestarted={() => goto(`${base}/`, { invalidateAll: true })} />
			{/if}
		</div>
	</div>
{:else if app.phase === 'loading'}
	<!-- The demo's pages wait for its engine, so its progress shows while they load. -->
	{#if import.meta.env.VITE_SQLTIDE_DEMO === '1'}
		<DemoBanner />
	{/if}
	<div class="text-muted-foreground grid h-screen place-items-center font-mono text-[13px]">
		loading project…
	</div>
{:else if app.phase === 'compile_failing'}
	<CompileErrorScreen />
{:else if app.phase === 'unreachable'}
	<div class="text-muted-foreground grid h-screen place-items-center font-mono text-[13px]">
		<div class="flex flex-col items-center gap-2">
			<div>sqt dev is not reachable</div>
			<div class="text-[var(--sb-text-faint)] text-[11px]">{app.fetchError}</div>
		</div>
	</div>
{/if}

<AppVersionNotice />
