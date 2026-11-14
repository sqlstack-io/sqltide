<script lang="ts">
	import { base } from '$app/paths';
	import SunIcon from '@lucide/svelte/icons/sun';
	import MoonIcon from '@lucide/svelte/icons/moon';
	import EyeIcon from '@lucide/svelte/icons/eye';
	import RefreshCwIcon from '@lucide/svelte/icons/refresh-cw';
	import RotateCcwIcon from '@lucide/svelte/icons/rotate-ccw';
	import LogOutIcon from '@lucide/svelte/icons/log-out';
	import WarehouseUnavailableBanner from '$lib/presentation/components/warehouse-unavailable-banner.svelte';
	import { createTheme } from '$lib/presentation/main/_create-theme.svelte';
	import { CAN_EXECUTE_BUILD } from '$lib/api/constants';
	import { getApp } from '$lib/api/main/project/get-app';
	import { getProject } from '$lib/api/main/project/get-project';
	import { refreshLiveState } from '$lib/api/main/project/refresh-live-state';
	import { reloadProject } from '$lib/api/main/project/reload-project';
	import { formatClock } from '$lib/formatting/main/format-clock';
	import type { Project } from '$lib/domain/types';
	import { getAuth } from '$lib/auth/main/get-auth';
	import { logout } from '$lib/auth/main/logout';
	import { can } from '$lib/auth/main/can';

	type Props = {
		title: string;
		/** Overrides the default `<project> / <target>` context. */
		breadcrumb?: string;
		children?: import('svelte').Snippet;
	};
	let { title, breadcrumb, children }: Props = $props();

	const project: Project = getProject();
	const app = getApp();
	const theme = createTheme();
	const auth = getAuth();
	const context = $derived(breadcrumb ?? `${project.name} / ${project.target}`);
	// The warehouse read is a SNAPSHOT, so state when it was taken rather than
	// implying a live feed. An absolute clock is also honest about the fact that
	// the plan a user copies may already be stale by the time they run it.
	// $derived so the 30s poll moves the clock instead of freezing it at mount.
	const snapshotClock = $derived(formatClock(project.capturedAt));
	const connected = $derived(app.status?.warehouseConnected ?? false);
	const reloadAllowed = $derived(can('project.reload'));

	// Polling runs every 30s; this forces a snapshot NOW — same fetch path, so
	// everything on screen updates together.
	let refreshing = $state<boolean>(false);

	async function forceRefresh(): Promise<void> {
		refreshing = true;
		try {
			await refreshLiveState({ force: true });
		} finally {
			refreshing = false;
		}
	}

	async function signOut(): Promise<void> {
		await logout();
		window.location.assign(`${base}/login`);
	}

	async function reloadDefinitions(): Promise<void> {
		await reloadProject();
	}
</script>

<svelte:head>
	<title>{title} · SQLTide</title>
</svelte:head>

