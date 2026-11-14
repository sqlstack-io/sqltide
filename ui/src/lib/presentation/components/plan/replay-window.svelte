<script lang="ts">
	import SpanTrack from '$lib/presentation/components/span-track.svelte';
	import ReplayDateTimePicker from '$lib/presentation/components/plan/replay-date-time-picker.svelte';
	import { clamp } from '$lib/formatting/main/clamp';
	import { formatCompact } from '$lib/formatting/main/format-compact';
	import { formatDaySpan } from '$lib/formatting/main/format-day-span';
	import { formatTimestamp } from '$lib/formatting/main/format-timestamp';
	import { parseUtc } from '$lib/formatting/main/parse-utc';
	import type { Project, Source } from '$lib/domain/types';
	import type { ReplayWindow } from '$lib/planning/types';

	type Props = {
		project: Project;
		/** Sources that root the current rebuild closure. */
		sources: Source[];
		window: ReplayWindow;
		/** The CLI only accepts --start-time alongside at least one --select. */
		selectionSpecified: boolean;
		/**
		 * Rows the replay will read, counted server-side at plan time with the same
		 * predicate the build uses. A fact, not an estimate — which is why there is
		 * deliberately no seconds figure next to it.
		 */
		rowsToReplay: number | null;
		onchange: (next: ReplayWindow) => void;
	};
	type ReplayPreset = {
		label: string;
		description: string;
		milliseconds: number;
	};
	const replayPresets: ReplayPreset[] = [
		{ label: '1h', description: '1 hour', milliseconds: 3_600_000 },
		{ label: '6h', description: '6 hours', milliseconds: 6 * 3_600_000 },
		{ label: '1d', description: '1 day', milliseconds: 86_400_000 },
		{ label: '3d', description: '3 days', milliseconds: 3 * 86_400_000 },
		{ label: '7d', description: '7 days', milliseconds: 7 * 86_400_000 }
	];
	let {
		project,
		sources,
		window: replayWindow,
		selectionSpecified,
		rowsToReplay,
		onchange
	}: Props = $props();

	// --start-time is the WORST flag to type and the BEST to render: its valid range
	// is fully bounded by data we already hold. Typed into a shell it fails at build
	// time; drawn against the retention track it fails before you commit.
	//
	// Framing is deliberately calm. Model tables are disposable derivations of the
	// source, so every direct build is ALREADY bounded by source retention — this
	// narrows the window inside an already-bounded system. It is a cost/time
	// control, not a new hazard. The durability statement lives on Sources.

	/**
	 * The earliest addressable instant across all rooting sources. Deliberately the
	 * MIN, not the max: --start-time is a global cutoff and each source replays from
	 * max(start, its own oldest), so clamping to the most-constrained source would
	 * stop you asking for history the other sources still hold.
	 */
	const retainedStartMilliseconds = $derived.by((): number | null => {
		const candidates: number[] = sources
			.map((source) => source.live.oldestEventAt)
			.filter((instant): instant is string => Boolean(instant))
			.map((instant) => parseUtc(instant).getTime())
			.filter((milliseconds) => Number.isFinite(milliseconds));
		return candidates.length === 0 ? null : Math.min(...candidates);
	});
	const boundToMilliseconds = $derived(parseUtc(project.capturedAt).getTime());
	const hasRetentionWindow = $derived(
		retainedStartMilliseconds !== null &&
			Number.isFinite(boundToMilliseconds) &&
			retainedStartMilliseconds < boundToMilliseconds
	);
	const boundFrom = $derived(
		retainedStartMilliseconds === null
			? project.capturedAt
			: new Date(retainedStartMilliseconds).toISOString()
	);
	const boundTo = $derived(project.capturedAt);
	const totalMs = $derived(
		Math.max(
			boundToMilliseconds - (retainedStartMilliseconds ?? boundToMilliseconds),
			1
		)
	);
	const canSetStartTime = $derived(selectionSpecified && hasRetentionWindow);
	const requestedStartMilliseconds = $derived(
		replayWindow.mode === 'from' ? parseUtc(replayWindow.startTime).getTime() : NaN
	);
	const effectiveStartMilliseconds = $derived.by((): number => {
		if (!Number.isFinite(requestedStartMilliseconds)) {
			return retainedStartMilliseconds ?? boundToMilliseconds;
		}
		return clamp(
			requestedStartMilliseconds,
			retainedStartMilliseconds ?? requestedStartMilliseconds,
			boundToMilliseconds
		);
	});
	const startTime = $derived(
		replayWindow.mode === 'from'
			? new Date(effectiveStartMilliseconds).toISOString()
			: boundFrom
	);

	$effect(() => {
		if (
			replayWindow.mode === 'from' &&
			canSetStartTime &&
			Math.floor(requestedStartMilliseconds / 1000) !==
				Math.floor(effectiveStartMilliseconds / 1000)
		) {
			onchange({ mode: 'from', startTime });
		}
	});

	/** Sources that cannot reach back as far as the chosen cutoff. */
	const shortSources = $derived.by((): Source[] => {
		const startMilliseconds: number = parseUtc(startTime).getTime();
		return sources.filter((source) => {
			if (!source.live.oldestEventAt) return false;
			const oldestMilliseconds: number = parseUtc(source.live.oldestEventAt).getTime();
			return Number.isFinite(oldestMilliseconds) && oldestMilliseconds > startMilliseconds;
		});
	});

	/** Slider position as a 0–1000 integer over the retention window. */
	const sliderValue = $derived(
		Math.round(
			clamp(
				(parseUtc(startTime).getTime() - parseUtc(boundFrom).getTime()) / totalMs,
				0,
				1
			) * 1000
		)
	);

	function setFromSlider(value: number): void {
		const instant: Date = new Date(
			parseUtc(boundFrom).getTime() + (clamp(value, 0, 1000) / 1000) * totalMs
		);
		// Snap to the minute — sub-minute precision is noise for a replay boundary.
		instant.setUTCSeconds(0, 0);
		onchange({ mode: 'from', startTime: instant.toISOString() });
	}

	function previewSlider(input: HTMLInputElement): void {
		input.style.setProperty(
			'--replay-start-position',
			`${clamp(Number(input.value), 0, 1000) / 10}%`
		);
	}

	function presetStartMilliseconds(milliseconds: number): number {
		return clamp(
			boundToMilliseconds - milliseconds,
			retainedStartMilliseconds ?? boundToMilliseconds,
			boundToMilliseconds
		);
	}

	function setFromPreset(milliseconds: number): void {
		onchange({
			mode: 'from',
			startTime: new Date(presetStartMilliseconds(milliseconds)).toISOString()
		});
	}

	function isSelectedPreset(milliseconds: number): boolean {
		return (
			milliseconds <= totalMs + 60_000 &&
			Math.abs(effectiveStartMilliseconds - presetStartMilliseconds(milliseconds)) < 60_000
		);
	}

	function enableStartTime(): void {
		if (!canSetStartTime) return;
		onchange({
			mode: 'from',
			startTime: new Date(
				boundToMilliseconds - Math.min(totalMs, 5.5 * 86_400_000)
			).toISOString()
		});
	}

	const skippedDays = $derived(
		(parseUtc(startTime).getTime() - parseUtc(boundFrom).getTime()) / 86_400_000
	);
	const replayedDays = $derived(
		(parseUtc(boundTo).getTime() - parseUtc(startTime).getTime()) / 86_400_000
	);

	const totalRetainedRows = $derived(
		sources.reduce((sum, source) => sum + source.live.rows, 0)
	);
