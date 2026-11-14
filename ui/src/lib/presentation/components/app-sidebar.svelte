<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import ServerIcon from '@lucide/svelte/icons/server';
	import SettingsIcon from '@lucide/svelte/icons/settings';
	import UsersIcon from '@lucide/svelte/icons/users';
	import { getApp } from '$lib/api/main/project/get-app';
	import { getProject } from '$lib/api/main/project/get-project';
	import type { Project } from '$lib/domain/types';
	import { SIDEBAR_NAV_GROUPS } from '$lib/presentation/constants';
	import type { SidebarNavItem } from '$lib/presentation/types';
	import { getAuth } from '$lib/auth/main/get-auth';

	const project: Project = getProject();
	const app = getApp();
	const auth = getAuth();

	// Two sections only. There is no scheduler — Runs is recorded CLI invocation
	// history from `_sqltide_invocations`, not orchestration.
	const footerItems: SidebarNavItem[] = $derived([
		{ label: 'Status', href: '/status', icon: ServerIcon },
		{ label: 'Settings', href: '/settings', icon: SettingsIcon },
		...(auth.roles.includes('admin') ? [{ label: 'Users', href: '/admin/users', icon: UsersIcon }] : [])
	]);

	// Mode is per pipeline, so the project line reports the shape of the project
	// rather than claiming one mode for everything in it.
	const virtualPipelineCount: number = $derived(
		project.pipelines.filter((pipeline) => pipeline.mode === 'virtual').length
	);
	const stagedCount: number = $derived(
		app.deployments.filter((deployment) => deployment.state === 'staged').length
	);

	function isActive(href: string): boolean {
		const path: string = page.url.pathname.slice(base.length) || '/';
		if (href === '/') return path === '/';
		return path.startsWith(href);
	}

</script>

<aside
	class="bg-sidebar flex h-full w-14 shrink-0 flex-col border-r border-[var(--sidebar-border)] px-1 py-3 md:w-56 md:px-2.5 md:py-3.5"
>
	<div class="flex items-center justify-center gap-2 px-1 pb-3 md:justify-start md:px-2 md:pb-4">
		<!-- The website's logo files (website/public/assets): two files rather than one
		     recoloured asset, because the cyan mark stays constant while the wordmark
		     inverts. The collapsed sidebar shows the mark alone, as the favicon does. -->
		<img src="{base}/favicon.png" alt="SQLTide" class="h-[18px] w-auto md:hidden" />
		<span class="hidden md:grid">
			<img src="{base}/logo-on-dark.png" alt="SQLTide" class="logo-dark h-[24px] w-auto" />
			<img src="{base}/logo-on-light.png" alt="SQLTide" class="logo-light h-[24px] w-auto" />
			<a
				href="https://sqlstack.io"
				target="_blank"
				rel="noreferrer"
				class="text-[var(--sb-text-faint)] ms-[34%] text-[11px] leading-[14px] whitespace-nowrap no-underline hover:text-[var(--sb-text)] hover:underline"
				>by SQLStack</a
			>
		</span>
		{#if app.status?.toolVersion}
			<span class="text-[var(--sb-text-faint)] ml-auto hidden font-mono text-[10px] tracking-wide md:inline"
				>v{app.status.toolVersion}</span
			>
		{/if}
	</div>

	<div
		class="text-[var(--sb-text-faint)] hidden px-2.5 pb-1.5 pt-2 font-mono text-[10px] uppercase tracking-[0.16em] md:block"
	>
		Project
	</div>
	<div class="mx-1 mb-1 hidden items-center gap-2.5 px-2.5 py-1.5 text-[13px] font-medium md:flex">
		<span class="bg-[var(--sb-secondary)] h-1.5 w-1.5 rounded-[2px]"></span>
		{project.name}
	</div>
	<div class="text-[var(--sb-text-faint)] mx-1 mb-3 hidden px-2.5 font-mono text-[10px] md:block">
		{project.adapter} · {project.pipelines.length} pipeline{project.pipelines.length === 1
			? ''
			: 's'}{virtualPipelineCount > 0 ? ` · ${virtualPipelineCount} virtual` : ''}
	</div>

	<nav class="flex flex-1 flex-col gap-px">
		{#each SIDEBAR_NAV_GROUPS as group (group.section)}
			<div
				class="text-[var(--sb-text-faint)] hidden px-3 pb-1 pt-3.5 font-mono text-[10px] uppercase tracking-[0.16em] md:block"
			>
				{group.section}
			</div>
			{#each group.items as item (item.href)}
				{@const Glyph = item.icon}
				<a
					href={base + item.href}
					title={item.label}
					class="relative flex items-center justify-center gap-2.5 rounded-md px-2 py-2 text-[13px] md:justify-start md:px-3 {isActive(
						item.href
					)
						? 'bg-[var(--sidebar-accent)] text-foreground before:absolute before:-left-2.5 before:top-1.5 before:bottom-1.5 before:w-[3px] before:rounded-r before:bg-primary before:content-[\'\']'
						: 'text-[var(--sidebar-foreground)] hover:bg-[var(--sb-hover)] hover:text-foreground'}"
				>
					<span class="grid w-4 place-items-center opacity-80"><Glyph size={14} /></span>
					<span class="hidden md:inline">{item.label}</span>
					{#if item.href === '/deployments' && stagedCount > 0}
						<span
							class="text-[var(--sb-warning)] ml-auto hidden font-mono text-[10px] md:inline"
							title="{stagedCount} staged deployment{stagedCount === 1 ? '' : 's'}"
							>● {stagedCount}</span
						>
					{/if}
				</a>
			{/each}
		{/each}

		<div class="flex-1"></div>

		{#each footerItems as item (item.href)}
			{@const Glyph = item.icon}
		<a
			href={base + item.href}
			title={item.label}
			class="text-[var(--sidebar-foreground)] hover:bg-[var(--sb-hover)] hover:text-foreground flex items-center justify-center gap-2.5 rounded-md px-2 py-2 text-[13px] md:justify-start md:px-3"
			>
				<span class="grid w-4 place-items-center opacity-80"><Glyph size={14} /></span>
			<span class="hidden md:inline">{item.label}</span>
			</a>
		{/each}
	</nav>

</aside>

<style>
	/* default (light theme): show the dark-ink wordmark */
	.logo-dark {
		display: none;
	}
	.logo-light {
		display: block;
	}
	/* dark theme: show the light-ink wordmark */
	:global(.dark) .logo-dark {
		display: block;
	}
	:global(.dark) .logo-light {
		display: none;
	}
</style>
