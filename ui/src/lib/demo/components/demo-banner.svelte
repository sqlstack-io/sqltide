<!-- Shown on every page of the UI demo build: where the demo's answers come from. The docs live at the website root, outside the demo's base path. -->
<script lang="ts">
	import { RECORDING_CHOICE_KEY } from '../constants';
	import { getDemo } from '../main/get-demo';
	import type { DemoController, DemoState, EngineProgress } from '../types';

	const controller: DemoController = getDemo();
	const demo: DemoState = controller.demo;

	function useRecording(): void {
		sessionStorage.setItem(RECORDING_CHOICE_KEY, '1');
		location.reload();
	}

	function continueWithRecording(): void {
		controller.useRecording('The real engine couldn’t start here, so this is the recording.');
	}

	function useEngine(): void {
		sessionStorage.removeItem(RECORDING_CHOICE_KEY);
		location.reload();
	}

	const megabytes = (bytes: number): string => (bytes / 1_000_000).toFixed(1);

	function describe(progress: EngineProgress | null): { label: string; fraction: number } {
		if (progress === null) return { label: 'Preparing the engine…', fraction: 0 };
		if (progress.phase === 'download') {
			return {
				label: `Downloading the engine: ${megabytes(progress.loaded)} of ${megabytes(progress.total)} MB`,
				fraction: 0.8 * (progress.total === 0 ? 0 : progress.loaded / progress.total)
			};
		}
		if (progress.phase === 'start') return { label: 'Starting ClickHouse…', fraction: 0.82 };
		return {
			label: `Setting up the playground: ${progress.step}`,
			fraction: 0.85 + 0.15 * (progress.total === 0 ? 1 : progress.done / progress.total)
		};
	}

	const loading = $derived(demo.mode.kind === 'real' && demo.mode.live === null ? describe(demo.mode.progress) : null);
</script>

<div
	class="flex shrink-0 flex-wrap items-center justify-center gap-x-2 gap-y-0.5 border-b border-border bg-[var(--sb-surface-low)] px-4 py-1.5 text-center font-mono text-[11px]"
	role="note"
>
	{#if demo.mode.kind === 'recorded' || demo.mode.kind === 'failed'}
		<span>UI demo with recorded example data. It is read-only.</span>
		{#if demo.mode.kind === 'recorded' && demo.mode.reason}
			<span class="text-muted-foreground">{demo.mode.reason}</span>
			{#if demo.mode.reason === 'You chose the recording.'}
				<button type="button" class="text-primary hover:underline" onclick={useEngine}>Run the real engine</button>
			{/if}
		{/if}
		<span class="text-muted-foreground">
			Run <code class="code">sqt playground</code> to try SQLTide with ClickHouse on your machine.
		</span>
		<a href="/docs/cli/playground/" class="text-primary hover:underline">How the playground works →</a>
	{:else if loading !== null}
		<span>Starting a real ClickHouse engine in this tab.</span>
		<span class="text-muted-foreground" aria-live="polite">{loading.label}</span>
		<span
			class="h-1.5 w-40 overflow-hidden rounded-full bg-[var(--sb-surface-high)]"
			role="progressbar"
			aria-label="Engine loading"
			aria-valuemin="0"
			aria-valuemax="100"
			aria-valuenow={Math.round(loading.fraction * 100)}
		>
			<span class="bg-primary block h-full transition-[width]" style:width="{loading.fraction * 100}%"></span>
		</span>
		<button type="button" class="text-primary hover:underline" onclick={useRecording}>Use the recording instead</button>
	{:else}
		<span>Real ClickHouse running in this tab: Execute, Promote and Roll back run their SQL on it, and every page reads it.</span>
		<span class="text-muted-foreground">
			Worked out ahead by sqt: each plan, its SQL diff, and the SQL each action runs. The switch to a new version isn’t atomic in this engine, which renames tables where ClickHouse exchanges them.
		</span>
		<button type="button" class="text-primary hover:underline" onclick={useRecording}>Use the recording</button>
	{/if}
</div>

{#if demo.mode.kind === 'failed'}
	<div class="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4" role="presentation">
		<div
			class="bg-background w-full max-w-md rounded-md border border-border p-5 font-mono text-[12px] shadow-lg"
			role="alertdialog"
			aria-modal="true"
			aria-labelledby="demo-engine-failed-title"
			aria-describedby="demo-engine-failed-error"
		>
			<div id="demo-engine-failed-title" class="font-display text-[15px] font-semibold">The real engine couldn’t start</div>
			<p class="text-muted-foreground mt-2">This demo runs ClickHouse in your browser, and it failed here:</p>
			<pre id="demo-engine-failed-error" class="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-[var(--sb-surface-low)] p-2 text-[11px]">{demo.mode.error}</pre>
			<p class="text-muted-foreground mt-3">You can watch the same story from a recording instead.</p>
			<div class="mt-4 flex justify-end gap-2">
				<button type="button" class="rounded border border-border px-3 py-1.5 hover:bg-[var(--sb-hover)]" onclick={() => location.reload()}>Try again</button>
				<button type="button" class="bg-primary text-primary-foreground rounded px-3 py-1.5" onclick={continueWithRecording}>Use the recording</button>
			</div>
		</div>
	</div>
{/if}
