<script lang="ts">
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import ArrowLeftIcon from '@lucide/svelte/icons/arrow-left';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import FactRow from '$lib/presentation/components/fact-row.svelte';
	import { can } from '$lib/auth/main/can';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';
	import TickTimeline from '$lib/sensor-automation/components/tick-timeline.svelte';
	import { createSensorsState } from '$lib/sensor-automation/main/create-sensors-state.svelte';
	import { describeTick } from '$lib/sensor-automation/main/describe-tick';
	import { tickTone } from '$lib/sensor-automation/main/tick-tone';
	import type { SensorSummary } from '$lib/sensor-automation/types';

	const sensors = createSensorsState();
	const sensorName = $derived(page.params.name ?? '');

	$effect(() => sensors.start());
	$effect(() => {
		if (sensorName !== '' && sensors.selectedSensor !== sensorName) {
			void sensors.selectSensor(sensorName);
		}
	});

	const manageAllowed = $derived(can('automation.manage'));
	const manageHint = 'Requires the target-scoped automation.manage permission';
	let skipReason = $state<string>('');

	const sensor = $derived<SensorSummary | null>(
		sensors.payload?.sensors.find((candidate) => candidate.name === sensorName) ?? null
	);
	const letters = $derived(
		sensors.deadLetters.filter((letter) => letter.sensorName === sensorName)
	);

	const FACT_TONES: Record<string, 'success' | 'warning' | 'error'> = {
		succeeded: 'success',
		skipped: 'warning',
		failed: 'error',
		dead_lettered: 'error',
		started: 'warning'
	};

	function sourceLabel(current: SensorSummary): string {
		if (current.kind === 'polling') {
			return `poll every ${current.minimumIntervalSeconds ?? 0}s`;
		}
		return current.eventType ?? 'event';
	}
</script>

{#snippet caption(text: string)}
	<div class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]">
		{text}
	</div>
{/snippet}

<AppTopbar title={sensorName} breadcrumb="Sensors" />

