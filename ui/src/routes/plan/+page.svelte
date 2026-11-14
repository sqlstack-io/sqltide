<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { goto } from '$app/navigation';
	import PlayIcon from '@lucide/svelte/icons/play';
	import RotateIcon from '@lucide/svelte/icons/rotate-ccw';
	import TriangleAlertIcon from '@lucide/svelte/icons/triangle-alert';
	import TerminalIcon from '@lucide/svelte/icons/terminal';
	import * as Popover from '$ui-kit/popover/main';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import SelectionCombobox from '$lib/presentation/components/plan/selection-combobox.svelte';
	import PlanGraph from '$lib/presentation/components/plan/plan-graph.svelte';
	import ReplayWindowControl from '$lib/presentation/components/plan/replay-window.svelte';
	import CopyCommandButton from '$lib/presentation/components/plan/copy-command-button.svelte';
	import SqlDiff from '$lib/presentation/components/sql-diff.svelte';
	import { startBuild } from '$lib/api/main/build/start-build';
	import { getProject } from '$lib/api/main/project/get-project';
	import { fetchRuns } from '$lib/api/main/runs/fetch-runs';
	import { canAnyPipeline } from '$lib/auth/main/can-any-pipeline';
	import { createPlanView } from '$lib/plan-view/main/create-plan-view';
	import { createPlanLoader } from '$lib/plan-view/main/create-plan-loader.svelte';
	import { createPlanSelection } from '$lib/plan-view/main/create-plan-selection.svelte';
	const project = getProject();
	const buildAllowed = $derived(canAnyPipeline('build.direct.run') || canAnyPipeline('deployment.create'));
	const planView = createPlanView();
	const planLoader = createPlanLoader({
		onLoaded(next, request): void {
			if (request.deploymentId === null && next.deploymentId !== null) {
				planRequestKey = planView.locationRequestKey(planView.deploymentUrl(page.url, next.deploymentId));
				void goto(planView.deploymentUrl(page.url, next.deploymentId), {
					replaceState: true,
					noScroll: true,
					keepFocus: true
				});
			}
		}
	});
	const warehouseConnected = $derived(planLoader.warehouseConnected);
	const planSelection = createPlanSelection({
		currentUrl: () => page.url,
		navigate(nextUrl): void {
			void goto(nextUrl, { replaceState: true, noScroll: true, keepFocus: true });
		}
	});
	const location = $derived(planSelection.location);
	const selectors = $derived(location.selectors);
	const changed = $derived(location.changed);
	const includeMissingUpstream = $derived(location.includeMissingUpstream);
	const replayWindow = $derived(location.replayWindow);
	const deploymentId = $derived(location.deploymentId);
	const plan = $derived(planLoader.plan);
	const planError = $derived(planLoader.error);
	const planLoading = $derived(planLoader.loading);
	const replayCountsLoading = $derived(planLoader.replayCountsLoading);
	let planRequestKey = $state<string>('');
	function requestPlan(
		tokens: string[],
		changedMode: boolean,
		includeMissing: boolean,
		start: string | null,
		deployment: string | null,
		includeReplayCounts: boolean = false
	): void {
		planLoader.request({
			selectors: tokens,
			changed: changedMode,
			includeMissingUpstream: includeMissing,
			startTime: start,
			deploymentId: deployment,
			includeReplayCounts
		});
	}
	let executing = $state<boolean>(false);
	let executeError = $state<string | null>(null);
	let protectionConfirmations = $state<Record<string, string>>({});
	const missingProtections = $derived(
		(plan?.protections ?? []).filter(
			(protection) => protectionConfirmations[protection.pipelineName] !== protection.confirmation
		)
	);
	const acceptedConfirmations = $derived(
		(plan?.protections ?? [])
			.filter(
				(protection) =>
					protectionConfirmations[protection.pipelineName] === protection.confirmation
			)
			.map((protection) => protection.confirmation)
	);
	const executionCommand = $derived(
		planView.buildCommand({
			selectors,
			changed,
			includeMissingUpstream,
			replayWindow,
			acceptedConfirmations,
			plan,
			planLoading
		})
	);
	const planStatus = $derived(planView.status({ planError, planLoading, plan }));
	/** POST the planned options in the dev server's pinned context and follow the run live. */ async function execute(): Promise<void> {
		executing = true;
		executeError = null;
		try {
			const tokens: string[] = selectors.map(planView.selectorToken);
			const start: string | null = planView.replayStartToken(replayWindow);
			const startResult: Awaited<ReturnType<typeof startBuild>> = await startBuild(
				tokens,
				start,
				acceptedConfirmations,
				plan?.deploymentId ?? null,
				changed,
				includeMissingUpstream
			);
			await goto(`${base}/runs/${startResult.invocationId}?live=1`);
		} catch (error) {
			executeError = error instanceof Error ? error.message : String(error);
		} finally {
			executing = false;
		}
	}
	function replan(): void {
		const tokens: string[] = selectors.map(planView.selectorToken);
		const start: string | null = planView.replayStartToken(replayWindow);
		requestPlan(tokens, changed, includeMissingUpstream, start, deploymentId);
	}
	function loadExactReplayCounts(): void {
		const tokens: string[] = selectors.map(planView.selectorToken);
		const start: string | null = planView.replayStartToken(replayWindow);
		requestPlan(tokens, changed, includeMissingUpstream, start, deploymentId, true);
	}
	$effect(() => () => planLoader.stop());
	$effect(() => {
		const tokens: string[] = selectors.map(planView.selectorToken);
		const start: string | null = planView.replayStartToken(replayWindow);
		if (planView.shouldClearReplayStart(page.url)) {
			planSelection.apply(selectors, { mode: 'full' });
			return;
		}
		const key: string = planView.locationRequestKey(page.url);
		if (key === planRequestKey) return;
		planRequestKey = key;
		requestPlan(tokens, changed, includeMissingUpstream, start, deploymentId);
	});
	const summary = $derived(planView.summary(plan));
	let lastBuild = $state<Awaited<ReturnType<typeof fetchRuns>>[number] | null>(null);
	$effect(() => {
		fetchRuns()
			.then((runs) => {
				lastBuild =
					runs.find((run) => run.command === 'build' && run.outcome === 'succeeded') ?? null;
			})
			.catch(() => {
				lastBuild = null;
			});
	});
	const rootSources = $derived<ReturnType<typeof planView.rootSources>>(
		planView.rootSources(project, summary.plannedModelNames)
	);
	let pasted = $state<string>('');
	let pasteOpen = $state<boolean>(false);
	let replacedNote = $state<string>('');
	let replaceFailed = $state<boolean>(false);
	function previewPasted(): void {
		const previousCount: number = selectors.length;
		let parsed: ReturnType<typeof planView.parseCommand>;
		try {
			parsed = planView.parseCommand(pasted);
		} catch (error) {
			replacedNote = error instanceof Error ? error.message : String(error);
			replaceFailed = true;
			return;
		}
		replaceFailed = false;
		planSelection.apply(
			parsed.selectors,
			parsed.replayWindow,
			parsed.deploymentId,
			parsed.changed,
			parsed.includeMissingUpstream
		);
		replacedNote =
			parsed.changed
				? 'Loaded changed-model selection from the pasted command.'
				: previousCount > 0
				? `Replaced ${previousCount} ${previousCount === 1 ? 'selector' : 'selectors'} with ${parsed.selectors.length} from the pasted command.`
				: `Loaded ${parsed.selectors.length} ${parsed.selectors.length === 1 ? 'selector' : 'selectors'} from the pasted command.`;
		setTimeout(() => (replacedNote = ''), 6000);
		pasted = '';
		pasteOpen = false;
	}
