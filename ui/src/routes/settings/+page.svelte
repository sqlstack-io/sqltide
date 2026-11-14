<script lang="ts">
	import { base } from '$app/paths';
	import LockIcon from '@lucide/svelte/icons/lock';
	import AppTopbar from '$lib/presentation/components/app-topbar.svelte';
	import FactRow from '$lib/presentation/components/fact-row.svelte';
	import { CAN_EXECUTE_BUILD } from '$lib/api/constants';
	import { getApp } from '$lib/api/main/project/get-app';
	import { getProject } from '$lib/api/main/project/get-project';
	import type { Project } from '$lib/domain/types';
	import { projectPipelineMode } from '$lib/pipeline-view/main/project-pipeline-mode';

	const project: Project = getProject();
	const app = getApp();
	const effectiveMode = projectPipelineMode(project);
</script>

<AppTopbar title="Settings" />

<div class="min-h-0 flex-1 overflow-y-auto">
	<!-- Durable config lives in version-controlled TOML. Same read-only stance as
	     `main` took on Providers: show it, explain where to change it, don't fake
	     an editor over GitOps. -->
	<div class="flex items-start gap-2.5 border-b border-border px-[18px] py-2.5">
		<LockIcon size={13} class="text-muted-foreground mt-[2px] shrink-0" />
		<p class="text-muted-foreground text-[12.5px]">
			Read-only — from <code class="font-mono text-[11.5px]">sqltide_project.toml</code> and
			<code class="font-mono text-[11.5px]">sqltide_local.toml</code>.
		</p>
	</div>

	<div class="grid gap-6 p-[18px]" style:grid-template-columns="repeat(2, minmax(0, 1fr))">
		<div>
			<div
				class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Project
			</div>
			<FactRow label="Name" value={project.name} mono />
			<FactRow label="Default target" value={project.target} mono />
			<FactRow label="Database" value={project.database} mono />
			<FactRow label="Adapter" value={project.adapter} mono />
			{#if app.status?.toolVersion}
				<FactRow label="SQLTide version" value={app.status.toolVersion} mono />
			{/if}
		</div>

		<div>
			<div
				class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Mode
			</div>
			<FactRow label="Effective mode" value={effectiveMode} mono />
			{#if effectiveMode === 'mixed'}
				{#each project.pipelines as pipeline (pipeline.name)}
					<FactRow label={pipeline.name} value={pipeline.mode} mono />
				{/each}
			{/if}
		</div>

		<div>
			<div
				class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Connection
			</div>
			<FactRow label="Host" value={project.connection.host} mono />
			<FactRow label="Port" value={String(project.connection.port)} mono />
			<FactRow label="Username" value={project.connection.username} mono />
			<FactRow label="Secure" value={String(project.connection.secure)} mono />
			<FactRow
				label="Access"
				value={CAN_EXECUTE_BUILD ? 'read + write' : 'read-only'}
				tone={CAN_EXECUTE_BUILD ? 'warning' : 'success'}
			/>
		</div>

		<div>
			<div
				class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Naming
			</div>
			<FactRow label="table_prefix" value={project.naming.tablePrefix} mono />
			<FactRow label="view_prefix" value={project.naming.viewPrefix} mono />
		</div>

		<div>
			<div
				class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Defaults
			</div>
			<FactRow
				label="managed_source_ttl"
				value={project.defaults.managedSourceTtl ?? 'none'}
				mono
			/>
			<a href="{base}/sources" class="text-primary mt-2 inline-block font-mono text-[11px] hover:underline"
				>See Sources →</a
			>
		</div>

		<div>
			<div
				class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Variables
			</div>
			{#each Object.entries(project.vars) as [key, value] (key)}
				<FactRow label={key} value={String(value)} mono />
			{/each}
		</div>

		<div style:grid-column="1 / -1">
			<div
				class="text-[var(--sb-text-faint)] pb-1.5 font-mono text-[10px] uppercase tracking-[0.14em]"
			>
				Macros
			</div>
			<table class="sb-list w-full text-left">
				<thead>
					<tr class="text-[var(--sb-text-faint)] font-mono text-[10px] uppercase tracking-[0.14em]">
						<th class="px-3 py-2 font-normal">Macro</th>
						<th class="px-3 py-2 font-normal">Signature</th>
						<th class="px-3 py-2 font-normal">File</th>
						<th class="px-3 py-2 font-normal">Description</th>
					</tr>
				</thead>
				<tbody>
					{#each project.macros as macro (macro.name)}
						<tr>
							<td class="code px-3 text-[12px]">{macro.name}</td>
							<td class="text-muted-foreground code px-3 text-[11px]">{macro.signature}</td>
							<td class="text-muted-foreground code px-3 text-[11px]">{macro.file}</td>
							<td class="text-muted-foreground px-3 text-[11.5px]">{macro.description ?? ''}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</div>
</div>
