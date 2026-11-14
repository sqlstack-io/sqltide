<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import ErrorPreview from '$lib/presentation/components/error-preview.svelte';
	import LineageCanvas from '$lib/presentation/components/lineage/lineage-canvas.svelte';
	import EdgeLegend from '$lib/presentation/components/lineage/edge-legend.svelte';
	import FactRow from '$lib/presentation/components/fact-row.svelte';
	import { fetchDeploymentDiff } from '$lib/api/main/deployments/fetch-deployment-diff';
	import { publishDeployment } from '$lib/api/main/deployments/publish-deployment';
	import { getProject } from '$lib/api/main/project/get-project';
	import { goto } from '$app/navigation';
	import { refreshDeployments } from '$lib/api/main/project/refresh-deployments';
	import { scopeDeploymentGraph } from '$lib/domain/main/graphs/scope-deployment-graph';
	import { formatBytes } from '$lib/formatting/main/format-bytes';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import { formatInteger } from '$lib/formatting/main/format-integer';
	import type { DeploymentDetail, Project } from '$lib/domain/types';
	import { canAnyPipeline } from '$lib/auth/main/can-any-pipeline';
	type DeploymentModel = DeploymentDetail['models'][number];
	import { createDeploymentDetailState } from './state.svelte';

	const project: Project = getProject();
	const deploymentId: string = $derived(page.params.id ?? '');
	const deployment = createDeploymentDetailState();

	const detail: DeploymentDetail | null = $derived(
		deployment.requestedId === deploymentId ? deployment.detail : null
	);
	const loadError: string | null = $derived(
		deployment.requestedId === deploymentId ? deployment.error : null
	);
	let tab = $state<'graph' | 'diff'>('graph');
	let diff = $state<Awaited<ReturnType<typeof fetchDeploymentDiff>> | null>(null);
	let diffError = $state<string | null>(null);
	let promoting = $state<boolean>(false);
	let promoteError = $state<string | null>(null);
	const promoteAllowed = $derived(canAnyPipeline('deployment.promote'));
	let rollingBack = $state<boolean>(false);
	let rollbackError = $state<string | null>(null);
	let diffRequestGeneration = 0;
	let promotionRequestGeneration = 0;
	let rollbackRequestGeneration = 0;

	$effect(() => {
		void deployment.load(deploymentId);
		return deployment.cancel;
	});

	$effect(() => {
		void deploymentId;
		diffRequestGeneration += 1;
		promotionRequestGeneration += 1;
		rollbackRequestGeneration += 1;
		tab = 'graph';
		diff = null;
		diffError = null;
		promoting = false;
		promoteError = null;
		rollingBack = false;
		rollbackError = null;
	});

	async function promote(): Promise<void> {
		const requestedId: string = deploymentId;
		const requestGeneration: number = ++promotionRequestGeneration;
		promoting = true;
		promoteError = null;
		try {
			const result: Awaited<ReturnType<typeof publishDeployment>> = await publishDeployment(requestedId, 'promote');
			if (deploymentId !== requestedId || promotionRequestGeneration !== requestGeneration) return;
			await refreshDeployments();
			if (deploymentId !== requestedId || promotionRequestGeneration !== requestGeneration) return;
			await goto(`${base}/runs/${result.invocationId}`);
		} catch (error) {
			if (deploymentId === requestedId && promotionRequestGeneration === requestGeneration) {
				promoteError = String(error);
				promoting = false;
			}
		}
	}

	async function rollback(): Promise<void> {
		const requestedId: string = deploymentId;
		const requestGeneration: number = ++rollbackRequestGeneration;
		rollingBack = true;
		rollbackError = null;
		try {
			const result: Awaited<ReturnType<typeof publishDeployment>> = await publishDeployment(requestedId, 'rollback');
			if (deploymentId !== requestedId || rollbackRequestGeneration !== requestGeneration) return;
			await refreshDeployments();
			if (deploymentId !== requestedId || rollbackRequestGeneration !== requestGeneration) return;
			await goto(`${base}/runs/${result.invocationId}`);
		} catch (error) {
			if (deploymentId === requestedId && rollbackRequestGeneration === requestGeneration) {
				rollbackError = String(error);
				rollingBack = false;
			}
		}
	}

	function loadDiff(): void {
		tab = 'diff';
		if (diff !== null) return;
		const requestedId: string = deploymentId;
		const requestGeneration: number = ++diffRequestGeneration;
		fetchDeploymentDiff(requestedId)
			.then((payload) => {
				if (deploymentId === requestedId && diffRequestGeneration === requestGeneration) {
					diff = payload;
				}
			})
			.catch((error: unknown) => {
				if (deploymentId === requestedId && diffRequestGeneration === requestGeneration) {
					diffError = String(error);
				}
			});
	}

	/** Deployment relation names are physical; the graph is keyed by model name. */
	const modelByRelation: Map<string, string> = $derived(
		new Map(project.models.map((model) => [model.relationName, model.name]))
	);

	const deploymentModels: DeploymentModel[] = $derived(detail?.models ?? []);

	const modelNames: Set<string> = $derived(
		new Set(
			deploymentModels
				.map((model) => modelByRelation.get(model.logicalName))
				.filter((name): name is string => name !== undefined)
		)
	);

	function exactRelationName(relation: string | null): string {
		if (relation === null) return '—';
		return relation;
	}

	/**
	 * The project graph narrowed to this deployment plus one hop upstream, with
	 * each node's sublabel replaced by its physical switchover. Reusing the
	 * lineage canvas keeps one visual language for "what depends on what".
	 */
	const scoped: ReturnType<typeof scopeDeploymentGraph> = $derived(
		scopeDeploymentGraph({ project, deploymentModels, modelByRelation, modelNames })
	);

	const totalStagedRows: number = $derived(
		deploymentModels.reduce((total, model) => total + model.stagedRows, 0)
	);
	const totalLiveRows: number = $derived(
		deploymentModels.reduce((total, model) => total + (model.liveRows ?? 0), 0)
	);
	const rowDelta: number = $derived(totalStagedRows - totalLiveRows);
	const newModelCount: number = $derived(
		deploymentModels.filter((model) => model.isNew).length
	);
	const isInitialPublish: boolean = $derived(
		detail?.promotionPreview?.classification === 'initial_publish'
	);
	const additionCount: number = $derived(detail?.promotionPreview?.additions.length ?? 0);
	const replacementCount: number = $derived(detail?.promotionPreview?.replacements.length ?? 0);
	const removalCount: number = $derived(detail?.promotionPreview?.removals.length ?? 0);
	const isPartialPromotion: boolean = $derived(
		detail?.state === 'active' && detail.promotionPreview !== null
	);
	const canRollBack: boolean = $derived(
		detail?.state === 'superseded' && detail.missingRelationNames.length === 0
	);
	const changedLogicalNames: Set<string> = $derived(
		new Set([
			...(detail?.promotionPreview?.additions.map((item) => item.logicalName) ?? []),
			...(detail?.promotionPreview?.replacements.map((item) => item.logicalName) ?? [])
		])
	);