<div class="min-h-0 flex-1 overflow-y-auto px-[18px] py-4">
	<div class="max-w-[880px] space-y-4">
		<a
			class="text-muted-foreground hover:text-foreground flex w-fit items-center gap-1.5 font-mono text-[11px]"
			href="{base}/sensors"
		>
			<ArrowLeftIcon size={12} />
			all sensors
		</a>
		{#if sensors.error !== null}
			<div class="font-mono text-[11px]" style:color="var(--sb-error)">{sensors.error}</div>
		{/if}
		{#if sensors.actionError !== null}
			<div
				class="font-mono text-[11px]"
				style:color="var(--sb-error)"
				data-testid="sensor-action-error"
				role="alert"
			>
				{sensors.actionError}
			</div>
		{/if}
		{#if sensors.actionMessage !== null}
			<div
				class="font-mono text-[11px]"
				style:color="var(--sb-success)"
				data-testid="sensor-action-message"
				role="status"
			>
				{sensors.actionMessage}
			</div>
		{/if}
		{#if sensor !== null}
			<div class="rounded-[4px] border border-border p-4">
				<div class="flex items-center gap-3 pb-1">
					<button
						class="relative h-[18px] w-[32px] shrink-0 rounded-full transition-colors disabled:opacity-50"
						style:background={sensor.effectiveStatus === 'running'
							? 'var(--sb-success)'
							: 'var(--border-strong)'}
						disabled={sensors.busy || !manageAllowed}
						onclick={() =>
							void sensors.setStatus(
								sensor.name,
								sensor.effectiveStatus === 'running' ? 'stopped' : 'running'
							)}
						title={manageAllowed
							? sensor.effectiveStatus === 'running'
								? 'Stop'
								: 'Start'
							: manageHint}
						aria-label="Toggle {sensor.name}"
					>
						<span
							class="absolute top-[2px] h-[14px] w-[14px] rounded-full bg-white transition-all"
							style:left={sensor.effectiveStatus === 'running' ? '16px' : '2px'}
						></span>
					</button>
					<span class="font-mono text-[13px]">{sensor.effectiveStatus}</span>
					{#if sensor.override !== null}
						<span class="text-[var(--sb-text-faint)] text-[10.5px]">(override)</span>
						<button
							class="text-[var(--sb-text-faint)] hover:text-foreground rounded-[4px] border border-border px-1.5 py-[1px] font-mono text-[10px] disabled:opacity-50"
							disabled={sensors.busy || !manageAllowed}
							title={manageAllowed ? 'Return to the status declared in code' : manageHint}
							onclick={() => void sensors.setStatus(sensor.name, 'declared_in_code')}
						>
							Reset
						</button>
					{/if}
				</div>
				{#if sensor.description}
					<p class="text-muted-foreground pb-1 text-[12px]">{sensor.description}</p>
				{/if}
				<FactRow label="reacts to" value={sourceLabel(sensor)} mono />
				{#if sensor.lastTick !== null}
					<FactRow
						label="latest tick"
						value={`${sensor.lastTick.status} · ${formatTimestamp(sensor.lastTick.startedAt)}`}
						tone={FACT_TONES[sensor.lastTick.status] ?? 'default'}
						mono
					/>
				{:else}
					<FactRow label="latest tick" value="never" tone="faint" mono />
				{/if}
				<FactRow
					label="retry policy"
					value={`${sensor.retryPolicy.maxAttempts} attempts / ${sensor.retryPolicy.backoffSeconds}s backoff`}
					mono
				/>
				<FactRow label="timeout" value={`${sensor.timeoutSeconds}s`} mono />
				<FactRow label="declared in" value={sensor.file} mono />
				<FactRow label="declared status" value={sensor.defaultStatus} mono />
			</div>

			{#if letters.length > 0}
				<div class="rounded-[4px] border border-border p-4">
					<div class="flex items-center gap-2">
						{@render caption('Dead letters')}
						<span class="text-[var(--sb-text-faint)] font-mono text-[10px]">{letters.length}</span>
						<input
							class="ml-auto h-6 w-48 rounded-[4px] border border-border bg-background px-2 font-mono text-[10.5px] outline-none focus:border-[var(--primary)]"
							placeholder="skip reason"
							bind:value={skipReason}
							aria-label="Skip reason"
						/>
					</div>
					<div class="text-[var(--sb-text-faint)] max-w-[640px] pb-1.5 pt-0.5 text-[10.5px]">
						Each event below exhausted its automatic retries and will not retry again on its own.
						Later events continue normally. Retry runs the handler now without repeating completed
						steps; Skip records your reason and resolves the event without running it.
					</div>
					{#each letters as letter (letter.tickId)}
						{@const retrying = sensors.pendingDeadLetterAction?.eventId === letter.eventId && sensors.pendingDeadLetterAction.type === 'retry'}
						{@const skipping = sensors.pendingDeadLetterAction?.eventId === letter.eventId && sensors.pendingDeadLetterAction.type === 'skip'}
						<div
							class="flex items-center gap-3 py-[3px] font-mono text-[10.5px]"
							data-testid={`dead-letter-${letter.eventId}`}
						>
							<span class="text-[var(--sb-text-faint)] w-44 shrink-0 truncate" title={letter.eventId}
								>{letter.eventId}</span
							>
							<span
								class="min-w-0 flex-1 truncate"
								style:color="var(--sb-error)"
								title={letter.errorMessage ?? undefined}
							>
								{letter.errorMessage ?? ''}
							</span>
							<button
								class="text-muted-foreground hover:text-foreground hover:bg-[var(--sb-hover)] rounded-[4px] border border-border px-2 py-[2px] disabled:opacity-50"
								disabled={sensors.busy || !manageAllowed}
								aria-busy={retrying}
								title={manageAllowed ? undefined : manageHint}
								onclick={() =>
									void sensors.retryDeadLetter(letter.sensorName, letter.eventId ?? '')}
							>
								{retrying ? 'Retrying…' : 'Retry'}
							</button>
							<button
								class="text-muted-foreground hover:text-foreground hover:bg-[var(--sb-hover)] rounded-[4px] border border-border px-2 py-[2px] disabled:opacity-50"
								disabled={sensors.busy || !manageAllowed || skipReason.trim() === ''}
								aria-busy={skipping}
								title={manageAllowed ? 'Requires a skip reason' : manageHint}
								onclick={() =>
									void sensors.skipDeadLetter(letter.sensorName, letter.eventId ?? '', skipReason)}
							>
								{skipping ? 'Skipping…' : 'Skip'}
							</button>
						</div>
					{/each}
				</div>
			{/if}

			<div class="rounded-[4px] border border-border p-4" data-testid="sensor-ticks">
				{@render caption('Tick history')}
				{#if sensors.ticks.length === 0}
					<div class="text-[var(--sb-text-faint)] pt-1.5 font-mono text-[11px]">
						No ticks recorded yet.
					</div>
				{:else}
					<TickTimeline {sensorName} seedTicks={sensors.ticks} />
					<div class="pt-2.5">
						{#each sensors.ticks as tick (tick.tickId)}
							<div class="flex gap-3 border-b border-[var(--border-subtle)] py-[5px] font-mono text-[10.5px] last:border-b-0">
								<span class="w-24 shrink-0" style:color={tickTone(tick.status)}>{tick.status}</span>
								<span class="text-[var(--sb-text-faint)] w-16 shrink-0">attempt {tick.attempt}</span>
								<span class="text-[var(--sb-text-faint)] w-36 shrink-0"
									>{formatTimestamp(tick.startedAt)}</span
								>
								<span class="text-muted-foreground min-w-0 flex-1 truncate" title={describeTick(tick)}
									>{describeTick(tick)}</span
								>
							</div>
						{/each}
					</div>
				{/if}
			</div>
		{:else if sensors.payload !== null}
			<div class="text-[var(--sb-text-faint)] font-mono text-[11px]">
				No sensor named {sensorName} is defined in this project.
			</div>
		{:else if sensors.loading}
			<div class="text-[var(--sb-text-faint)] font-mono text-[11px]">loading…</div>
		{/if}
	</div>
</div>
