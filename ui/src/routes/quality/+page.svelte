<script lang="ts">
	import { base } from '$app/paths';
	import PlayIcon from '@lucide/svelte/icons/play';
	import ChevronRightIcon from '@lucide/svelte/icons/chevron-right';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import SqlBlock from '$lib/presentation/components/sql-block.svelte';
	import AuditPolicySummary from '$lib/quality-monitoring/components/audit-policy-summary.svelte';
	import AuditScheduleCell from '$lib/quality-monitoring/components/audit-schedule-cell.svelte';
	import QualityAutomation from '$lib/quality-monitoring/components/quality-automation.svelte';
	import SchedulerStatus from '$lib/quality-monitoring/components/scheduler-status.svelte';
	import { createAuditSchedulerState } from '$lib/quality-monitoring/main/create-audit-scheduler-state.svelte';
	import { getProject } from '$lib/api/main/project/get-project';
	import { runCheck } from '$lib/api/main/quality/run-check';
	import { canAnyPipeline } from '$lib/auth/main/can-any-pipeline';
	import { auditCounts } from '$lib/domain/main/quality/audit-counts';
	import { testCounts } from '$lib/domain/main/quality/test-counts';
	import { formatAgo } from '$lib/formatting/main/format-ago';
	import type { CheckRunResult } from '$lib/api/types';
	import type { CellValue, Project, QualityDriftReason, SqlTest } from '$lib/domain/types';

	const project: Project = getProject();

	// Audits and tests are RUNNABLE here even though the UI is read-only: in
	// SQLTide both go through client.query() and never the mutation gateway.
	// Only `build` writes.
	const audits = $derived(auditCounts(project.audits));
	const tests = $derived(testCounts(project.tests));
	const scheduler = createAuditSchedulerState();
	const auditScheduleByName = $derived(
		new Map((scheduler.payload?.audits ?? []).map((item) => [item.name, item]))
	);

	$effect(() => scheduler.start());

	type Filter = 'all' | 'failing' | 'passing';
	let filter = $state<Filter>('all');
	type QualityView = 'audits' | 'tests' | 'history';
	let qualityView = $state<QualityView>('audits');

	function showView(view: QualityView): void {
		qualityView = view;
	}

	// Collapsed by default — the list is far easier to scan, and the row already
	// carries the outcome. Expand is opt-in.
	let expandedAudit = $state<string | null>(null);
	let expandedTest = $state<string | null>(null);
	let revealedAudit = $state<string | null>(null);

	function revealAudit(index: number): void {
		requestAnimationFrame(() => {
			const row: HTMLElement | null = document.getElementById(`quality-audit-${index}`);
			row?.scrollIntoView({ block: 'center' });
			row?.focus({ preventScroll: true });
		});
	}

	$effect(() => {
		const auditName: string | null = new URL(window.location.href).searchParams.get('audit');
		if (auditName === null || revealedAudit === auditName) return;
		const auditIndex: number = project.audits.findIndex((audit) => audit.name === auditName);
		if (auditIndex === -1) return;
		revealedAudit = auditName;
		qualityView = 'audits';
		filter = 'all';
		expandedAudit = auditName;
		revealAudit(auditIndex);
	});

	// A check that has never run is neither passing nor failing — it only shows
	// under "All". Fabricating an outcome for it was worse than useless.
	function auditVisible(audit: Project['audits'][number]): boolean {
		if (filter === 'all') return true;
		if (!audit.result) return false;
		if (audit.result.deferredUntil) return false;
		return filter === 'passing' ? audit.result.passed : !audit.result.passed;
	}

	function testVisible(test: SqlTest): boolean {
		if (filter === 'all') return true;
		if (!test.result) return false;
		return filter === 'passing' ? test.result.passed : !test.result.passed;
	}

	function cell(value: CellValue): string {
		if (value === null) return 'NULL';
		if (value === '') return '‹empty›';
		return String(value);
	}

	const DRIFT_LABEL: Record<QualityDriftReason, string> = {
		binding_changed: 'binding changed',
		definition_changed: 'definition changed',
		execution_changed: 'execution changed',
		schedule_changed: 'schedule changed'
	};

	function driftSummary(reasons: QualityDriftReason[]): string {
		return reasons.map((reason) => DRIFT_LABEL[reason]).join(', ');
	}

	const visibleAudits = $derived(project.audits.filter(auditVisible));
	const visibleTests = $derived(project.tests.filter(testVisible));

	let runningCheck = $state<string | null>(null);
	let runError = $state<string | null>(null);
	const auditsAllowed = $derived(canAnyPipeline('quality.audit.run'));
	const testsAllowed = $derived(canAnyPipeline('quality.test.run'));
	let runningAll = $state<'audits' | 'tests' | null>(null);
	let runAllProgress = $state<number>(0);

	function applyAuditOutcome(name: string, outcome: CheckRunResult): void {
		const audit: Project['audits'][number] | undefined = project.audits.find(
			(item) => item.name === name
		);
		if (!audit) return;
		audit.result = {
			passed: outcome.passed,
			failingRowCount: outcome.failingRowCount ?? 0,
			sampleColumns: outcome.sampleColumns ?? [],
			sampleRows: outcome.sampleRows ?? [],
			checkedAt: new Date().toISOString(),
			driftReasons: [],
			deferredUntil: outcome.deferredUntil ?? null
		};
	}

	async function executeAudit(name: string): Promise<void> {
		runningCheck = name;
		runError = null;
		try {
			const outcome: CheckRunResult = (await runCheck({ kind: 'audit', name })) as CheckRunResult;
			if (outcome.deferredUntil) {
				runError = `Audit is warming up until ${outcome.deferredUntil} UTC`;
				return;
			}
			applyAuditOutcome(name, outcome);
		} catch (error) {
			runError = String(error);
		} finally {
			runningCheck = null;
		}
	}

	async function executeTest(name: string): Promise<void> {
		runningCheck = name;
		runError = null;
		try {
			const outcome: CheckRunResult = (await runCheck({ kind: 'test', name })) as CheckRunResult;
			const test: SqlTest | undefined = project.tests.find((item) => item.name === name);
			if (test) {
					test.result = {
					passed: outcome.passed,
					targets: (outcome.targets ?? []).map((target) => ({
						targetModelName: target.targetModelName,
						passed: target.passed,
						columns: target.columns ?? [],
						missingRows: target.missingRows ?? [],
						unexpectedRows: target.unexpectedRows ?? []
					})),
					checkedAt: new Date().toISOString(),
					errorMessage: outcome.errorMessage ?? null,
					driftReasons: []
				};
			}
		} catch (error) {
			runError = String(error);
		} finally {
			runningCheck = null;
		}
	}

	async function executeAllAudits(): Promise<void> {
		runningAll = 'audits';
		runAllProgress = 0;
		runError = null;
		try {
			const outcomes: (CheckRunResult & { name: string })[] = (await runCheck({
				kind: 'audits',
				names: project.audits.map((audit) => audit.name)
			})) as (CheckRunResult & { name: string })[];
			for (const outcome of outcomes) {
				applyAuditOutcome(outcome.name, outcome);
			}
		} catch (error) {
			runError = String(error);
		} finally {
			runningAll = null;
		}
	}

	async function executeAllTests(): Promise<void> {
		runningAll = 'tests';
		runAllProgress = 0;
		for (const test of project.tests) {
			await executeTest(test.name);
			runAllProgress += 1;
		}
		runningAll = null;
	}
