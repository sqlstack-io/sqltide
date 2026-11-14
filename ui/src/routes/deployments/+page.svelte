<script lang="ts">
	import { base } from '$app/paths';
	import { onMount } from 'svelte';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import Button from '$ui-kit/button/button.svelte';
	import ErrorPreview from '$lib/presentation/components/error-preview.svelte';
	import { cleanupDeployments } from '$lib/api/main/deployments/cleanup-deployments';
	import { getApp } from '$lib/api/main/project/get-app';
	import { refreshDeployments } from '$lib/api/main/project/refresh-deployments';
	import { formatBytes } from '$lib/formatting/main/format-bytes';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import type { Deployment, DeploymentState } from '$lib/domain/types';
	import { can } from '$lib/auth/main/can';

	// Deployments are grouped by lifecycle state rather than listed flat: the
	// question is always "what is live, what is waiting, what is dead weight",
	// and a single sorted table answers none of those.
	const SECTIONS: { state: DeploymentState; label: string; hint: string }[] = [
		{ state: 'active', label: 'Active', hint: 'serving the stable views' },
		{ state: 'staged', label: 'Staged', hint: 'built, awaiting promotion' },
		{ state: 'superseded', label: 'Superseded', hint: 'replaced, nothing points here' }
	];

	const PROBLEM_STATES: DeploymentState[] = [
		'incomplete',
		'metadata_missing',
		'physical_missing'
	];

	const app = getApp();
	const deployments: Deployment[] = $derived(app.deployments);
	let refreshing = $state<boolean>(true);
	let showInvalid = $state<boolean>(false);
	const POLL_MS: number = 30_000;

	async function refreshInventory(): Promise<void> {
		if (deployments.length === 0) refreshing = true;
		await refreshDeployments();
		refreshing = false;
	}

	onMount(() => {
		void refreshInventory();
		const timer: ReturnType<typeof setInterval> = setInterval(() => {
			if (!document.hidden) void refreshInventory();
		}, POLL_MS);
		return () => clearInterval(timer);
	});

	function inState(state: DeploymentState): Deployment[] {
		return deployments.filter((deployment) => deployment.state === state);
	}

	const problems: Deployment[] = $derived(
		deployments.filter((deployment) => PROBLEM_STATES.includes(deployment.state))
	);
	const supersededBytes: number = $derived(
		inState('superseded').reduce((total, deployment) => total + deployment.bytes, 0)
	);

	function shortId(deploymentId: string): string {
		return deploymentId.split('_').at(-1) ?? deploymentId;
	}

	let cleaning = $state<boolean>(false);
	let cleanupError = $state<string | null>(null);
	let cleanupSummary = $state<string | null>(null);
	const cleanupAllowed = $derived(can('deployment.cleanup'));

	// Retention matches the janitor's own default; anything newer is protected
	// as a rollback target, so this only ever removes what is already dead.
	const CLEANUP_RETENTION_DAYS: number = 7;

	async function cleanup(): Promise<void> {
		cleaning = true;
		cleanupError = null;
		cleanupSummary = null;
		try {
			const result: Awaited<ReturnType<typeof cleanupDeployments>> = await cleanupDeployments(
				CLEANUP_RETENTION_DAYS
			);
			cleanupSummary =
				result.removedRelations === 0
					? 'nothing was outside the retention window'
					: `removed ${result.removedRelations} relations across ${result.removedDeployments} deployments`;
			await refreshDeployments();
		} catch (error) {
			cleanupError = String(error);
		}
		cleaning = false;
	}
</script>

