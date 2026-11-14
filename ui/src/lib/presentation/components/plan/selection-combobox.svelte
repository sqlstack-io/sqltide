<script lang="ts">
	import { tick } from 'svelte';
	import SearchIcon from '@lucide/svelte/icons/search';
	import XIcon from '@lucide/svelte/icons/x';
	import { selectorToken } from '$lib/planning/main/selector-token';
	import type { Project } from '$lib/domain/types';
	import type { Selector } from '$lib/planning/types';

	type Props = {
		id: string;
		labelledby: string;
		project: Project;
		selectors: Selector[];
		onchange: (next: Selector[]) => void;
	};
	let { id, labelledby, project, selectors, onchange }: Props = $props();

	// ONE control, not two. `main`'s run panel had an editable command line AND a
	// selector query input, both writing the same state — two controls fighting
	// over one value. Here the command string is a read-only receipt.
	//
	// A text-command input would also buy nothing: `plan`/`build` accept only a
	// bare model name or `pipeline:<name>`, and SQLTide has no tags, groups or
	// globs to query on. This is a finite checklist, so it gets a picker.
	let query = $state<string>('');
	let open = $state<boolean>(false);
	let activeToken = $state<string | null>(null);
	let rootElement: HTMLDivElement;
	let inputElement: HTMLInputElement;
	let listboxElement = $state<HTMLDivElement>();

	const listboxId = $derived(`${id}-listbox`);

	type Option = {
		selector: Selector;
		token: string;
		primary: string;
		secondary: string;
		group: 'Pipelines' | 'Models';
	};

	const allOptions = $derived.by((): Option[] => {
		const pipelines: Option[] = project.pipelines.map((pipeline) => ({
			selector: { kind: 'pipeline', name: pipeline.name },
			token: `pipeline:${pipeline.name}`,
			primary: `pipeline:${pipeline.name}`,
			secondary: `${pipeline.models.length} models`,
			group: 'Pipelines'
		}));
		const models: Option[] = project.models.map((model) => ({
			selector: { kind: 'model', name: model.name },
			token: model.name,
			primary: model.name,
			secondary: `${model.pipeline} · ${model.storage.engine ?? 'view'}`,
			group: 'Models'
		}));
		return [...pipelines, ...models];
	});

	const chosen = $derived(new Set(selectors.map(selectorToken)));

	const matches = $derived.by((): Option[] => {
		const needle: string = query.trim().toLowerCase();
		return allOptions
			.filter((option) => !chosen.has(option.token))
			.filter(
				(option) =>
					needle === '' ||
					option.primary.toLowerCase().includes(needle) ||
					option.secondary.toLowerCase().includes(needle)
			)
			.slice(0, 12);
	});
	const activeIndex = $derived(matches.findIndex((option) => option.token === activeToken));
	const activeOptionId = $derived(
		open && activeIndex >= 0 ? `${id}-option-${activeIndex}` : undefined
	);

	function close(): void {
		open = false;
		activeToken = null;
	}

	async function scrollActiveOptionIntoView(token: string): Promise<void> {
		await tick();
		if (activeToken !== token) return;
		listboxElement
			?.querySelector<HTMLElement>('[data-active="true"]')
			?.scrollIntoView({ block: 'nearest' });
	}

	function moveActive(direction: 1 | -1): void {
		open = true;
		if (matches.length === 0) {
			activeToken = null;
			return;
		}

		const nextIndex: number =
			activeIndex < 0
				? direction === 1
					? 0
					: matches.length - 1
				: (activeIndex + direction + matches.length) % matches.length;
		activeToken = matches[nextIndex].token;
		void scrollActiveOptionIntoView(activeToken);
	}

	function add(option: Option): void {
		query = '';
		close();
		onchange([...selectors, option.selector]);
		inputElement.focus({ preventScroll: true });
	}

	function remove(token: string): void {
		onchange(selectors.filter((selector) => selectorToken(selector) !== token));
		inputElement.focus({ preventScroll: true });
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.isComposing || event.keyCode === 229) return;
		if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
			event.preventDefault();
			moveActive(event.key === 'ArrowDown' ? 1 : -1);
			return;
		}
		if (event.key === 'Enter') {
			if (activeIndex < 0) return;
			event.preventDefault();
			add(matches[activeIndex]);
			return;
		}
		if (event.key === 'Backspace' && query === '' && selectors.length > 0) {
			remove(selectorToken(selectors[selectors.length - 1]));
			return;
		}
		if (event.key === 'Escape') {
			event.preventDefault();
			close();
			return;
		}
		if (event.key === 'Tab') close();
	}

	function onInput(event: Event): void {
		query = (event.currentTarget as HTMLInputElement).value;
		activeToken = null;
		open = true;
	}

	function onOutsidePointerdown(event: PointerEvent): void {
		if (open && !event.composedPath().includes(rootElement)) close();
	}
