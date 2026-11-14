<!-- The real engine's own answers, refreshed every two seconds: what the consumer query returns now, which version it reads, and how many rows each version holds. -->
<script lang="ts">
	import { base } from '$app/paths';
	import { recordedDeploymentIds } from '../_api/recorded-deployments';
	import { getDemo } from '../main/get-demo';
	import type { DemoEngine, DemoState } from '../types';

	type Props = { engine: DemoEngine };
	let { engine }: Props = $props();

	type Order = { order_id: string; status: string; total_cents: string };
	type Snapshot = { orders: Order[]; total: number; reads: string; versions: { id: string; rows: number }[] };

	const demo: DemoState = getDemo().demo;
	const { published, staged }: { published: string; staged: string } = recordedDeploymentIds();

	// Docked beside the page, the panel leaves narrower screens too little room, so it starts as a
	// rail there; a promotion opens it to show the switch.
	let open = $state<boolean>(globalThis.innerWidth >= 1600);
	$effect(() => {
		if (demo.promoted) open = true;
	});
	let adding = $state<boolean>(true);
	let snapshot = $state<Snapshot | null>(null);
	let error = $state<string | null>(null);

	async function refresh(): Promise<void> {
		const database: string = engine.database;
		try {
			const [orders, [{ total }], [view], sizes] = await Promise.all([
				engine.rows<Order>(
					`SELECT order_id, status, total_cents FROM ${database}.tbl__order_report ORDER BY order_id DESC LIMIT 5`
				),
				engine.rows<{ total: string }>(`SELECT count() AS total FROM ${database}.tbl__order_report`),
				engine.rows<{ query: string }>(
					`SELECT as_select AS query FROM system.tables WHERE database = '${database}' AND name = 'tbl__order_report'`
				),
				engine.tableSizes()
			]);
			snapshot = {
				orders,
				total: Number(total),
				reads: [published, staged].find((id) => view?.query.includes(id)) ?? 'unknown',
				versions: [published, staged].map((id) => ({ id, rows: sizes.get(`tbl__order_report__${id}`)?.rows ?? 0 }))
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

	const dollars = (cents: string): string => `$${(Number(cents) / 100).toFixed(2)}`;
	const label = (id: string): string => (id === staged ? 'completed orders only' : 'all orders');
	const versionName = (id: string): string =>
		id === staged ? (demo.promoted ? 'Current' : 'New') : demo.promoted ? 'Previous' : 'Current';
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
					Reads the {snapshot.reads === staged ? 'new' : 'current'} version ({label(snapshot.reads)}):
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
					{#each snapshot.versions as version (version.id)}
						<div class="flex justify-between gap-2">
							<span>{versionName(version.id)} version ({label(version.id)})</span>
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
			{#if !demo.promoted}
				<div class="text-muted-foreground">
					Promote the staged deployment on <a class="text-primary hover:underline" href="{base}/deployments/{staged}">its page</a> to switch the query to the new version.
				</div>
			{/if}
		</div>
	</aside>
{:else}
	<aside class="shrink-0 border-l border-border px-1 py-2 font-mono text-[11px]" aria-label="Live engine">
		<button type="button" class="text-primary hover:underline [writing-mode:vertical-rl]" aria-expanded={open} onclick={() => (open = true)}>
			Show live engine
		</button>
	</aside>
{/if}