<AppTopbar title="Deployments">
	{#if problems.length > 0}
		<Button
			variant="outline"
			size="sm"
			class="font-mono text-[10.5px]"
			aria-pressed={showInvalid}
			onclick={() => (showInvalid = !showInvalid)}
		>
			{showInvalid ? 'Hide invalid' : `Show invalid (${problems.length})`}
		</Button>
	{/if}
</AppTopbar>

<div class="min-h-0 flex-1 overflow-y-auto px-[18px] py-4">
	{#if cleanupError !== null}
		<div class="pb-3">
			<ErrorPreview text={cleanupError} title="Cleanup failed" clamp="line-clamp-3" class="max-w-full" />
		</div>
	{:else if cleanupSummary !== null}
		<div class="text-muted-foreground pb-3 text-[12px]">{cleanupSummary}</div>
	{/if}

	{#if refreshing && deployments.length === 0}
		<div class="text-muted-foreground p-6 font-mono text-[12px]">loading deployments…</div>
	{:else}
		{#if inState('active').length === 0}
			<div class="text-muted-foreground mb-3 rounded-[4px] border border-border px-3 py-2.5 text-[12px]">
				<div class="font-mono text-[11.5px] font-medium text-foreground">No active deployments</div>
				{#if deployments.length === 0 || deployments.length === problems.length}
					<div class="pt-1 leading-relaxed">
						Virtual builds create deployments here. Direct builds do not create deployments; find
						them in <a href="{base}/runs" class="text-primary hover:underline">Runs</a>.
					</div>
				{/if}
				{#if problems.length > 0 && !showInvalid}
					<div class="pt-1 font-mono text-[10.5px] text-[var(--sb-text-faint)]">
						{problems.length} invalid {problems.length === 1 ? 'deployment is' : 'deployments are'}
						hidden. Use Show invalid ({problems.length}) above to review.
					</div>
				{/if}
			</div>
		{/if}

		{#each SECTIONS as section (section.state)}
			{@const rows = inState(section.state)}
			{#if rows.length > 0}
				<div class="flex items-baseline gap-2 pb-1.5 pt-4 first:pt-0">
					<span
						class="font-mono text-[10px] uppercase tracking-[0.16em]"
						style:color={section.state === 'staged'
							? 'var(--sb-warning)'
							: section.state === 'active'
								? 'var(--sb-secondary)'
								: 'var(--sb-text-faint)'}>{section.label}</span
					>
					<span class="text-[var(--sb-text-faint)] text-[10.5px]">{section.hint}</span>
					{#if section.state === 'superseded'}
						<span class="text-[var(--sb-text-faint)] ml-auto code text-[10.5px]"
							>{rows.length} · {formatBytes(supersededBytes)} reclaimable</span
						>
						<button
							class="rounded-[3px] border border-border px-2.5 py-1 font-mono text-[10.5px] transition-colors disabled:opacity-50 hover:bg-[var(--sb-hover)]"
							onclick={() => void cleanup()}
							disabled={cleaning || !cleanupAllowed}
							title={cleanupAllowed
								? `Remove deployments outside the ${CLEANUP_RETENTION_DAYS} day retention window`
								: 'Requires the target-scoped deployment.cleanup permission'}
						>
							{cleaning ? 'cleaning…' : 'Clean up'}
						</button>
					{/if}
				</div>

				<table class="sb-list w-full table-fixed text-left" data-testid="deployment-table-{section.state}">
					<colgroup>
						<col class="w-[32%]" />
						<col class="w-[14%]" />
						<col class="w-[14%]" />
						<col class="w-[27%]" />
						<col class="w-[13%]" />
					</colgroup>
					<tbody>
						{#each rows as deployment (deployment.deploymentId)}
							<tr>
								<td class="py-2 pr-3">
									<a
										href="{base}/deployments/{deployment.deploymentId}"
										class="text-primary code text-[12.5px] font-medium hover:underline"
										>{deployment.deploymentId}</a
									>
									<div class="text-[var(--sb-text-faint)] code pt-0.5 text-[10.5px]">
										{deployment.rootNames.join(', ') || 'no recorded roots'}
									</div>
								</td>
								<td class="code px-3 text-[12px] whitespace-nowrap">
									{deployment.modelCount} model{deployment.modelCount === 1 ? '' : 's'}
									<div class="text-[var(--sb-text-faint)] pt-0.5 text-[10.5px]">
										{deployment.relationCount} relations
									</div>
								</td>
								<td class="code px-3 text-[12px] whitespace-nowrap">
									{formatCompact(deployment.rows)} rows
									<div class="text-[var(--sb-text-faint)] pt-0.5 text-[10.5px]">
										{formatBytes(deployment.bytes)}
									</div>
								</td>
								<td class="code px-3 text-[11.5px] whitespace-nowrap text-muted-foreground">
									{#if deployment.publishedAt}
										published {deployment.publishedAt.slice(0, 16)}
									{:else if deployment.createdAt}
										built {deployment.createdAt.slice(0, 16)}
									{:else}
										<span class="text-[var(--sb-text-faint)]">no timestamp</span>
									{/if}
								</td>
								<td class="px-3 pr-0 text-right whitespace-nowrap">
									{#if deployment.state === 'staged'}
										<a
											href="{base}/deployments/{deployment.deploymentId}"
											class="sb-tag code hover:text-foreground"
											style:color="var(--sb-warning)">review · {shortId(deployment.deploymentId)}</a
										>
									{:else if deployment.state === 'active'}
										<span class="sb-tag code" style:color="var(--sb-secondary)">live</span>
									{:else}
										<span class="sb-tag code text-[var(--sb-text-faint)]">orphaned</span>
									{/if}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		{/each}

		{#if showInvalid && problems.length > 0}
			<div class="flex items-baseline gap-2 pb-1.5 pt-5">
				<span
					class="font-mono text-[10px] uppercase tracking-[0.16em]"
					style:color="var(--sb-error)">Invalid</span
				>
				<span class="text-[var(--sb-text-faint)] text-[10.5px]">
					incomplete metadata or missing warehouse relations
				</span>
			</div>
			<table class="sb-list w-full text-left">
				<tbody>
					{#each problems as deployment (deployment.deploymentId)}
						<tr>
							<td class="code py-2 pr-3 text-[12.5px]">
								<a
									href="{base}/deployments/{deployment.deploymentId}"
									class="text-primary hover:underline">{deployment.deploymentId}</a
								>
							</td>
							<td class="px-3">
								<span class="sb-tag code" style:color="var(--sb-error)"
									>{deployment.state.replaceAll('_', ' ')}</span
								>
							</td>
							<td class="text-muted-foreground px-3 text-[11.5px]">
								{deployment.missingRelationNames.length} missing relation{deployment
									.missingRelationNames.length === 1
									? ''
									: 's'}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	{/if}
</div>