</script>

<AppTopbar title={deploymentId} />

<div class="min-h-0 flex-1 overflow-y-auto px-[18px] py-4">
	{#if loadError !== null}
		<ErrorPreview text={loadError} title="Deployment failed to load" clamp="line-clamp-4" class="max-w-full" />
	{:else if detail === null}
		<div class="flex items-center gap-2 pb-3">
			<a href="{base}/deployments" class="text-muted-foreground hover:text-foreground text-[12px]"
				>← deployments</a
			>
		</div>
		<div
			role="status"
			data-testid="deployment-loading-state"
			class="text-muted-foreground flex items-center gap-2.5 rounded-[4px] border border-border bg-[var(--sb-surface-low)] px-3 py-4 font-mono text-[12px]"
		>
			<span class="h-3 w-3 shrink-0 animate-spin rounded-full border border-current border-t-transparent"></span>
			<div>
				<div class="text-foreground">Loading deployment snapshot…</div>
				<div class="text-[var(--sb-text-faint)] pt-0.5 text-[10.5px]">
					Reading staged relations and promotion state
				</div>
			</div>
		</div>
		<div class="mt-4 flex flex-col gap-4 lg:flex-row" aria-hidden="true">
			<div class="h-[420px] min-w-0 flex-1 animate-pulse rounded-[4px] border border-border bg-[var(--sb-inset)]/35"></div>
			<div class="flex w-full shrink-0 flex-col gap-4 lg:w-[320px]">
				<div class="h-40 animate-pulse rounded-[4px] border border-border bg-[var(--sb-inset)]/35"></div>
				<div class="h-32 animate-pulse rounded-[4px] border border-border bg-[var(--sb-inset)]/35"></div>
			</div>
		</div>
	{:else}
		<div class="flex items-center gap-2 pb-3">
			<a href="{base}/deployments" class="text-muted-foreground hover:text-foreground text-[12px]"
				>← deployments</a
			>
			<span
				class="sb-tag code"
				style:color={detail.state === 'active'
					? 'var(--sb-secondary)'
					: detail.state === 'staged'
						? 'var(--sb-warning)'
						: 'var(--sb-text-faint)'}>{detail.state}</span
			>
			{#if detail.state === 'staged'}
				<span class="text-[var(--sb-text-faint)] text-[11.5px]">
					nothing changes until this is {isInitialPublish ? 'published' : 'promoted'}
				</span>
			{:else if detail.state === 'physical_missing'}
				<span class="text-[11.5px]" style:color="var(--sb-error)">
					invalid · {detail.missingRelationNames.length} mapped relation{detail.missingRelationNames
						.length === 1
						? ''
						: 's'} missing · promotion and rollback disabled
				</span>
			{/if}
		</div>

		<div class="flex flex-col gap-4 lg:flex-row">
			<div class="min-w-0 flex-1 overflow-hidden rounded-[4px] border border-border">
				<div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-1.5">
					<div class="flex shrink-0 overflow-hidden rounded-[3px] border border-border">
						<button
							aria-pressed={tab === 'graph'}
							class="px-2.5 py-1 font-mono text-[10.5px] {tab === 'graph'
								? 'bg-[var(--sb-hover)] text-foreground'
								: 'text-muted-foreground hover:text-foreground'}"
							onclick={() => (tab = 'graph')}>Graph</button
						>
						<button
							aria-pressed={tab === 'diff'}
							class="border-l border-border px-2.5 py-1 font-mono text-[10.5px] {tab === 'diff'
								? 'bg-[var(--sb-hover)] text-foreground'
								: 'text-muted-foreground hover:text-foreground'}"
							onclick={() => loadDiff()}>Diff</button
						>
					</div>
					{#if tab === 'graph'}
						<EdgeLegend compact />
						<span class="text-[var(--sb-text-faint)] ml-auto whitespace-nowrap font-mono text-[10px]">
							{isInitialPublish ? 'node shows new staged relation' : 'node shows live → staged'}
						</span>
					{:else}
						<span class="text-[var(--sb-text-faint)] ml-auto whitespace-nowrap font-mono text-[10px]">
							{diff === null ? 'comparing…' : `${diff.fromEndpoint} → ${diff.toEndpoint}`}
						</span>
					{/if}
				</div>
				{#if tab === 'graph'}
					<div style:height="520px">
						<LineageCanvas {project} graph={scoped} groupMode="none" layoutSalt={deploymentId} />
					</div>
				{:else}
					<div class="max-h-[520px] overflow-y-auto">
						{#if diffError !== null}
							<div class="p-4">
								<ErrorPreview text={diffError} title="Diff failed" clamp="line-clamp-3" class="max-w-full" />
							</div>
						{:else if diff === null}
							<div class="text-muted-foreground p-4 text-[12px]">comparing…</div>
						{:else}
							<table class="sb-list w-full text-left">
								<thead>
									<tr
										class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]"
									>
										<th class="px-3 py-2 font-normal">Relation</th>
										<th class="px-3 py-2 font-normal">Status</th>
										<th class="px-3 py-2 font-normal">Rows</th>
										<th class="px-3 py-2 font-normal">Columns</th>
									</tr>
								</thead>
								<tbody>
									{#each diff.relations as relation (relation.logicalName)}
										<tr>
											<td class="code px-3 py-1.5 text-[12px]">{relation.logicalName}</td>
											<td class="px-3">
												<span
													class="sb-tag code"
													style:color={relation.status === 'unchanged'
														? 'var(--sb-text-faint)'
																: 'var(--sb-warning)'}>{relation.status}</span
												>
											</td>
											<td class="code px-3 text-[11.5px]">
												{relation.fromRowCount ?? '—'} → {relation.toRowCount ?? '—'}
											</td>
											<td class="code text-[var(--sb-text-faint)] px-3 text-[11px]">
												{relation.addedColumns.length > 0
													? `+${relation.addedColumns.join(', ')}`
													: ''}
												{relation.removedColumns.length > 0
													? `-${relation.removedColumns.join(', ')}`
													: ''}
												{relation.addedColumns.length === 0 && relation.removedColumns.length === 0
													? '—'
													: ''}
											</td>
										</tr>
									{/each}
								</tbody>
							</table>
						{/if}
					</div>
				{/if}
			</div>

			<div class="flex w-full shrink-0 flex-col gap-4 lg:w-[320px]">
				<div class="rounded-[4px] border border-border">
					<div
						class="text-[var(--sb-text-faint)] border-b border-border px-3 py-2 font-mono text-[10px] uppercase tracking-[0.16em]"
					>
						Deployment
					</div>
					<div class="px-3 py-1.5">
						<FactRow label="Models" value={formatInteger(detail.modelCount)} mono />
						<FactRow label="Relations" value={formatInteger(detail.relationCount)} mono />
						<FactRow label="New models" value={formatInteger(newModelCount)} mono />
						<FactRow label="Storage" value={formatBytes(detail.bytes)} mono />
						<FactRow label="Created" value={detail.createdAt ?? '—'} mono />
						<FactRow
							label="Published"
							value={detail.publishedAt ?? (isInitialPublish ? 'not published' : 'not promoted')}
							mono
						/>
					</div>
				</div>

				{#if isInitialPublish}
					<div class="rounded-[4px] border border-border">
						<div
							class="text-[var(--sb-text-faint)] border-b border-border px-3 py-2 font-mono text-[10px] uppercase tracking-[0.16em]"
						>
							Initial publish
						</div>
						<div class="px-3 py-1.5">
							<FactRow label="Models to publish" value={formatInteger(detail.modelCount)} mono />
							<FactRow label="Staged rows" value={formatCompact(totalStagedRows)} mono />
							<FactRow label="Bindings added" value={formatInteger(additionCount)} mono />
						</div>
					</div>
				{:else}
					<div class="rounded-[4px] border border-border">
						<div
							class="text-[var(--sb-text-faint)] border-b border-border px-3 py-2 font-mono text-[10px] uppercase tracking-[0.16em]"
						>
							Rows versus live
						</div>
						<div class="px-3 py-1.5">
							<FactRow label="Staged rows" value={formatCompact(totalStagedRows)} mono />
							<FactRow label="Matching live rows" value={formatCompact(totalLiveRows)} mono />
							<FactRow
								label="Delta"
								value={`${rowDelta >= 0 ? '+' : ''}${formatInteger(rowDelta)}`}
								mono
							/>
						</div>
					</div>
				{/if}

				{#if canRollBack}
					<div class="rounded-[4px] border border-border">
						<div
							class="text-[var(--sb-text-faint)] border-b border-border px-3 py-2 font-mono text-[10px] uppercase tracking-[0.16em]"
						>
							Roll back
						</div>
						<div class="px-3 py-2.5">
							<div class="text-muted-foreground pb-2 text-[11.5px]">
								This deployment's relations are still on disk, so it remains a rollback target
								until the janitor removes them. Rolling back makes it live again; bindings change
								one at a time, not all together.
							</div>
							<button
								class="bg-primary text-primary-foreground w-full rounded-[3px] px-3 py-1.5 text-[12px] font-medium disabled:opacity-50"
								onclick={() => void rollback()}
								disabled={rollingBack || !promoteAllowed}
								title={promoteAllowed ? undefined : 'Requires the deployment.promote permission'}
							>
								{rollingBack ? 'rolling back…' : 'Roll back'}
							</button>
							{#if rollbackError !== null}
								<div class="pt-2">
									<ErrorPreview text={rollbackError} title="Rollback failed" clamp="line-clamp-3" class="max-w-full" />
								</div>
							{/if}
							<div class="text-[var(--sb-text-faint)] pt-2 font-mono text-[10.5px]">
								sqt deployment rollback {detail.deploymentId}
							</div>
						</div>
					</div>
				{/if}

				{#if detail.promotionPreview !== null}
					<div class="rounded-[4px] border border-border">
						<div
							class="text-[var(--sb-text-faint)] border-b border-border px-3 py-2 font-mono text-[10px] uppercase tracking-[0.16em]"
						>
							{isInitialPublish ? 'Publish' : isPartialPromotion ? 'Resume promotion' : 'Promote'}
						</div>
						<div class="px-3 py-2.5">
							{#if detail.state !== 'incomplete'}
								<div class="text-muted-foreground pb-2 text-[11.5px]">
									{#if isInitialPublish}
										This operation adds {additionCount} live binding{additionCount === 1 ? '' : 's'}.
										It replaces 0 and removes 0 live bindings.
									{:else}
										Bindings change one at a time, not all together. This operation adds
										{additionCount}, replaces {replacementCount}, and removes {removalCount}. After
										promotion, {detail.wouldOrphan.relationCount} physical relation{detail.wouldOrphan
											.relationCount === 1
											? ''
											: 's'} ({formatBytes(detail.wouldOrphan.bytes)}) will no longer be live.
									{/if}
								</div>
							{/if}
							{#if detail.promotionPreview.blockedReason !== null}
								<div
									class="whitespace-pre-wrap break-words pb-2 font-mono text-[10.5px] leading-snug"
									style:color="var(--sb-error)"
								>{detail.promotionPreview.blockedReason}</div>
							{/if}
							<button
								class="bg-primary text-primary-foreground w-full rounded-[3px] px-3 py-1.5 text-[12px] font-medium disabled:opacity-50"
								onclick={() => void promote()}
								disabled={promoting || !promoteAllowed || detail.promotionPreview.blockedReason !== null}
								title={promoteAllowed ? undefined : 'Requires the deployment.promote permission'}
							>
								{promoting
									? isInitialPublish
										? 'publishing…'
										: 'promoting…'
									: isInitialPublish
										? 'Publish'
										: isPartialPromotion
											? 'Resume promotion'
											: 'Promote'}
							</button>
							{#if promoteError !== null}
								<div class="pt-2">
									<ErrorPreview text={promoteError} title="Promote failed" clamp="line-clamp-3" class="max-w-full" />
								</div>
							{/if}
							<div class="text-[var(--sb-text-faint)] pt-2 font-mono text-[10.5px]">
								sqt deployment promote {detail.deploymentId}
							</div>
						</div>
					</div>
				{/if}
			</div>
		</div>

		<table class="sb-list mt-4 w-full text-left">
			<thead>
				<tr class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]">
					<th class="px-3 py-2 font-normal">Model</th>
					<th class="px-3 py-2 font-normal">Action</th>
					<th class="px-3 py-2 font-normal">Live</th>
					<th class="px-3 py-2 font-normal">Staged</th>
					<th class="px-3 py-2 font-normal">Live rows</th>
					<th class="px-3 py-2 font-normal">Staged rows</th>
					<th class="px-3 py-2 font-normal">Delta</th>
				</tr>
			</thead>
			<tbody>
				{#if detail.promotionPreview !== null}
					{#each detail.promotionPreview.additions as addition (`${addition.database}:${addition.logicalName}`)}
						{@const model = deploymentModels.find((item) => item.logicalName === addition.logicalName)}
						<tr>
							<td class="code px-3 py-1.5 text-[12px]">{addition.logicalName}</td>
							<td class="code px-3 text-[11px]" style:color="var(--primary)">add</td>
							<td class="code text-[var(--sb-text-faint)] px-3 text-[11px]">not published</td>
							<td class="code px-3 text-[11px]">{exactRelationName(addition.physicalName)}</td>
							<td class="code px-3 text-[11.5px]">—</td>
							<td class="code px-3 text-[11.5px]">{model ? formatInteger(model.stagedRows) : '—'}</td>
							<td class="code px-3 text-[11.5px]" style:color="var(--primary)">new</td>
						</tr>
					{/each}
					{#each detail.promotionPreview.replacements as replacement (`${replacement.database}:${replacement.logicalName}`)}
						{@const model = deploymentModels.find((item) => item.logicalName === replacement.logicalName)}
						<tr>
							<td class="code px-3 py-1.5 text-[12px]">{replacement.logicalName}</td>
							<td class="code px-3 text-[11px]" style:color="var(--sb-warning)">replace</td>
							<td class="code text-[var(--sb-text-faint)] px-3 text-[11px]">
								{#if model?.liveDeploymentId && model.liveDeploymentId !== deploymentId}
									<a
										href="{base}/deployments/{model.liveDeploymentId}"
										aria-label="Open live deployment {model.liveDeploymentId}"
										class="text-primary hover:underline">{exactRelationName(replacement.fromPhysicalName)}</a
									>
								{:else}
									{exactRelationName(replacement.fromPhysicalName)}
								{/if}
							</td>
							<td class="code px-3 text-[11px]">{exactRelationName(replacement.toPhysicalName)}</td>
							<td class="code px-3 text-[11.5px]">{model?.liveRows === null || model === undefined ? '—' : formatInteger(model.liveRows)}</td>
							<td class="code px-3 text-[11.5px]">{model ? formatInteger(model.stagedRows) : '—'}</td>
							<td class="code px-3 text-[11.5px]">
								{#if model === undefined || model.liveRows === null}
									—
								{:else}
									{@const delta = model.stagedRows - model.liveRows}
									<span style:color={delta === 0 ? 'var(--sb-text-faint)' : 'var(--sb-secondary)'}
										>{delta >= 0 ? '+' : ''}{formatInteger(delta)}</span
									>
								{/if}
							</td>
						</tr>
					{/each}
					{#each deploymentModels.filter((model) => !changedLogicalNames.has(model.logicalName)) as model (model.logicalName)}
						<tr>
							<td class="code px-3 py-1.5 text-[12px]">{model.logicalName}</td>
							<td class="code text-[var(--sb-text-faint)] px-3 text-[11px]">unchanged</td>
							<td class="code text-[var(--sb-text-faint)] px-3 text-[11px]">{exactRelationName(model.liveRelation)}</td>
							<td class="code px-3 text-[11px]">{exactRelationName(model.stagedRelation)}</td>
							<td class="code px-3 text-[11.5px]">{model.liveRows === null ? '—' : formatInteger(model.liveRows)}</td>
							<td class="code px-3 text-[11.5px]">{formatInteger(model.stagedRows)}</td>
							<td class="code px-3 text-[11.5px]">0</td>
						</tr>
					{/each}
					{#each detail.promotionPreview.removals as removal (`${removal.database}:${removal.logicalName}`)}
						<tr>
							<td class="code px-3 py-1.5 text-[12px]">{removal.logicalName}</td>
							<td class="code px-3 text-[11px]" style:color="var(--sb-error)">remove</td>
							<td class="code text-[var(--sb-text-faint)] px-3 text-[11px]">{exactRelationName(removal.physicalName)}</td>
							<td class="code px-3 text-[11px]">removed</td>
							<td class="code px-3 text-[11.5px]">—</td>
							<td class="code px-3 text-[11.5px]">—</td>
							<td class="code px-3 text-[11.5px]" style:color="var(--sb-error)">removed</td>
						</tr>
					{/each}
				{:else}
					{#each deploymentModels as model (model.logicalName)}
						<tr>
							<td class="code px-3 py-1.5 text-[12px]">{model.logicalName}</td>
							<td class="code text-[var(--sb-text-faint)] px-3 text-[11px]">unchanged</td>
							<td class="code text-[var(--sb-text-faint)] px-3 text-[11px]">{exactRelationName(model.liveRelation)}</td>
							<td class="code px-3 text-[11px]">{exactRelationName(model.stagedRelation)}</td>
							<td class="code px-3 text-[11.5px]">{model.liveRows === null ? '—' : formatInteger(model.liveRows)}</td>
							<td class="code px-3 text-[11.5px]">{formatInteger(model.stagedRows)}</td>
							<td class="code px-3 text-[11.5px]">—</td>
						</tr>
					{/each}
				{/if}
			</tbody>
		</table>
	{/if}
</div>
