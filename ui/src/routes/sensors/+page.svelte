<script lang="ts">
	import { base } from '$app/paths';
	import RadioIcon from '@lucide/svelte/icons/radio';
	import TimerIcon from '@lucide/svelte/icons/timer';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import { can } from '$lib/auth/main/can';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';
	import { createSensorsState } from '$lib/sensor-automation/main/create-sensors-state.svelte';
	import { tickTone } from '$lib/sensor-automation/main/tick-tone';
	import type { SensorSummary } from '$lib/sensor-automation/types';

	const sensors = createSensorsState();

	$effect(() => sensors.start());

	const manageAllowed = $derived(can('automation.manage'));
	const manageHint = 'Requires the target-scoped automation.manage permission';

	function sourceLabel(sensor: SensorSummary): string {
		if (sensor.kind === 'polling') {
			return `every ${sensor.minimumIntervalSeconds ?? 0}s`;
		}
		return sensor.eventType ?? 'event';
	}

	function nextStatus(sensor: SensorSummary): string {
		return sensor.effectiveStatus === 'running' ? 'stopped' : 'running';
	}

	const runningCount = $derived(
		(sensors.payload?.sensors ?? []).filter((sensor) => sensor.effectiveStatus === 'running')
			.length
	);
</script>

<AppTopbar title="Sensors" />

{#if sensors.payload !== null}
	<div class="flex items-center gap-2.5 border-b border-border px-4 py-2.5" data-testid="sensor-health">
		<span class="flex items-center gap-1.5 font-mono text-[11px]">
			<span
				class="h-[7px] w-[7px] rounded-full"
				style:background={sensors.payload.health.latestError
					? 'var(--sb-error)'
					: 'var(--sb-success)'}
			></span>
			dispatcher {sensors.payload.health.state}
		</span>
		{#if sensors.payload.health.latestError}
			<span
				class="max-w-[380px] truncate font-mono text-[10.5px]"
				style:color="var(--sb-error)"
				title={sensors.payload.health.latestError}
			>
				{sensors.payload.health.latestError}
			</span>
		{/if}
		{#if sensors.actionError !== null}
			<span
				class="max-w-[380px] truncate font-mono text-[10.5px]"
				style:color="var(--sb-error)"
				data-testid="sensor-action-error"
			>
				{sensors.actionError}
			</span>
		{/if}
		<div class="text-[var(--sb-text-faint)] ml-auto flex items-center gap-3 font-mono text-[11px]">
			<span>{sensors.payload.sensors.length} sensors</span>
			<span>·</span>
			<span style="color:var(--sb-success)">{runningCount} running</span>
			{#if sensors.payload.deadLetterCount > 0}
				<span>·</span>
				<span style="color:var(--sb-error)">
					{sensors.payload.deadLetterCount}
					{sensors.payload.deadLetterCount === 1 ? 'dead letter' : 'dead letters'}
				</span>
			{/if}
		</div>
	</div>
{/if}

<div class="min-h-0 flex-1 overflow-y-auto">
	{#if sensors.error !== null}
		<div class="px-4 py-3 font-mono text-[11px]" style:color="var(--sb-error)">
			{sensors.error}
		</div>
	{/if}
	{#if sensors.payload !== null}
		<table class="sb-list w-full">
			<thead class="sticky top-0 z-10 bg-[var(--background)]">
				<tr>
					{#each ['Sensor', 'Reacts to', 'Status', 'Last tick', 'Retry'] as heading (heading)}
						<th
							class="text-[var(--sb-text-faint)] border-b border-border px-4 py-2.5 text-left font-mono text-[10px] font-medium uppercase tracking-[0.1em]"
							>{heading}</th
						>
					{/each}
				</tr>
			</thead>
			<tbody>
				{#each sensors.payload.sensors as sensor (sensor.name)}
					<tr
						class="group"
						class:sb-row-off={sensor.effectiveStatus !== 'running'}
						data-testid={`sensor-row-${sensor.name}`}
					>
						<td class="px-4 py-3">
							<div class="flex items-center gap-3">
								<button
									class="relative h-[18px] w-[32px] shrink-0 rounded-full transition-colors disabled:opacity-50"
									style:background={sensor.effectiveStatus === 'running'
										? 'var(--sb-success)'
										: 'var(--border-strong)'}
									disabled={sensors.busy || !manageAllowed}
									onclick={() => void sensors.setStatus(sensor.name, nextStatus(sensor))}
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
								<a
									class="hover:text-primary text-foreground text-[13px] font-medium"
									href={`${base}/sensors/${sensor.name}`}
								>
									{sensor.name}
								</a>
							</div>
							{#if sensor.description}
								<div class="text-[var(--sb-text-faint)] mt-0.5 max-w-[420px] truncate pl-[44px] text-[10.5px]">
									{sensor.description}
								</div>
							{/if}
						</td>
						<td class="px-4 py-3">
							<span class="sb-tag">
								{#if sensor.kind === 'polling'}
									<TimerIcon size={11} class="sb-tag-ico" />
								{:else}
									<RadioIcon size={11} class="sb-tag-ico" />
								{/if}
								{sourceLabel(sensor)}
							</span>
						</td>
						<td class="px-4 py-3">
							<span class="font-mono text-[11.5px]">{sensor.effectiveStatus}</span>
							{#if sensor.override !== null}
								<span class="text-[var(--sb-text-faint)] text-[10.5px]"> (override)</span>
							{/if}
						</td>
						<td class="px-4 py-3">
							{#if sensor.lastTick !== null}
								<span class="sb-tag">
									<span
										class="h-1.5 w-1.5 rounded-[2px]"
										style:background={tickTone(sensor.lastTick.status)}
									></span>
									<span style:color={tickTone(sensor.lastTick.status)}
										>{sensor.lastTick.status}</span
									>
									<span class="text-[var(--sb-text-faint)]"
										>· {formatTimestamp(sensor.lastTick.startedAt)}</span
									>
								</span>
							{:else}
								<span class="text-[var(--sb-text-faint)] text-[11.5px]">never</span>
							{/if}
						</td>
						<td class="px-4 py-3">
							<span class="code text-[12px]"
								>{sensor.retryPolicy.maxAttempts}× / {sensor.retryPolicy.backoffSeconds}s</span
							>
						</td>
					</tr>
				{:else}
					<tr>
						<td colspan="5" class="text-[var(--sb-text-faint)] px-6 py-10 text-center font-mono text-[12px]">
							No sensors are defined; author them under sensors/ in the project.
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	{:else if sensors.loading}
		<div class="text-[var(--sb-text-faint)] px-4 py-3 font-mono text-[11px]">loading…</div>
	{/if}
</div>