</script>

<AppTopbar title="Plan" />

<div class="flex min-h-0 flex-1 flex-col">
	<div class="min-h-0 flex-1 overflow-y-auto">
	<!-- ── selection ───────────────────────────────────────────────────────── -->
	<div class="border-b border-border px-[18px] py-3.5">
		<div class="flex items-center gap-2 pb-2">
			<span
				id="plan-selection-label"
				class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]"
				>What to rebuild</span
			>
			<!-- Deliberately not a second input field: pasting REPLACES the selection
			     wholesale ("what does this command do?"), which is a different act from
			     building one up. Different weight, different affordance. -->
			<Popover.Root bind:open={pasteOpen}>
				<Popover.Trigger
					class="text-muted-foreground hover:text-foreground ml-auto flex items-center gap-1.5 rounded-[4px] border border-border px-2 py-1 font-mono text-[10.5px]"
				>
					<TerminalIcon size={11} /> Preview a command
				</Popover.Trigger>
				<Popover.Content class="w-[min(440px,calc(100vw-2rem))] p-3" align="end">
					<div
						class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
					>
						Paste a command to see what it would do
					</div>
					<input
						bind:value={pasted}
						placeholder="sqt build --select pipeline:order_events"
						class="bg-[var(--sb-inset)] w-full rounded-[4px] border border-border px-2.5 py-1.5 font-mono text-[11.5px] outline-none focus:border-[var(--primary)]"
						onkeydown={(event) => {
							if (event.key === 'Enter') previewPasted();
						}}
					/>
					{#if selectors.length}
						<p class="text-[var(--sb-text-faint)] pt-2 text-[11px] leading-snug">
							Replaces the current selection of {selectors.length}
							{selectors.length === 1 ? 'selector' : 'selectors'}.
						</p>
					{/if}
					<button
						class="bg-primary mt-2 w-full rounded-[4px] px-2.5 py-1.5 font-mono text-[11px] text-white"
						onclick={previewPasted}>Preview</button
					>
				</Popover.Content>
			</Popover.Root>
		</div>

		<SelectionCombobox
			id="plan-selection"
			labelledby="plan-selection-label"
			{project}
			{selectors}
			onchange={planSelection.setSelectors}
		/>
		<div class="flex flex-wrap items-center gap-x-4 gap-y-2 pt-2">
			<button
				type="button"
				aria-pressed={changed}
				class="rounded-[3px] border px-2.5 py-1 font-mono text-[10.5px] transition-colors {changed
					? 'border-[var(--primary)] bg-[var(--sidebar-accent)] text-foreground'
					: 'border-border text-muted-foreground hover:text-foreground'}"
				onclick={() => planSelection.setChanged(!changed)}
			>
				Changed models only
			</button>
			<label class="text-muted-foreground flex items-center gap-1.5 font-mono text-[10.5px]">
				<input
					type="checkbox"
					checked={includeMissingUpstream}
					disabled={selectors.length === 0 && !changed}
					onchange={(event) =>
						planSelection.setIncludeMissingUpstream(event.currentTarget.checked)}
					class="accent-primary h-3 w-3 disabled:opacity-50"
				/>
				Include missing upstream
			</label>
			<span class="text-[var(--sb-text-faint)] font-mono text-[10px]">
				{changed
					? 'Selecting a pipeline or model exits changed mode.'
					: 'Missing upstream dependencies remain excluded unless enabled.'}
			</span>
		</div>

		{#if replacedNote}
			<p
				class="pt-2 font-mono text-[11px]"
				style:color={replaceFailed ? 'var(--sb-error)' : 'var(--sb-secondary)'}>{replacedNote}</p
			>
		{/if}

	</div>

	<!-- ── scope, as a shape ────────────────────────────────────────────────
	     Sits directly under the selection because it answers the question the
	     selection just raised: what did I actually just point at. The tables
	     below can say which models are in scope; only this says how wide the
	     blast radius is and where it stops. -->
	{#if planStatus === 'error'}
		<div
			role="alert"
			data-testid="plan-error-state"
			class="mx-[18px] my-3 space-y-1.5 rounded-[4px] border px-3 py-2.5 font-mono text-[12px]"
			style:border-color="color-mix(in srgb, var(--sb-error) 45%, var(--border))"
			style:background="color-mix(in srgb, var(--sb-error) 6%, transparent)"
		>
			<div class="font-semibold" style:color="var(--sb-error)">Cannot plan this selection</div>
			<div class="text-foreground">{planError}</div>
			<div class="text-[var(--sb-text-faint)] text-[11px]">
				Nothing is planned, so no scope is shown below. Adjust the selection and re-plan.
			</div>
		</div>
	{:else if planStatus === 'loading'}
		<div
			role="status"
			data-testid="plan-loading-state"
			class="text-muted-foreground mx-[18px] my-4 flex items-center gap-2.5 rounded-[4px] border border-border bg-[var(--sb-surface-low)] px-3 py-4 font-mono text-[12px]"
		>
			<RotateIcon size={13} class="animate-spin" />
			<div>
				<div class="text-foreground">
					{changed
						? 'Planning changed models…'
						: selectors.length === 0
							? 'Planning all models…'
							: 'Planning selected scope…'}
				</div>
				<div class="text-[var(--sb-text-faint)] pt-0.5 text-[10.5px]">
					Reading the current warehouse state
				</div>
			</div>
		</div>
	{:else if planStatus === 'ready' && plan !== null}
	<PlanGraph {project} {plan} />
	{/if}

	{#if planStatus === 'ready' && plan !== null}
		<div class="border-b border-border bg-[var(--sb-surface-low)] px-[18px] py-3">
			<div class="flex flex-wrap items-baseline gap-x-3 gap-y-1">
				<span class="font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--sb-text-faint)]">
					Build mode
				</span>
				<strong class="font-mono text-[12px] uppercase" data-testid="plan-build-mode">{plan.mode}</strong>
				<span class="text-muted-foreground text-[11.5px]">
					{#if plan.mode === 'virtual'}
						changes are staged for later promotion
					{:else if plan.mode === 'mixed'}
						virtual changes are staged before direct changes are applied
					{:else}
						changes are applied immediately
					{/if}
				</span>
			</div>
			<div class="text-muted-foreground pt-1 font-mono text-[10.5px] leading-relaxed">
				Replay reads through an upper boundary captured when Execute starts, then continues with live ingestion.
				{#if plan.deploymentId !== null}
					Deployment <code class="text-foreground">{plan.deploymentId}</code> is fixed in this Plan URL.
				{/if}
			</div>
		</div>
	{/if}

	<!-- ── scope ───────────────────────────────────────────────────────────── -->
	{#if planStatus === 'ready' && plan !== null}
	<div class="grid grid-cols-1 gap-5 px-3 py-4 sm:px-[18px] xl:grid-cols-[minmax(0,1fr)_380px]">
		<div class="flex min-w-0 flex-col gap-5">
			<div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
				<div class="rounded-[4px] border border-border p-3">
					<div
						class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
					>
						You selected
					</div>
					{#if changed}
						<div class="font-mono text-[13px]">changed models</div>
						<div class="text-muted-foreground pt-1 font-mono text-[10.5px]">
							resolved from current SQL baselines and source settings
						</div>
					{:else if selectors.length === 0}
						<div class="font-mono text-[13px]">no selector — all models</div>
					{:else}
						<div class="font-display text-[22px] font-semibold leading-none">
							{selectors.length}
							<span class="text-muted-foreground text-[13px] font-normal"
								>{selectors.length === 1 ? 'selector' : 'selectors'}</span
							>
						</div>
						<div class="flex flex-wrap gap-1.5 pt-2">
						{#each selectors as selector (planView.selectorToken(selector))}
							<code class="sb-tag code">{planView.selectorToken(selector)}</code>
							{/each}
						</div>
					{/if}
				</div>

				<div
					class="rounded-[4px] border p-3"
					style:border-color={summary.hasDirectPhase
						? 'color-mix(in srgb, var(--sb-error) 45%, var(--border))'
						: 'color-mix(in srgb, var(--primary) 45%, var(--border))'}
				>
					<div
						class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
					>
						{plan.mode === 'direct'
							? 'Will be dropped and recreated'
							: plan.mode === 'virtual'
								? 'Will be staged'
								: 'Runs in two phases'}
					</div>
					<div class="font-display text-[22px] font-semibold leading-none">
						{summary.plannedModelNames.length}
						<span class="text-muted-foreground text-[13px] font-normal">models</span>
						<span class="text-muted-foreground text-[13px] font-normal"
							>· {summary.plannedRelationNames.length} relations</span
						>
					</div>
					<div class="text-muted-foreground pt-2 font-mono text-[11px]">
						{#if plan.mode !== 'direct'}
							{plan.executionOrder.join(' → ')}
						{:else if selectors.length === 0 && !changed}
							{summary.planEntries.length} · all models
						{:else if changed}
							{summary.changedCount} changed
							{#if summary.sourceDriftCount > 0}
								· {summary.sourceDriftCount} for source drift
							{/if}
							· {summary.downstreamCount} downstream of changed models
						{:else}
							{summary.selectedCount} selected · {summary.downstreamCount} downstream of selection
						{/if}
						{#if summary.missingUpstreamCount > 0}
							· {summary.missingUpstreamCount} missing upstream
						{/if}
						{#if summary.sourceRecreations.length > 0}
							· {summary.sourceRecreations.length}
							{summary.sourceRecreations.length === 1 ? 'source' : 'sources'} recreated
						{/if}
					</div>
				</div>
			</div>

			{#if summary.sourceRecreations.length > 0}
				<div data-testid="plan-source-recreations">
					<div
						class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
					>
						Sources recreated
						<span class="normal-case tracking-normal">— declared Kafka settings changed</span>
					</div>
					<div class="overflow-hidden rounded-[4px] border border-border">
						{#each summary.sourceRecreations as recreation (recreation.relationName)}
							<div
								class="border-b border-[var(--border-subtle)] px-2.5 py-2 last:border-b-0"
								title={recreation.message}
							>
								<div class="flex items-center gap-2.5">
									<span class="code truncate text-[11px]">{recreation.sourceName}</span>
									<span class="text-[var(--sb-text-faint)] ml-auto shrink-0 font-mono text-[10px]">
										source
									</span>
								</div>
								<div class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5 pt-1.5 font-mono text-[10.5px]">
									<span class="text-[var(--sb-text-faint)]">recreated</span>
									<span class="flex min-w-0 flex-wrap gap-x-2">
										<span class="code truncate">{recreation.relationName}</span>
										<span class="text-[var(--sb-text-faint)]">Kafka</span>
										<span class="code truncate">{recreation.landingViewName}</span>
										<span class="text-[var(--sb-text-faint)]">MV</span>
									</span>
									<span class="text-[var(--sb-text-faint)]">kept</span>
									<span class="flex min-w-0 flex-wrap gap-x-2">
										<span class="code truncate">{recreation.landingRelationName}</span>
										<span style:color="var(--sb-success)">rows kept</span>
									</span>
									<span class="text-[var(--sb-text-faint)]">settings</span>
									<span class="flex min-w-0 flex-wrap gap-1.5">
										{#each recreation.driftedSettings as setting (setting)}
											<code class="sb-tag code">{setting}</code>
										{/each}
									</span>
									{#if recreation.selectedPipeline !== null}
										<span class="text-[var(--sb-text-faint)]">rebuilds</span>
										<span class="flex min-w-0 flex-wrap gap-x-2">
											<span class="code truncate">pipeline:{recreation.selectedPipeline}</span>
											{#if recreation.otherReaderPipelines.length > 0}
												<span class="text-[var(--sb-text-faint)]">
													{recreation.otherReaderPipelines.join(', ')} keep reading {recreation.landingRelationName}
												</span>
											{/if}
										</span>
									{/if}
								</div>
								<div class="text-[var(--sb-text-faint)] pt-1.5 font-mono text-[10px] leading-relaxed">
									Resumes from the committed offsets of consumer group
									<code class="text-muted-foreground">{recreation.consumerGroup}</code>; a topic where the
									group has none follows the Kafka engine's offset reset policy.
								</div>
							</div>
						{/each}
					</div>
				</div>
			{/if}

			<div>
				<div
					class="text-[var(--sb-text-faint)] pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
				>
					Execution phases
				</div>
				<div class="grid grid-cols-1 gap-3 {plan.phases.length > 1 ? 'lg:grid-cols-2' : ''}">
					{#each plan.phases as phase, phaseIndex (`${phase.mode}-${phaseIndex}`)}
						<div class="rounded-[4px] border border-border p-3">
							<div class="flex items-baseline gap-2">
								<span class="font-mono text-[10px] text-[var(--sb-text-faint)]">{phaseIndex + 1}</span>
								<strong class="font-mono text-[11.5px] uppercase">{phase.mode}</strong>
								<span class="text-muted-foreground ml-auto font-mono text-[10px]">
									{phase.effect === 'staged' ? 'staged' : 'applied immediately'}
								</span>
							</div>
							<div class="text-muted-foreground pt-1 font-mono text-[10.5px]">
								{phase.modelNames.length} models · {phase.contextModelNames.length} context · {phase.relationNames.length} relations · {phase.actions.length} actions
							</div>
							{#if phase.deploymentId !== null}
								<div class="truncate pt-1 font-mono text-[10px] text-[var(--sb-text-faint)]">
									{phase.deploymentId}
								</div>
							{/if}
							{#if phase.actions.length > 0}
								<details class="pt-2">
									<summary class="text-muted-foreground cursor-pointer font-mono text-[10px]">
										view ordered actions
									</summary>
									<div class="mt-1 max-h-48 overflow-auto rounded-[3px] bg-[var(--sb-inset)] px-2 py-1">
										{#each phase.actions as action, actionIndex (`${action.action}-${action.logicalName}-${actionIndex}`)}
											<div class="border-b border-[var(--border-subtle)] py-1 font-mono text-[10px] last:border-b-0">
												<span class="text-[var(--sb-text-faint)]">{action.phase}</span>
												· {action.action} · {action.logicalName}
												{#if action.physicalName} → {action.physicalName}{/if}
											</div>
										{/each}
									</div>
								</details>
							{/if}
						</div>
					{/each}
				</div>
			</div>

			{#if summary.planEntries.length > 0}
			<div>
				<div
					class="text-[var(--sb-text-faint)] pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
				>
					Model SQL baselines
				</div>
				<div class="overflow-hidden rounded-[4px] border border-border">
					{#each summary.planEntries as entry (entry.modelName)}
						<div class="border-b border-[var(--border-subtle)] px-2.5 py-2 last:border-b-0">
							<div class="flex items-center gap-2.5">
								<span class="code truncate text-[11px]">{entry.modelName}</span>
								<span class="text-[var(--sb-text-faint)] font-mono text-[10px]">
									{planView.reasonBadge(entry.reason)}
								</span>
								{#if entry.sqlChange}
									<span
										class="ml-auto shrink-0 font-mono text-[10px]"
									style:color={planView.sqlChangeColour[entry.sqlChange.status]}
									>
									{planView.sqlChangeLabel[entry.sqlChange.status]}
									</span>
								{:else}
									<span class="text-[var(--sb-text-faint)] ml-auto font-mono text-[10px]">
										not compared
									</span>
								{/if}
							</div>
							{#if entry.sqlChange?.warning}
								<div class="pt-1 font-mono text-[10px]" style:color="var(--sb-warning)">
									{entry.sqlChange.warning}
								</div>
							{/if}
							{#if entry.sqlChange?.unifiedDiff}
								<details class="pt-1">
									<summary class="text-muted-foreground cursor-pointer font-mono text-[10px]">
										view SQL diff
									</summary>
									<div class="mt-1">
										<SqlDiff diff={entry.sqlChange.unifiedDiff} maxHeight="14rem" />
									</div>
								</details>
							{/if}
						</div>
					{/each}
				</div>
			</div>
			{/if}


			<!-- destructive notice -->
			{#if summary.hasDirectPhase}
			<div
				class="flex items-start gap-2.5 rounded-[4px] border px-3 py-2.5"
				style:border-color="color-mix(in srgb, var(--sb-error) 45%, var(--border))"
				style:background="color-mix(in srgb, var(--sb-error) 7%, transparent)"
			>
				<TriangleAlertIcon size={14} class="mt-[2px] shrink-0" color="var(--sb-error)" />
				<div class="text-[12px] leading-snug">
					<span class="font-medium">The direct phase is destructive and does not roll back.</span>
					A failure after teardown leaves the graph incomplete.
				</div>
			</div>
			{/if}

			<!-- teardown / creation -->
			{#if summary.hasDirectPhase}
			<div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
				<div>
					<div
						class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
					>
						Teardown <span class="normal-case tracking-normal">— reverse dependency order</span>
					</div>
					<div class="overflow-hidden rounded-[4px] border border-border">
						{#each plan?.teardown ?? [] as action, index (action.relationName)}
							<div
								class="flex items-center gap-2.5 border-b border-[var(--border-subtle)] px-2.5 py-1.5 last:border-b-0"
							>
								<span class="text-[var(--sb-text-faint)] w-5 shrink-0 text-right font-mono text-[10px]"
									>{index + 1}</span
								>
								<span class="code truncate text-[11px]">{action.relationName}</span>
								<span class="text-[var(--sb-text-faint)] ml-auto shrink-0 font-mono text-[10px]"
									>{action.resourceKind === 'materialized_view' ? 'MV' : action.resourceKind}</span
								>
							</div>
						{/each}
					</div>
				</div>
				<div>
					<div
						class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
					>
						Creation <span class="normal-case tracking-normal">— dependency order</span>
					</div>
					<div class="overflow-hidden rounded-[4px] border border-border">
						{#each plan?.creation ?? [] as action, index (action.relationName)}
							<div
								class="flex items-center gap-2.5 border-b border-[var(--border-subtle)] px-2.5 py-1.5 last:border-b-0"
							>
								<span class="text-[var(--sb-text-faint)] w-5 shrink-0 text-right font-mono text-[10px]"
									>{index + 1}</span
								>
								<span class="code truncate text-[11px]">{action.relationName}</span>
								<span class="text-[var(--sb-text-faint)] ml-auto shrink-0 font-mono text-[10px]"
									>{action.resourceKind === 'materialized_view' ? 'MV' : action.resourceKind}</span
								>
							</div>
						{/each}
					</div>
				</div>
			</div>
			{/if}

		</div>

		<!-- ── right rail ──────────────────────────────────────────────────── -->
		<div class="flex flex-col gap-4">
			<ReplayWindowControl
				{project}
				sources={rootSources}
				window={replayWindow}
				selectionSpecified={selectors.length > 0 || changed}
				rowsToReplay={summary.rowsToReplay}
				onchange={planSelection.setReplayWindow}
			/>

			<!-- replay roots: where the rebuild reads from and what bounds it -->
			{#if plan?.replayRoots.length}
				<div class="rounded-[4px] border border-border p-3">
					<div class="flex items-center pb-1.5">
						<div class="font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--sb-text-faint)]">
							Replay roots
						</div>
						{#if plan.replayRoots.some((root) => root.rowsToReplay === null)}
							<button
								class="text-primary ml-auto font-mono text-[10px] hover:underline disabled:opacity-50"
								disabled={replayCountsLoading}
								onclick={loadExactReplayCounts}
							>
								{replayCountsLoading ? 'Counting…' : 'Load exact counts'}
							</button>
						{/if}
					</div>
					{#each plan.replayRoots as root (root.modelName)}
						<div class="border-b border-[var(--border-subtle)] py-1.5 last:border-b-0">
							<div class="code text-[11px]">{root.modelName}</div>
							<div class="text-[var(--sb-text-faint)] pt-0.5 font-mono text-[10px] leading-relaxed">
								reads {root.drivingInputRelationName} · {root.boundaryMode}
								{#if planView.boundaryColumns(root)}
									on <span class="code">{planView.boundaryColumns(root)}</span>
								{/if}
								{#if root.rowsToReplay !== null}
									· {planView.formatCompact(root.rowsToReplay)} rows
								{/if}
							</div>
							{#if root.hasAggregateSemantics}
								<div class="pt-0.5 font-mono text-[10px]" style:color="var(--sb-warning)">
									aggregate — a start time may not bound it
								</div>
							{/if}
							{#if Object.keys(root.settings).length}
								<div class="pt-0.5 font-mono text-[10px] text-[var(--sb-text-faint)]">
									replay settings · {Object.entries(root.settings)
										.map(([name, value]) => `${name}=${value}`)
										.join(' · ')}
								</div>
							{/if}
						</div>
					{/each}
				</div>
			{/if}

			<!-- planner warnings straight from the server plan -->
			{#if summary.plannerWarnings.length}
				<div
					class="rounded-[4px] border p-3"
					style:border-color="color-mix(in srgb, var(--sb-warning) 45%, var(--border))"
				>
					<div
						class="pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
						style:color="var(--sb-warning)"
					>
						Planner warnings
					</div>
					{#each summary.plannerWarnings as warning, index (index)}
						<div class="border-b border-[var(--border-subtle)] py-1.5 last:border-b-0">
							{#if warning.relatedModel}
								<div class="code text-[11px]">{warning.relatedModel}</div>
							{/if}
							<div class="pt-0.5 font-mono text-[10.5px] leading-relaxed">{warning.message}</div>
						</div>
					{/each}
				</div>
			{/if}

			<!-- ownership hazards get their own loud block -->
			{#if summary.riskyOwnership.length}
				<div
					class="rounded-[4px] border p-3"
					style:border-color="color-mix(in srgb, var(--sb-error) 45%, var(--border))"
				>
					<div
						class="pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
						style:color="var(--sb-error)"
					>
						Ownership hazards
					</div>
					{#each summary.riskyOwnership as item (item.relation)}
						<div class="border-b border-[var(--border-subtle)] py-1.5 last:border-b-0">
							<div class="code text-[11px]">{item.relation}</div>
							<div class="pt-0.5 font-mono text-[10px]" style:color="var(--sb-warning)">
								{planView.ownershipLabel[item.ownership]}
							</div>
						</div>
					{/each}
				</div>
			{/if}

		</div>
	</div>
	{/if}

	</div>

	<!-- ── hand-off ────────────────────────────────────────────────────────── -->
	<div class="bg-[var(--sb-surface-low)] shrink-0 border-t border-border px-[18px] py-3">
		{#if (plan?.protections.length ?? 0) > 0}
			<div
				class="mb-3 space-y-2 rounded-[4px] border px-3 py-2.5"
				style:border-color="color-mix(in srgb, var(--sb-warning) 45%, var(--border))"
				style:background="color-mix(in srgb, var(--sb-warning) 7%, transparent)"
			>
				{#each plan?.protections ?? [] as protection (protection.pipelineName)}
					<div class="grid gap-1.5 sm:grid-cols-[1fr_260px] sm:items-end">
						<div>
							<div class="font-mono text-[11.5px] font-semibold" style:color="var(--sb-warning)">
								Protected pipeline: {protection.pipelineName}
							</div>
							<div class="text-muted-foreground pt-0.5 text-[11.5px]">{protection.warning}</div>
						</div>
						<label class="grid gap-1 font-mono text-[10.5px] text-muted-foreground">
							Type <code>{protection.confirmation}</code> to continue
							<input
								value={protectionConfirmations[protection.pipelineName] ?? ''}
								oninput={(event) =>
									(protectionConfirmations[protection.pipelineName] = event.currentTarget.value)}
								spellcheck="false"
								class="bg-[var(--sb-inset)] rounded-[4px] border border-border px-2.5 py-1.5 font-mono text-[11px] text-foreground outline-none focus:border-[var(--sb-warning)]"
							/>
						</label>
					</div>
				{/each}
			</div>
		{/if}
		<div class="flex flex-wrap items-center gap-3">
			<div class="text-muted-foreground shrink-0 font-mono text-[10.5px]">
				{#if plan === null}
					Waiting for the current warehouse plan
				{:else}
					Planned against the {planView.formatClock(plan.plannedAt)} snapshot
					{#if lastBuild}
						· last build {planView.formatDuration(lastBuild.durationMs / 1000)}
						({lastBuild.selectedNodeCount} nodes, {planView.formatAgo(
							lastBuild.startedAt,
							project.capturedAt
						)})
					{/if}
				{/if}
			</div>
			<button
				class="text-muted-foreground hover:text-foreground flex shrink-0 items-center gap-1.5 rounded-[4px] border border-border px-2 py-1 font-mono text-[10.5px] disabled:opacity-60"
				title="The warehouse may have moved since this snapshot"
				disabled={planLoading}
				onclick={replan}
			>
				<RotateIcon size={11} /> {planLoading ? 'planning…' : 'Re-plan'}
			</button>
			<code
				class="bg-[var(--sb-inset)] order-last min-w-0 basis-full truncate rounded-[4px] border border-border px-2.5 py-1.5 font-mono text-[11.5px] lg:order-none lg:ml-auto lg:flex-1 lg:basis-auto"
				>$ {executionCommand}</code
			>
			<CopyCommandButton
				command={executionCommand}
				disabled={planLoading || planError !== null || plan === null}
			/>
			<button
				class="bg-primary flex shrink-0 items-center gap-1.5 rounded-[4px] px-3 py-1.5 font-mono text-[11px] font-medium text-white disabled:opacity-60"
				title={!warehouseConnected
					? 'Waiting for the warehouse connection'
					: buildAllowed
						? "Runs these options in the dev server's pinned context"
					: 'Requires build.direct.run or deployment.create'}
					disabled={executing ||
					!warehouseConnected ||
					planLoading ||
					planError !== null ||
					plan === null ||
					missingProtections.length > 0 ||
					(changed && summary.plannedModelNames.length === 0) ||
					!buildAllowed}
				onclick={() => void execute()}
			>
				<PlayIcon size={12} /> {executing ? 'starting…' : 'Execute'}
			</button>
		</div>
		{#if executeError}
			<div class="pt-2 font-mono text-[11px]" style:color="var(--sb-error)">{executeError}</div>
		{/if}
	</div>
</div>
