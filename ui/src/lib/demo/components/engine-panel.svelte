<!-- The real engine's own answers, refreshed every two seconds: what the consumer query returns now, which version it reads, and how many rows each version holds. -->
<script lang="ts">
	import { base } from '$app/paths';
	import type { DemoEngine, LiveDemo } from '../types';

	/** `onrestarted` takes the visitor to where the story starts once the engine is back there. */
	type Props = { live: LiveDemo; onrestarted: () => Promise<void> };
	let { live, onrestarted }: Props = $props();
	const engine: DemoEngine = $derived(live.engine);

	type Order = { order_id: string; status: string; total_cents: string };
	type Version = { name: string; rows: string };
	type Snapshot = { orders: Order[]; total: number; reads: string | null; versions: { id: string; rows: number }[] };

	const PHYSICAL_PREFIX: string = 'tbl__order_report__';

	// Docked beside the page, the panel leaves narrower screens too little room, so it starts as a rail there.
	let open = $state<boolean>(globalThis.innerWidth >= 1600);
	let adding = $state<boolean>(true);
	let snapshot = $state<Snapshot | null>(null);
	let error = $state<string | null>(null);

	async function refresh(): Promise<void> {
		const database: string = engine.database;
		try {
			// The engine runs queries in the order they arrive, and an action's statements can land
			// between them, so the view is read first: the rows then come from the version it names.
			const [[view], versions, orders, [{ total }]] = await Promise.all([
				engine.rows<{ query: string }>(
					`SELECT as_select AS query FROM system.tables WHERE database = '${database}' AND name = 'tbl__order_report'`
				),
				engine.rows<Version>(
					`SELECT name, total_rows AS rows FROM system.tables WHERE database = '${database}' AND startsWith(name, '${PHYSICAL_PREFIX}') AND engine != 'View' ORDER BY name`
				),
				engine.rows<Order>(
					`SELECT order_id, status, total_cents FROM ${database}.tbl__order_report ORDER BY order_id DESC LIMIT 5`
				),
				engine.rows<{ total: string }>(`SELECT count() AS total FROM ${database}.tbl__order_report`)
			]);
			const ids: string[] = versions.map((version) => version.name.slice(PHYSICAL_PREFIX.length));
			snapshot = {
				orders,
				total: Number(total),
				reads: ids.find((id) => view?.query.includes(`${PHYSICAL_PREFIX}${id}`)) ?? null,
				versions: versions.map((version, index) => ({ id: ids[index], rows: Number(version.rows ?? 0) }))
			};
			error = null;
		} catch (caught) {
			error = caught instanceof Error ? caught.message : String(caught);
		}
	}

	$effect(() => {
		void refresh();
		const timer: ReturnType<typeof setInterval> = setInterval(() => void refresh(), 2000);
		return () => clearInterval(timer);
	});

	async function toggleOrders(): Promise<void> {
		if (engine.addingOrders) engine.stopOrders();
		else await engine.startOrders();
		adding = engine.addingOrders;
	}

	let restarting = $state<boolean>(false);

	/** Puts the engine back where the story starts, without downloading it again. */
	async function startOver(): Promise<void> {
		restarting = true;
		error = null;
		engine.stopOrders();
		try {
			await live.story.startOver();
			await engine.startOrders();
			adding = true;
			await onrestarted();
		} catch (caught) {
			error = caught instanceof Error ? caught.message : String(caught);
			adding = engine.addingOrders;
		} finally {
			restarting = false;
		}
	}

	const dollars = (cents: string): string => `$${(Number(cents) / 100).toFixed(2)}`;
</script>

{#if open}
	<aside
		class="flex w-[320px] shrink-0 flex-col overflow-auto border-l border-border font-mono text-[11px]"
		aria-label="Live engine"
	>
		<header class="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
			<span class="font-semibold">Live in this tab</span>
			<button type="button" class="text-primary hover:underline" aria-expanded={open} onclick={() => (open = false)}>Hide</button>
		</header>
		<div class="flex flex-col gap-2 px-3 py-2">
			<div>
				<div class="text-muted-foreground">Your query, unchanged:</div>
				<code class="code block">SELECT order_id, status, total_cents FROM tbl__order_report</code>
			</div>
			{#if snapshot !== null}
				<div data-testid="engine-reads">
					Reads deployment {#if snapshot.reads === null}unknown{:else}<a class="text-primary hover:underline" href="{base}/deployments/{snapshot.reads}">{snapshot.reads}</a>{/if}:
					<strong>{snapshot.total} rows</strong>
				</div>
				<table class="w-full text-left">
					<tbody>
						{#each snapshot.orders as order (order.order_id)}
							<tr><td>#{order.order_id}</td><td>{order.status}</td><td class="text-right">{dollars(order.total_cents)}</td></tr>
						{/each}
					</tbody>
				</table>
				<div class="border-t border-border pt-2">
					<div class="text-muted-foreground">Versions of tbl__order_report:</div>
					{#each snapshot.versions as version (version.id)}
						<div class="flex justify-between gap-2" data-testid="engine-version">
							<span class:font-semibold={version.id === snapshot.reads}>{version.id}{version.id === snapshot.reads ? ' (read now)' : ''}</span>
							<span class="shrink-0 whitespace-nowrap">{version.rows} rows</span>
						</div>
					{/each}
				</div>
			{:else if error === null}
				<div class="text-muted-foreground">Reading the tables…</div>
			{/if}
			{#if error !== null}
				<div style:color="var(--sb-error)">{error}</div>
			{/if}
			<div class="flex items-center justify-between gap-2 border-t border-border pt-2">
				<span class="text-muted-foreground">{adding ? 'A new order arrives every 4 s.' : 'New orders are paused.'}</span>
				<button type="button" class="text-primary hover:underline" onclick={toggleOrders}>{adding ? 'Pause' : 'Resume'}</button>
			</div>
			<div class="flex items-center justify-between gap-2 border-t border-border pt-2">
				<span class="text-muted-foreground">Back to the published pipeline and the staged change, unbuilt.</span>
				<button type="button" class="text-primary shrink-0 hover:underline disabled:opacity-50" disabled={restarting} onclick={startOver}>
					{restarting ? 'Starting over…' : 'Start over'}
				</button>
			</div>
			<div class="text-muted-foreground">
				Execute a plan on the <a class="text-primary hover:underline" href="{base}/plan">Plan page</a>, then promote or roll back on
				<a class="text-primary hover:underline" href="{base}/deployments">Deployments</a>, and watch which version the query reads.
			</div>
		</div>
	</aside>
{:else}
	<aside class="shrink-0 border-l border-border px-1 py-2 font-mono text-[11px]" aria-label="Live engine">
		<button type="button" class="text-primary hover:underline [writing-mode:vertical-rl]" aria-expanded={open} onclick={() => (open = true)}>
			Show live engine
		</button>
	</aside>
{/if}