</script>

<svelte:window onpointerdowncapture={onOutsidePointerdown} />

<div class="relative" bind:this={rootElement}>
	<div
		class="bg-[var(--sb-inset)] flex flex-wrap items-center gap-1.5 rounded-[4px] border border-border px-2 py-2 focus-within:border-[var(--primary)]"
	>
		{#each selectors as selector (selectorToken(selector))}
			{@const token = selectorToken(selector)}
			<span
				class="flex items-center gap-1.5 rounded-[3px] border px-2 py-1 font-mono text-[11px]"
				style:border-color={selector.kind === 'pipeline' ? 'var(--primary)' : 'var(--border-strong)'}
				style:background={selector.kind === 'pipeline' ? 'var(--sidebar-accent)' : 'transparent'}
			>
				{token}
				<button
					class="text-muted-foreground hover:text-foreground"
					aria-label="Remove {token}"
					onclick={() => remove(token)}><XIcon size={11} /></button
				>
			</span>
		{/each}
		<div class="flex min-w-[180px] flex-1 items-center gap-1.5">
			<SearchIcon size={12} class="text-[var(--sb-text-faint)] shrink-0" />
			<input
				bind:this={inputElement}
				{id}
				value={query}
				role="combobox"
				aria-autocomplete="list"
				aria-haspopup="listbox"
				aria-expanded={open}
				aria-controls={listboxId}
				aria-activedescendant={activeOptionId}
				aria-labelledby={labelledby}
				onfocus={() => (open = true)}
				onpointerdown={() => (open = true)}
				oninput={onInput}
				onkeydown={onKeydown}
				placeholder={selectors.length ? 'add another…' : 'pick pipelines or models…'}
				class="min-w-0 flex-1 bg-transparent py-0.5 font-mono text-[11.5px] outline-none"
			/>
		</div>
	</div>

	{#if open}
		<div
			bind:this={listboxElement}
			id={listboxId}
			role="listbox"
			aria-labelledby={labelledby}
			aria-multiselectable="true"
			class="bg-popover absolute left-0 right-0 top-full z-30 mt-1 max-h-[320px] overflow-y-auto rounded-[4px] border border-border shadow-lg"
		>
			{#each ['Pipelines', 'Models'] as group (group)}
				{@const groupMatches = matches.filter((option) => option.group === group)}
				{#if groupMatches.length}
					<div role="group" aria-labelledby={`${id}-group-${group.toLowerCase()}`}>
						<div
							id={`${id}-group-${group.toLowerCase()}`}
							role="presentation"
							class="text-[var(--sb-text-faint)] bg-[var(--sb-surface-low)] px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em]"
						>
							{group}
						</div>
						{#each groupMatches as option (option.token)}
							{@const optionIndex = matches.findIndex((match) => match.token === option.token)}
							<button
								type="button"
								id={`${id}-option-${optionIndex}`}
								role="option"
								aria-selected="false"
								data-active={activeToken === option.token}
								tabindex="-1"
								class="hover:bg-[var(--sb-hover)] flex w-full items-center gap-2.5 px-2.5 py-1.5 text-left"
								style:background={activeToken === option.token ? 'var(--sb-hover)' : undefined}
								onpointerenter={() => (activeToken = option.token)}
								onclick={() => add(option)}
							>
								<span class="code truncate text-[11.5px]">{option.primary}</span>
								<span class="text-muted-foreground ml-auto shrink-0 font-mono text-[10px]"
									>{option.secondary}</span
								>
							</button>
						{/each}
					</div>
				{/if}
			{/each}
			{#if matches.length === 0}
				<div
					role="option"
					aria-disabled="true"
					aria-selected="false"
					class="text-muted-foreground px-2.5 py-2 font-mono text-[11px]"
				>
					No matching options
					{#if import.meta.env.VITE_SQLTIDE_DEMO === '1' && query.trim() !== ''}
						<p class="mt-1" data-testid="demo-unprepared-selector">
							<code class="code">{query.trim()}</code> isn’t prepared in the demo, which plans the pipeline and models listed here. Run
							<code class="code">sqt playground</code> to plan your own models with ClickHouse on your machine.
						</p>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>