<div class="flex h-[54px] shrink-0 items-center gap-2 border-b border-border px-3 sm:gap-3.5 sm:px-[18px]">
	<h1 class="font-display text-[16px] font-semibold">{title}</h1>
	<span class="text-[var(--sb-text-faint)] hidden truncate font-mono text-[12px] md:inline">{context}</span>
	<div class="ml-auto flex items-center gap-2.5">
		{#if !CAN_EXECUTE_BUILD}
			<!-- Tier 1 is read-only. State it once, globally, rather than disabling
			     buttons page by page and leaving people guessing why. -->
			<span
				class="text-muted-foreground flex items-center gap-1.5 rounded-[4px] border border-border px-2 py-1 font-mono text-[10.5px]"
				title="This UI holds a read-only warehouse connection. Rebuilds are previewed and handed off as a command."
			>
				<EyeIcon size={11} /> read-only
			</span>
		{/if}
		<span
			class="text-muted-foreground hidden items-center gap-[7px] font-mono text-[11px] tracking-wide sm:flex"
		>
			{#if connected}
				<span class="conn-tick bg-[var(--sb-secondary)] relative h-[7px] w-[7px] rounded-[2px]"
				></span>
				<b class="text-[var(--sb-secondary)] font-medium">connected</b> · snapshot {snapshotClock}
				<span class="hidden xl:inline">{project.timeZone}</span>
			{:else}
				<span class="relative h-[7px] w-[7px] rounded-[2px] bg-[var(--sb-error)]"></span>
				<b class="font-medium" style:color="var(--sb-error)">no warehouse</b>
			{/if}
		</span>
		<button
			class="text-muted-foreground hover:text-foreground hover:bg-[var(--sb-hover)] flex h-7 items-center gap-1.5 rounded-[4px] border border-border px-2 disabled:opacity-60"
			aria-label="Refresh snapshot"
			title="Reconnect if necessary, then refresh the warehouse snapshot"
			disabled={refreshing}
			onclick={() => void forceRefresh()}
		>
			<span class:animate-spin={refreshing}><RefreshCwIcon size={13} /></span>
			<span class="hidden font-mono text-[10.5px] xl:inline">Refresh snapshot</span>
		</button>
		<button
			class="text-muted-foreground hover:text-foreground hover:bg-[var(--sb-hover)] flex h-7 items-center gap-1.5 rounded-[4px] border border-border px-2 disabled:opacity-60"
			aria-label="Reload definitions"
			title={reloadAllowed
				? 'Reread project files and compile definitions without executing a build'
				: 'Requires the project.reload permission'}
			disabled={app.reloading || !reloadAllowed}
			onclick={() => void reloadDefinitions()}
		>
			<span class:animate-spin={app.reloading}><RotateCcwIcon size={13} /></span>
			<span class="hidden font-mono text-[10.5px] xl:inline">
				{app.reloading ? 'Compiling…' : 'Reload definitions'}
			</span>
		</button>
		{#if children}{@render children()}{/if}
		{#if auth.user}
			<span class="text-muted-foreground hidden font-mono text-[10.5px] lg:inline">{auth.user.username}</span>
		{/if}
		{#if auth.config?.mode === 'password'}
			<button class="text-muted-foreground hover:text-foreground hover:bg-[var(--sb-hover)] grid h-7 w-7 place-items-center rounded-[4px] border border-border" aria-label="Sign out" title="Sign out" onclick={() => void signOut()}><LogOutIcon size={13} /></button>
		{:else if auth.config?.proxyLogoutUrl}
			<a class="text-muted-foreground hover:text-foreground hover:bg-[var(--sb-hover)] grid h-7 w-7 place-items-center rounded-[4px] border border-border" aria-label="Sign out" title="Sign out" href={auth.config.proxyLogoutUrl}><LogOutIcon size={13} /></a>
		{/if}
		<button
			class="text-muted-foreground hover:text-foreground hover:bg-[var(--sb-hover)] grid h-7 w-7 place-items-center rounded-[4px] border border-border"
			aria-label="Toggle theme"
			onclick={() => theme.toggle()}
		>
			{#if theme.value === 'dark'}
				<SunIcon size={14} />
			{:else}
				<MoonIcon size={14} />
			{/if}
		</button>
	</div>
</div>

{#if app.status?.error && app.project}
	<div
		class="shrink-0 border-b px-[18px] py-2 font-mono text-[11px]"
		style:border-color="color-mix(in srgb, var(--sb-error) 35%, var(--border))"
		style:background="color-mix(in srgb, var(--sb-error) 7%, var(--background))"
	>
		<span class="font-semibold" style:color="var(--sb-error)">Definitions reload failed.</span>
		 Showing the last successful compilation. {app.status.error.message}
	</div>
{/if}

{#if app.status && !connected}
	<WarehouseUnavailableBanner
		status={app.status}
		{refreshing}
		onretry={() => void forceRefresh()}
	/>
{/if}

<style>
	.conn-tick::after {
		content: '';
		position: absolute;
		inset: -3px;
		border-radius: 3px;
		border: 1px solid var(--sb-secondary);
		opacity: 0.5;
		animation: connpulse 2.4s ease-out infinite;
	}
	@keyframes connpulse {
		0% {
			transform: scale(0.8);
			opacity: 0.6;
		}
		70% {
			transform: scale(1.5);
			opacity: 0;
		}
		100% {
			opacity: 0;
		}
	}
</style>