</script>

<AppTopbar title="Quality">
	<button
		class="text-muted-foreground hover:text-foreground flex items-center gap-1.5 rounded-[4px] border border-border px-2.5 py-1.5 font-mono text-[11px] disabled:opacity-60"
		disabled={runningAll !== null || runningCheck !== null || !auditsAllowed}
		title={auditsAllowed ? undefined : 'Requires the quality.audit.run permission'}
		onclick={() => void executeAllAudits()}
	>
		<PlayIcon size={11} />
		{runningAll === 'audits'
			? `running ${project.audits.length} audits…`
			: 'Run audits'}
	</button>
	<button
		class="text-muted-foreground hover:text-foreground flex items-center gap-1.5 rounded-[4px] border border-border px-2.5 py-1.5 font-mono text-[11px] disabled:opacity-60"
		disabled={runningAll !== null || runningCheck !== null || !testsAllowed}
		title={testsAllowed ? undefined : 'Requires the quality.test.run permission'}
		onclick={() => void executeAllTests()}
	>
		<PlayIcon size={11} />
		{runningAll === 'tests' ? `running ${runAllProgress + 1}/${project.tests.length}…` : 'Run tests'}
	</button>
</AppTopbar>

<div class="min-h-0 flex-1 overflow-y-auto">
	{#if import.meta.env.VITE_SQLTIDE_DEMO === '1' && project.audits.length === 0 && project.tests.length === 0}
		<p class="text-muted-foreground mx-[18px] mt-[18px] rounded-[4px] border border-border p-3.5 text-[12px]" data-testid="demo-no-audits">
			This playground defines no audits or tests, so there is nothing to run here. Run
			<code class="font-mono">sqt playground --template streaming</code> to try audits and tests with ClickHouse on your machine.
		</p>
	{/if}
	<div class="px-[18px] pt-[18px]">
		<SchedulerStatus payload={scheduler.payload} loading={scheduler.loading} error={scheduler.error} />
	</div>
	<div class="mt-3 flex items-center gap-2.5 overflow-x-auto border-b border-border px-[18px]">
		{#each [['audits', `Audits ${audits.passing}/${audits.total}`], ['tests', `Tests ${tests.passing}/${tests.total}`], ['history', 'Cycle history']] as [key, label] (key)}
			<button
				aria-pressed={qualityView === key}
				class="relative shrink-0 px-3 py-2.5 font-mono text-[11px] transition-colors {qualityView === key
					? 'text-foreground after:absolute after:inset-x-2 after:bottom-0 after:h-[2px] after:rounded-t after:bg-primary after:content-[\'\']'
					: 'text-muted-foreground hover:text-foreground'}"
				onclick={() => showView(key as QualityView)}
			>
				{label}
			</button>
		{/each}
		{#if qualityView === 'audits' || qualityView === 'tests'}
			<div class="ml-1 flex items-center gap-1 border-l border-border pl-3">
				{#each [['all', 'All'], ['failing', 'Failing'], ['passing', 'Passing']] as [key, label] (key)}
					<button
						aria-pressed={filter === key}
						class="rounded-[4px] px-2 py-1 font-mono text-[10.5px] transition-colors {filter === key
							? 'bg-[var(--sidebar-accent)] text-foreground'
							: 'text-muted-foreground hover:text-foreground'}"
						onclick={() => (filter = key as Filter)}
					>
						{label}
					</button>
				{/each}
			</div>
			{#if qualityView === 'audits'}
				<span class="text-muted-foreground ml-auto shrink-0 font-mono text-[11px]">
					{audits.passing} passing
					{#if audits.warning}· <span style:color="var(--sb-warning)">{audits.warning} warning</span>{/if}
					{#if audits.failing}· <span style:color="var(--sb-error)">{audits.failing} failed</span>{/if}
				</span>
			{:else}
				<span class="text-muted-foreground ml-auto shrink-0 font-mono text-[11px]">
					{tests.passing} passing · {tests.total - tests.passing - tests.failing} not run
					{#if tests.failing}· <span style:color="var(--sb-error)">{tests.failing} failed</span>{/if}
				</span>
			{/if}
		{/if}
	</div>

	<div class="flex flex-col gap-6 p-[18px]">
		<QualityAutomation view={qualityView === 'history' ? 'history' : 'current'} capturedAt={project.capturedAt} />

		{#if qualityView === 'audits'}
			<!-- ── audits ──────────────────────────────────────────────────────── -->
			<div>
			<div
				class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Audits
			</div>

			<div class="overflow-hidden rounded-[4px] border border-border">
				{#each visibleAudits as audit, auditIndex (audit.name)}
					{@const failing = audit.result && !audit.result.passed}
					{@const schedule = auditScheduleByName.get(audit.name)}
					<div
						id="quality-audit-{auditIndex}"
						data-quality-name={audit.name}
						tabindex="-1"
						class="border-b border-[var(--border-subtle)] last:border-b-0"
					>
						<div
							class="hover:bg-[var(--sb-hover)] flex w-full items-center gap-3 px-3 py-2 text-left"
						>
							<button
								aria-label="Expand audit {audit.name}"
								aria-expanded={expandedAudit === audit.name}
								aria-controls="audit-panel-{auditIndex}"
								class="shrink-0 transition-transform"
								style:transform={expandedAudit === audit.name ? 'rotate(90deg)' : 'none'}
								onclick={() => (expandedAudit = expandedAudit === audit.name ? null : audit.name)}
								><ChevronRightIcon size={12} class="text-muted-foreground" /></button
							>
							<span
								class="h-1.5 w-1.5 shrink-0 rounded-[2px]"
								style:background={!audit.result
									? 'var(--border)'
									: audit.result.passed
										? 'var(--sb-success)'
										: audit.severity === 'warning'
											? 'var(--sb-warning)'
											: 'var(--sb-error)'}
							></span>
							<span class="code min-w-0 flex-1 truncate text-[12px]">{audit.name}</span>
							<span class="hidden w-[70px] shrink-0 font-mono text-[10.5px] lg:block">
								{#if audit.generic}
									<span class="text-[var(--sb-text-faint)]">generic</span>
								{:else}
									<span class="text-[var(--sb-text-faint)]">singular</span>
								{/if}
							</span>
							<span class="hidden w-[64px] shrink-0 font-mono text-[10.5px] md:block">
								{#if failing}
									<span style:color={audit.severity === 'warning' ? 'var(--sb-warning)' : 'var(--sb-error)'}>
										{audit.severity}
									</span>
								{/if}
							</span>
							<span class="hidden w-[170px] shrink-0 truncate font-mono text-[11px] xl:block">
								{#each audit.referencedModels as model, index (model)}
									<a href="{base}/catalog/{model}" class="text-primary hover:underline"
										>{model}</a
									>{#if index < audit.referencedModels.length - 1}, {/if}
								{/each}
							</span>
							<AuditScheduleCell
								scheduled={audit.policy.scheduled}
								{schedule}
								payload={scheduler.payload}
								error={scheduler.error}
							/>
							<span class="hidden w-[92px] shrink-0 text-right font-mono text-[11px] sm:block">
								{#if !audit.result}
									<span class="text-[var(--sb-text-faint)]">not run</span>
								{:else if audit.result.deferredUntil}
									<span style:color="var(--sb-warning)">warming up</span>
								{:else if failing}
									<span style:color={audit.severity === 'warning' ? 'var(--sb-warning)' : 'var(--sb-error)'}
										>{audit.result.failingRowCount.toLocaleString()} rows</span
									>
								{:else}
									<span style:color="var(--sb-success)">pass</span>
								{/if}
								{#if audit.result?.driftReasons.length}
									<span class="text-[var(--sb-text-faint)]" title={driftSummary(audit.result.driftReasons)}
										>· changed</span
									>
								{/if}
							</span>
							<span class="text-[var(--sb-text-faint)] hidden w-[74px] shrink-0 text-right font-mono text-[10.5px] lg:block"
								>{formatAgo(audit.result?.checkedAt ?? null, project.capturedAt)}</span
							>
						</div>

						{#if expandedAudit === audit.name}
							<div
								id="audit-panel-{auditIndex}"
								class="flex flex-col gap-3 border-t border-[var(--border-subtle)] px-3 py-3"
							>
								<AuditPolicySummary
									severity={audit.severity}
									scheduled={audit.policy.scheduled}
									cadenceSeconds={audit.policy.cadenceSeconds}
									warmupSeconds={audit.policy.warmupSeconds}
								/>
								<div class="flex items-center gap-2">
									<button
										class="text-muted-foreground hover:text-foreground flex items-center gap-1.5 rounded-[4px] border border-border px-2 py-1 font-mono text-[10.5px] disabled:opacity-60"
										disabled={runningCheck !== null || !auditsAllowed}
										title={auditsAllowed ? undefined : 'Requires the quality.audit.run permission'}
										onclick={() => void executeAudit(audit.name)}
									>
										{runningCheck === audit.name ? 'running…' : 'run audit'}
									</button>
									{#if runError && runningCheck === null}
										<span class="font-mono text-[11px]" style:color="var(--sb-error)">{runError}</span>
									{/if}
								</div>
								{#if audit.description}
									<p class="text-muted-foreground text-[12px] leading-relaxed">
										{audit.description}
									</p>
								{/if}

								<!-- Sample violating rows: the payoff the CLI can only print once. -->
								{#if audit.result && !audit.result.passed && audit.result.sampleRows.length}
									<div>
										<div
											class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
										>
											Violating rows — sample of {audit.result.failingRowCount.toLocaleString()}
										</div>
										<div class="overflow-x-auto rounded-[3px] border border-border">
											<table class="w-full text-left">
												<thead>
													<tr class="bg-[var(--sb-surface-low)]">
														{#each audit.result.sampleColumns as column (column)}
															<th
																class="text-[var(--sb-text-faint)] border-b border-border px-2.5 py-1.5 font-mono text-[10px] font-normal"
																>{column}</th
															>
														{/each}
													</tr>
												</thead>
												<tbody>
													{#each audit.result.sampleRows as row, rowIndex (rowIndex)}
														<tr>
															{#each row as value, colIndex (colIndex)}
																<td
																	class="border-b border-[var(--border-subtle)] px-2.5 py-1.5 font-mono text-[11px]"
																	style:color={value === null || value === ''
																		? 'var(--sb-mono-err)'
																		: 'var(--foreground)'}>{cell(value)}</td
																>
															{/each}
														</tr>
													{/each}
												</tbody>
											</table>
										</div>
									</div>
								{/if}

								<SqlBlock
									artifacts={[{ label: 'Audit SQL', code: audit.sql }]}
									maxHeight="200px"
									caption={audit.file}
								/>
							</div>
						{/if}
					</div>
				{/each}
			</div>
		</div>

		{:else if qualityView === 'tests'}
			<!-- ── tests ───────────────────────────────────────────────────────── -->
			<div>
			<div
				class="text-[var(--sb-text-faint)] flex items-baseline gap-2 pb-2 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Tests
			</div>

			<div class="overflow-hidden rounded-[4px] border border-border">
				{#each visibleTests as test, testIndex (test.name)}
					<div
						data-quality-name={test.name}
						class="border-b border-[var(--border-subtle)] last:border-b-0"
					>
						<div
							class="hover:bg-[var(--sb-hover)] flex w-full items-center gap-3 px-3 py-2 text-left"
						>
							<button
								aria-label="Expand test {test.name}"
								aria-expanded={expandedTest === test.name}
								aria-controls="test-panel-{testIndex}"
								class="shrink-0 transition-transform"
								style:transform={expandedTest === test.name ? 'rotate(90deg)' : 'none'}
								onclick={() => (expandedTest = expandedTest === test.name ? null : test.name)}
								><ChevronRightIcon size={12} class="text-muted-foreground" /></button
							>
							<span
								class="h-1.5 w-1.5 shrink-0 rounded-[2px]"
								style:background={!test.result
									? 'var(--border)'
									: test.result.passed
										? 'var(--sb-success)'
										: 'var(--sb-error)'}
							></span>
							<span class="min-w-0 flex-1 truncate text-[12px]">{test.name}</span>
							<span class="w-[200px] shrink-0 truncate font-mono text-[11px]">
								{#each test.targets as target, index (target)}
									<a href="{base}/catalog/{target}" class="text-primary hover:underline"
										>{target}</a
									>{#if index < test.targets.length - 1}, {/if}
								{/each}
							</span>
							<span class="w-[90px] shrink-0 text-right font-mono text-[11px]">
								{#if !test.result}
									<span class="text-[var(--sb-text-faint)]">not run</span>
								{:else if test.result.passed}
									<span style:color="var(--sb-success)">pass</span>
								{:else}
									<span style:color="var(--sb-error)">fail</span>
								{/if}
								{#if test.result?.driftReasons.length}
									<span class="text-[var(--sb-text-faint)]" title={driftSummary(test.result.driftReasons)}
										>· changed</span
									>
								{/if}
							</span>
							<span class="text-[var(--sb-text-faint)] w-[74px] shrink-0 text-right font-mono text-[10.5px]"
								>{formatAgo(test.result?.checkedAt ?? null, project.capturedAt)}</span
							>
						</div>

						{#if expandedTest === test.name}
							<div
								id="test-panel-{testIndex}"
								class="flex flex-col gap-3 border-t border-[var(--border-subtle)] px-3 py-3"
							>
								<div class="flex items-center gap-2">
									<button
										class="text-muted-foreground hover:text-foreground flex items-center gap-1.5 rounded-[4px] border border-border px-2 py-1 font-mono text-[10.5px] disabled:opacity-60"
										disabled={runningCheck !== null || !testsAllowed}
										title={testsAllowed ? undefined : 'Requires the quality.test.run permission'}
										onclick={() => void executeTest(test.name)}
									>
										{runningCheck === test.name ? 'running…' : 'run test'}
									</button>
								</div>
								{#if test.result && !test.result.passed}
									{#if test.result.errorMessage}
										<p class="font-mono text-[11px]" style:color="var(--sb-error)">
											{test.result.errorMessage}
										</p>
									{/if}
									<!-- Expected vs actual per compared target, side by side — the
									     shape the result model already has. -->
									{#each test.result.targets.filter((target) => !target.passed) as target (target.targetModelName)}
										<div>
											{#if test.result.targets.length > 1}
												<div class="code pb-1.5 text-[11px]">{target.targetModelName}</div>
											{/if}
											<div class="grid grid-cols-2 gap-3">
												<div>
													<div class="pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]" style:color="var(--sb-warning)">
														Missing — expected, not produced
													</div>
													{#if target.missingRows.length === 0}
														<p class="text-muted-foreground text-[11.5px]">none</p>
													{:else}
														<div class="overflow-x-auto rounded-[3px] border border-border">
															<table class="w-full text-left">
																<thead>
																	<tr class="bg-[var(--sb-surface-low)]">
																		{#each target.columns as column (column)}
																			<th
																				class="text-[var(--sb-text-faint)] border-b border-border px-2.5 py-1.5 font-mono text-[10px] font-normal"
																				>{column}</th
																			>
																		{/each}
																	</tr>
																</thead>
																<tbody>
																	{#each target.missingRows as row, rowIndex (rowIndex)}
																		<tr>
																			{#each row as value, colIndex (colIndex)}
																				<td
																					class="border-b border-[var(--border-subtle)] px-2.5 py-1.5 font-mono text-[11px]"
																					>{cell(value)}</td
																				>
																			{/each}
																		</tr>
																	{/each}
																</tbody>
															</table>
														</div>
													{/if}
												</div>
												<div>
													<div class="pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]" style:color="var(--sb-error)">
														Unexpected — produced, not expected
													</div>
													{#if target.unexpectedRows.length === 0}
														<p class="text-muted-foreground text-[11.5px]">none</p>
													{:else}
														<div class="overflow-x-auto rounded-[3px] border border-border">
															<table class="w-full text-left">
																<thead>
																	<tr class="bg-[var(--sb-surface-low)]">
																		{#each target.columns as column (column)}
																			<th
																				class="text-[var(--sb-text-faint)] border-b border-border px-2.5 py-1.5 font-mono text-[10px] font-normal"
																				>{column}</th
																			>
																		{/each}
																	</tr>
																</thead>
																<tbody>
																	{#each target.unexpectedRows as row, rowIndex (rowIndex)}
																		<tr>
																			{#each row as value, colIndex (colIndex)}
																				<td
																					class="border-b border-[var(--border-subtle)] px-2.5 py-1.5 font-mono text-[11px]"
																					style:color="var(--sb-mono-err)">{cell(value)}</td
																				>
																			{/each}
																		</tr>
																	{/each}
																</tbody>
															</table>
														</div>
													{/if}
												</div>
											</div>
										</div>
									{/each}
								{/if}

								<SqlBlock
									artifacts={[{ label: 'Test SQL', code: test.sql }]}
									maxHeight="240px"
									caption={test.file}
								/>
							</div>
						{/if}
					</div>
				{/each}
			</div>
		</div>
		{/if}
	</div>
</div>
