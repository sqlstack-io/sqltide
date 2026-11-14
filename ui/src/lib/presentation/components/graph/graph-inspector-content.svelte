<script lang="ts">
	import { base } from '$app/paths';
	import SqlBlock from '$lib/presentation/components/sql-block.svelte';
	import type { SqlArtifact } from '$lib/presentation/components/sql-block.svelte';
	import AnchorBadge from '$lib/presentation/components/anchor-badge.svelte';
	import FactRow from '$lib/presentation/components/fact-row.svelte';
	import { formatAgo } from '$lib/formatting/main/format-ago';
	import { formatBytes } from '$lib/formatting/main/format-bytes';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import { formatDuration } from '$lib/formatting/main/format-duration';
	import { formatInteger } from '$lib/formatting/main/format-integer';
	import { formatRate } from '$lib/formatting/main/format-rate';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';
	import { OWNERSHIP_LABEL, REF_TYPE_LABEL } from '$lib/domain/constants';
	import type { Audit, Model, Project, Source, SqlTest } from '$lib/domain/types';

	type Props = {
		tab: 'overview' | 'columns' | 'replay' | 'checks';
		project: Project;
		model: Model | undefined;
		source: Source | undefined;
		audits: Audit[];
		tests: SqlTest[];
	};

	let { tab, project, model, source, audits, tests }: Props = $props();

	const artifacts = $derived.by((): SqlArtifact[] => {
		if (!model) return [];
		return [
			{ label: 'Model', code: model.sql.authored },
			{ label: 'Compiled', code: model.sql.compiled },
			{ label: 'Table DDL', code: model.sql.tableDdl },
			{ label: 'MV DDL', code: model.sql.mvDdl },
			{ label: 'View DDL', code: model.sql.viewDdl }
		];
	});
</script>