</script>

<div class="rounded-[4px] border border-border">
	<div class="flex items-center gap-3 border-b border-border px-3 py-2.5">
		<span
			class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]"
			>Replay window</span
		>
		<div class="ml-auto flex overflow-hidden rounded-[4px] border border-border">
			<button
				class="px-2.5 py-1 font-mono text-[10.5px] transition-colors {replayWindow.mode === 'full'
					? 'bg-[var(--sb-hover)] text-foreground'
					: 'text-muted-foreground hover:text-foreground'}"
				onclick={() => onchange({ mode: 'full' })}
			>
				Full retained
			</button>
			<button
				class="border-l border-border px-2.5 py-1 font-mono text-[10.5px] transition-colors {replayWindow.mode ===
				'from'
					? 'bg-[var(--sb-hover)] text-foreground'
					: 'text-muted-foreground hover:text-foreground'} disabled:cursor-not-allowed disabled:opacity-45"
				disabled={!canSetStartTime}
				title={!selectionSpecified
					? 'Choose at least one model or pipeline first'
					: !hasRetentionWindow
						? 'No retained source window is available yet'
						: undefined}
				onclick={enableStartTime}
			>
				From a time
			</button>
		</div>
	</div>

	<div class="flex flex-col gap-3 px-3 py-3">
		{#if sources.length === 0}
			<p class="text-muted-foreground text-[12px]">
				This selection has no rooting stream source, so there is nothing to replay.
			</p>
		{:else if !selectionSpecified}
			<p class="text-muted-foreground text-[12px]">
				Choose at least one model or pipeline to set a start time. SQLTide requires
				<code class="code">--start-time</code> to be scoped by <code class="code">--select</code>.
			</p>
		{:else if !hasRetentionWindow && import.meta.env.VITE_SQLTIDE_DEMO === '1'}
			<!-- The UI demo's orders table, like the basic playground's, records no landing times. -->
			<p class="text-muted-foreground text-[12px]" data-testid="demo-no-replay-window">
				This playground's orders table doesn't record when each order landed, so there is no retained
				window to pick a start time from. Run <code class="code">sqt playground --template streaming</code> to
				replay from a time with ClickHouse on your machine.
			</p>
		{:else if !hasRetentionWindow}
			<p class="text-muted-foreground text-[12px]">
				A retained source window is not available yet. Build the source first, then choose a
				bounded replay start time.
			</p>
		{:else}
			<!-- retention track, with the chosen cutoff drawn on it -->
			<div>
				<div class="text-muted-foreground flex items-baseline gap-2 pb-1 font-mono text-[10px]">
					<span
						>{sources.length === 1 ? sources[0].relationName : `${sources.length} sources`} retained</span
					>
					<span class="ml-auto">{formatDaySpan(totalMs / 86_400_000)}</span>
				</div>
				<SpanTrack
					domainFrom={boundFrom}
					domainTo={boundTo}
					height={22}
					markerAt={replayWindow.mode === 'from' ? startTime : null}
					markerLabel={formatTimestamp(startTime)}
					bands={replayWindow.mode === 'full'
						? [
								{
									from: boundFrom,
									to: boundTo,
									colour: 'var(--sb-secondary)',
									opacity: 0.55,
									label: 'will replay'
								}
							]
						: [
								{
									from: boundFrom,
									to: startTime,
									colour: 'transparent',
									hatch: true,
									label: 'skipped'
								},
								{
									from: startTime,
									to: boundTo,
									colour: 'var(--sb-secondary)',
									opacity: 0.55,
									label: 'will replay'
								}
							]}
				/>
				<div
					class="text-[var(--sb-text-faint)] flex justify-between pt-1 font-mono text-[10px]"
				>
					<span>{formatTimestamp(boundFrom)}</span>
					<span>{formatTimestamp(boundTo)}</span>
				</div>
			</div>

			{#if replayWindow.mode === 'from'}
				<!-- slider + calendar, mutually bound; both clamped to the retention window -->
				<div class="flex flex-col gap-3 rounded-[4px] border border-[var(--border-subtle)] p-2.5">
					<div>
						<div class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]">
							Quick range
						</div>
						<div class="grid grid-cols-5 gap-1">
							{#each replayPresets as preset (preset.label)}
								<button
									class="rounded-[3px] border px-1 py-1 font-mono text-[10.5px] transition-colors {isSelectedPreset(
										preset.milliseconds
									)
										? 'border-[var(--primary)] bg-[color-mix(in_srgb,var(--primary)_12%,transparent)] text-foreground'
										: 'border-border text-muted-foreground hover:bg-[var(--sb-hover)] hover:text-foreground'}"
									aria-label={`Replay the last ${preset.description}`}
									aria-pressed={isSelectedPreset(preset.milliseconds)}
									title={preset.milliseconds > totalMs
										? `${preset.description} exceeds retained history; the earliest retained time will be used`
										: `Replay the last ${preset.description}`}
									onclick={() => setFromPreset(preset.milliseconds)}
								>
									{preset.label}
								</button>
							{/each}
						</div>
					</div>
					<label class="block">
						<span class="text-[var(--sb-text-faint)] block font-mono text-[10px] uppercase tracking-[0.14em]">
							Start position
						</span>
						<input
							type="range"
							min="0"
							max="1000"
							value={sliderValue}
							class="replay-window-range mt-1.5 block w-full cursor-pointer"
							style:--replay-start-position={`${sliderValue / 10}%`}
							aria-label="Replay start time"
							oninput={(event) => previewSlider(event.currentTarget)}
							onchange={(event) => setFromSlider(Number(event.currentTarget.value))}
						/>
						<span
							class="text-[var(--sb-text-faint)] flex justify-between pt-0.5 font-mono text-[9.5px]"
							aria-hidden="true"
						>
							<span>more history</span>
							<span>less history</span>
						</span>
					</label>
					<ReplayDateTimePicker
						value={startTime}
						minimum={boundFrom}
						maximum={boundTo}
						timeZone={project.timeZone}
						onapply={(nextStartTime) => onchange({ mode: 'from', startTime: nextStartTime })}
					/>
				</div>

				<div class="grid grid-cols-2 gap-3">
					<div class="rounded-[3px] border border-[var(--border-subtle)] px-2.5 py-2">
						<div class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]">
							Skipped
						</div>
						<div class="text-muted-foreground pt-1 font-mono text-[12px]">
							{formatDaySpan(Math.max(skippedDays, 0))}
						</div>
					</div>
					<div class="rounded-[3px] border border-[var(--border-subtle)] px-2.5 py-2">
						<div class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]">
							Will replay
						</div>
						<div class="pt-1 font-mono text-[12px]" style:color="var(--sb-secondary)">
							{formatDaySpan(Math.max(replayedDays, 0))}
						</div>
					</div>
				</div>
			{/if}

			<!-- estimate -->
			{#if shortSources.length && sources.length > 1}
				<p class="text-[var(--sb-text-faint)] text-[11px] leading-snug">
					{shortSources.map((source) => source.name).join(', ')} replays from
					{shortSources.length === 1
						? formatTimestamp(shortSources[0].live.oldestEventAt)
						: 'its own earliest retained event'}
				</p>
			{/if}

			{#if rowsToReplay !== null}
				<div
					class="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-t border-[var(--border-subtle)] pt-2.5 font-mono text-[11px]"
				>
					<span class="text-muted-foreground"
						>{formatCompact(rowsToReplay)} rows of {formatCompact(totalRetainedRows)} retained</span
					>
					<span class="text-[var(--sb-text-faint)] ml-auto">counted at plan time</span>
				</div>
				{#if replayWindow.mode === 'from'}
					<p class="text-[var(--sb-text-faint)] text-[11px]">
						Resulting extent {formatTimestamp(startTime)} → now
					</p>
				{/if}
			{:else}
				<p class="text-[var(--sb-text-faint)] border-t border-[var(--border-subtle)] pt-2.5 text-[11px]">
					Exact replay rows are unavailable until every rooting source can be counted.
				</p>
			{/if}
		{/if}
	</div>
</div>

<style>
	.replay-window-range {
		height: 20px;
		appearance: none;
		background: transparent;
	}

	.replay-window-range:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
		border-radius: 4px;
	}

	.replay-window-range::-webkit-slider-runnable-track {
		height: 7px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: linear-gradient(
			to right,
			color-mix(in srgb, var(--muted-foreground) 28%, var(--sb-inset)) 0
				var(--replay-start-position),
			color-mix(in srgb, var(--sb-secondary) 72%, var(--sb-inset))
				var(--replay-start-position) 100%
		);
	}

	.replay-window-range::-webkit-slider-thumb {
		width: 17px;
		height: 17px;
		margin-top: -6px;
		appearance: none;
		border: 2px solid var(--background);
		border-radius: 999px;
		background: var(--primary);
		box-shadow: 0 0 0 1px var(--primary);
	}

	.replay-window-range::-moz-range-track {
		height: 7px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: linear-gradient(
			to right,
			color-mix(in srgb, var(--muted-foreground) 28%, var(--sb-inset)) 0
				var(--replay-start-position),
			color-mix(in srgb, var(--sb-secondary) 72%, var(--sb-inset))
				var(--replay-start-position) 100%
		);
	}

	.replay-window-range::-moz-range-thumb {
		width: 13px;
		height: 13px;
		border: 2px solid var(--background);
		border-radius: 999px;
		background: var(--primary);
		box-shadow: 0 0 0 1px var(--primary);
	}
</style>