<div class="flex flex-col gap-4 p-4">
	{#if tab === 'overview'}
		{#if model}
			{#if model.description}
				<p class="text-muted-foreground text-[12.5px] leading-relaxed">{model.description}</p>
			{/if}
			<SqlBlock {artifacts} maxHeight="300px" />
			<div>
				<FactRow label="Pipeline" value={model.pipeline} href="{base}/pipelines/{model.pipeline}" />
				<FactRow label="Relation" value={model.relationName} mono />
				{#if model.mvRelationName}
					<FactRow label="Writing MV" value={model.mvRelationName} mono />
				{/if}
				<FactRow label="Engine" value={model.storage.engine ?? '—'} mono />
				<FactRow label="Rows" value={formatInteger(model.live.rows)} />
				<FactRow label="Disk" value="{formatBytes(model.live.diskBytes)} · {model.live.parts} parts" />
				<FactRow label="Newest row" value={formatAgo(model.live.newestRowAt, project.capturedAt)} />
				<div
					data-testid="lineage-activity"
					data-state={model.live.activity.state}
					data-source={model.live.activity.source}
					data-approximate={String(model.live.activity.approximate)}
				>
					<FactRow
						label="Activity"
						value={model.live.activity.state}
						tone={model.live.activity.state === 'moving'
							? 'success'
							: model.live.activity.state === 'stalled'
								? 'warning'
								: 'default'}
					/>
					<FactRow
						label="Activity evidence"
						value={model.live.activity.approximate
							? `${model.live.activity.source} (approximate)`
							: model.live.activity.source}
						mono
					/>
					{#if model.live.activity.lastTriggeredAt}
						<FactRow
							label="Last trigger"
							value={formatAgo(model.live.activity.lastTriggeredAt, project.capturedAt)}
						/>
					{/if}
					{#if model.live.activity.lastWriteAt}
						<FactRow
							label="Last write"
							value={formatAgo(model.live.activity.lastWriteAt, project.capturedAt)}
						/>
					{/if}
					{#if !model.live.activity.approximate && model.live.activity.sourceAvailable}
						<FactRow
							label="Recent writes"
							value="{formatInteger(model.live.activity.rowsWritten)} rows / {formatDuration(
								model.live.activity.windowSeconds
							)}"
						/>
					{/if}
					<p class="text-muted-foreground py-1.5 text-[11px] leading-relaxed">
						{model.live.activity.detail}
					</p>
				</div>
				<FactRow
					label="Ownership"
					value={OWNERSHIP_LABEL[model.live.ownership]}
					tone={model.live.ownership === 'direct' ? 'default' : 'warning'}
				/>
				<FactRow
					label="vs compiled"
					value={model.live.inSyncWithCompiled ? 'in sync' : 'drift — rebuild needed'}
					tone={model.live.inSyncWithCompiled ? 'success' : 'warning'}
				/>
				{#each model.live.driftReasons as reason (reason)}
					<div
						class="border-b border-[var(--border-subtle)] py-1.5 font-mono text-[10.5px] leading-relaxed"
						style:color="var(--sb-warning)"
					>
						{reason}
					</div>
				{/each}
			</div>
		{:else if source}
			<div>
				<FactRow label="Kind" value={source.kind === 'kafka' ? 'managed Kafka' : 'adopted table'} />
				{#if source.topic}<FactRow label="Topic" value={source.topic} mono />{/if}
				{#if source.brokerList}<FactRow label="Broker" value={source.brokerList} mono />{/if}
				<FactRow label="Boundary" value={source.boundaryMode} mono />
				<FactRow label="Relation" value={source.relationName} mono />
				<FactRow label="Rows" value={formatInteger(source.live.rows)} />
				<FactRow label="Rate" value={formatRate(source.live.rowsPerSecond)} />
				{#if source.live.kafkaLagMessages !== null}
					<FactRow label="Kafka lag" value={`${formatInteger(source.live.kafkaLagMessages)} messages`} />
				{/if}
				{#if source.live.lastArrivalSeconds !== null}
					<FactRow label="Last arrival" value={`${formatDuration(source.live.lastArrivalSeconds)} ago`} />
				{/if}
				<FactRow
					label="Retention"
					value={source.retentionDays === null ? 'no TTL — rebuilds are lossless' : `${source.retentionDays}d`}
					tone={source.retentionDays === null ? 'success' : 'default'}
				/>
			</div>
			<a href="{base}/sources/{source.name}" class="text-primary font-mono text-[11px] hover:underline"
				>Open source detail →</a
			>
		{/if}
	{:else if tab === 'columns'}
		{#if model}
			{@const authored = model.columns.filter((column) => column.replayRole === null)}
			{@const replay = model.columns.filter((column) => column.replayRole !== null)}
			<div>
				<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">
					Projected columns
				</div>
				{#each authored as column (column.name)}
					<div class="flex items-baseline gap-3 border-b border-[var(--border-subtle)] py-1.5">
						<span class="font-mono text-[11.5px]">{column.name}</span>
						<span class="text-muted-foreground ml-auto font-mono text-[11px]">{column.type}</span>
					</div>
				{/each}
			</div>
			{#if replay.length}
				<div>
					<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">
						Replay columns
					</div>
					{#each replay as column (column.name)}
						<div class="flex items-baseline gap-3 border-b border-[var(--border-subtle)] py-1.5">
							<span class="text-muted-foreground font-mono text-[11.5px] italic">{column.name}</span>
							<span class="text-muted-foreground ml-auto font-mono text-[11px]">{column.type}</span>
						</div>
					{/each}
				</div>
			{/if}
		{:else if source && source.columnMapping}
			<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">
				Replay column mapping
			</div>
			{#each Object.entries(source.columnMapping) as [role, column] (role)}
				<FactRow label={role} value={column ?? '—'} mono />
			{/each}
		{:else}
			<p class="text-muted-foreground text-[12px]">No column mapping.</p>
		{/if}
	{:else if tab === 'replay'}
		{#if model}
			<div>
				<div class="pb-2"><AnchorBadge anchor={model.anchor} showReason /></div>
				<FactRow label="Driving input" value={model.drivingInput ?? 'none (terminal view)'} mono />
				{#if model.live.recordedCoverage}
					<FactRow
						label="Recorded coverage"
						value="{formatTimestamp(model.live.recordedCoverage.from)} → {formatTimestamp(
							model.live.recordedCoverage.to
						)}"
					/>
				{/if}
				<FactRow label="Aggregate" value={model.isAggregate ? 'yes' : 'no'} />
			</div>
			{#if model.refs.length > 1}
				<div>
					<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">
						References
					</div>
					{#each model.refs as ref (ref.name + ref.type)}
						<div class="flex items-center gap-3 border-b border-[var(--border-subtle)] py-1.5">
							<a class="text-primary font-mono text-[11.5px] hover:underline" href={ref.isSource ? `${base}/sources/${ref.name}` : `${base}/catalog/${ref.name}`}>{ref.name}</a>
							<span
								class="ml-auto font-mono text-[10.5px]"
								style:color={ref.type === 'mutable_reference' ? 'var(--sb-warning)' : 'var(--sb-text-faint)'}
								>{REF_TYPE_LABEL[ref.type]}</span
							>
						</div>
					{/each}
				</div>
			{/if}
		{:else if source}
			<div>
				<FactRow label="Boundary mode" value={source.boundaryMode} mono />
				<FactRow label="Retained from" value={formatTimestamp(source.live.oldestEventAt)} />
				<FactRow label="Newest event" value={formatTimestamp(source.live.newestEventAt)} />
				<FactRow label="Retained rows" value={formatCompact(source.live.rows)} />
			</div>
		{/if}
	{:else if tab === 'checks'}
		{#if audits.length === 0 && tests.length === 0}
			<p class="text-muted-foreground text-[12px]">No audits or tests reference this model.</p>
		{/if}
		{#if audits.length}
			<div>
				<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">Audits</div>
				{#each audits as audit (audit.name)}
					<div class="flex items-center gap-2.5 border-b border-[var(--border-subtle)] py-1.5">
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
						<span class="truncate font-mono text-[11.5px]">{audit.name}</span>
						<span class="text-muted-foreground ml-auto shrink-0 font-mono text-[10.5px]">
							{audit.result && !audit.result.passed
								? `${formatInteger(audit.result.failingRowCount)} rows`
								: audit.severity}
						</span>
					</div>
				{/each}
			</div>
		{/if}
		{#if tests.length}
			<div>
				<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">Tests</div>
				{#each tests as test (test.name)}
					<div class="flex items-center gap-2.5 border-b border-[var(--border-subtle)] py-1.5">
						<span
							class="h-1.5 w-1.5 shrink-0 rounded-[2px]"
							style:background={!test.result
								? 'var(--border)'
								: test.result.passed
									? 'var(--sb-success)'
									: 'var(--sb-error)'}
						></span>
						<span class="truncate text-[11.5px]">{test.name}</span>
					</div>
				{/each}
			</div>
		{/if}
		<a href="{base}/quality" class="text-primary font-mono text-[11px] hover:underline">Open Quality →</a>
	{/if}
</div>
