import type { RunEvent } from '$lib/api/types';
import { formatDuration } from '$lib/formatting/main/format-duration';
import type { RunEventLabelContext } from '$lib/run-presentation/types';

export function labelRunEvent(event: RunEvent, context: RunEventLabelContext): string {
	const stepId: string | null = event.stepId;
	if (stepId === null) {
		if (event.event === 'cancellation_requested') {
			return `Warehouse cancellation requested for ${event.queryId ?? 'no active query'}`;
		}
		if (event.event === 'cancellation_completed') {
			return `Warehouse query ${event.queryId ?? 'unknown'} cancelled`;
		}
		if (event.event === 'cancellation_failed') {
			return `Warehouse cancellation failed for ${event.queryId ?? 'unknown'}${event.errorMessage ? ` · ${event.errorMessage}` : ''}`;
		}
		if (event.event === 'run_completed') return event.outcome ?? 'completed';
		if (event.event === 'run_started' && event.startupTimings) {
			return `${context.displayCommand} · prepared in ${formatDuration(event.startupTimings.totalMs / 1000)} (compile ${formatDuration(event.startupTimings.compileMs / 1000)}, observability ${formatDuration(event.startupTimings.observabilityMs / 1000)}, warehouse plan ${formatDuration(event.startupTimings.planningMs / 1000)})`;
		}
		return context.displayCommand;
	}
	if (event.event === 'audit_started' || event.event === 'audit_completed') {
		const statusLabel: string = event.status ? ` · ${humanizeIdentifier(event.status)}` : '';
		const failureLabel: string =
			(event.failureCount ?? 0) > 0 ? ` · ${event.failureCount} failures` : '';
		return `${stepId}${statusLabel}${failureLabel}`;
	}
	if (event.displayName) return event.displayName;
	return labelRunStepId(stepId, context);
}

export function labelRunStepId(
	stepId: string,
	context: RunEventLabelContext | null = null
): string {
	const metadataStep: RegExpMatchArray | null = stepId.match(/^prepare_metadata_(\d+)$/);
	if (metadataStep) {
		return numberedLabel(
			'Prepare metadata schema',
			metadataStep[1],
			context?.metadataPreparationCount ?? 0
		);
	}
	const persistenceStep: RegExpMatchArray | null = stepId.match(
		/^persist_candidate_metadata_(\d+)$/
	);
	if (persistenceStep) {
		return numberedLabel(
			'Record deployment metadata',
			persistenceStep[1],
			context?.candidateMetadataCount ?? 0
		);
	}
	const migrationStep: RegExpMatchArray | null = stepId.match(/^migrate_metadata_(\d+)$/);
	if (migrationStep) {
		return numberedLabel(
			'Prepare metadata schema',
			migrationStep[1],
			context?.metadataMigrationCount ?? 0
		);
	}
	const publicationStep: RegExpMatchArray | null = stepId.match(/^persist_publish_event_(\d+)$/);
	if (publicationStep) {
		return numberedLabel(
			'Record publication',
			publicationStep[1],
			context?.publicationCount ?? 0
		);
	}
	const reconcileStep: RegExpMatchArray | null = stepId.match(/^persist_reconcile_state_(\d+)$/);
	if (reconcileStep) {
		return numberedLabel(
			'Record reconciled metadata',
			reconcileStep[1],
			context?.reconcileCount ?? 0
		);
	}
	const auditStep: RegExpMatchArray | null = stepId.match(/^audit_\d+_(.+)_(count|sample)$/);
	if (auditStep) {
		return `${auditStep[2] === 'count' ? 'Check audit' : 'Sample audit failures'} · ${auditStep[1]}`;
	}
	const exact: Record<string, string> = {
		prepare_target_database: 'Ensure target database exists',
		assert_candidate_metadata: 'Validate deployment metadata',
		assert_candidate_unpublished: 'Confirm deployment is unpublished',
		wait_for_virtual_live_stabilization: 'Wait for source stabilization',
		wait_for_live_stabilization: 'Wait for source stabilization',
		record_population_started: 'Record build start',
		capture_boundary_time: 'Capture replay boundary',
		read_boundary_time: 'Read replay boundary',
		record_population_completed: 'Record build completion',
		replace_active_view: 'Repair active view'
	};
	if (exact[stepId]) return exact[stepId];
	const prefixes: [string, string][] = [
		['assert_candidate_relation_', 'Check candidate relation'],
		['prepare_source_', 'Prepare source'],
		['replace_stable_binding_', 'Publish'],
		['remove_stable_binding_', 'Unpublish'],
		['drop_', 'Remove existing relation'],
		['realize_', 'Create relation'],
		['attach_source_', 'Activate source'],
		['activate_source_', 'Activate source'],
		['capture_replay_', 'Capture replay range'],
		['capture_watermark_', 'Capture source watermark'],
		['assert_qualifying_input_', 'Verify replayable input'],
		['seed_', 'Seed replay input'],
		['replay_', 'Replay source data'],
		['read_readiness_', 'Measure source readiness'],
		['assert_readiness_', 'Verify source readiness']
	];
	for (const [prefix, label] of prefixes) {
		if (stepId.startsWith(prefix)) return `${label} · ${stepId.slice(prefix.length)}`;
	}
	const numbered: [string, string][] = [
		['destroy_relation_', 'Drop relation'],
		['remove_obsolete_binding_', 'Remove obsolete binding'],
		['cleanup_relation_', 'Delete retained relation'],
		['record_direct_fingerprint_', 'Record build fingerprint'],
		['record_terminal_observation_', 'Record run result']
	];
	for (const [prefix, label] of numbered) {
		if (stepId.startsWith(prefix)) return `${label} (${Number(stepId.slice(prefix.length))})`;
	}
	return stepId;
}

function humanizeIdentifier(value: string): string {
	const words: string = value.replaceAll('_', ' ');
	return words.charAt(0).toUpperCase() + words.slice(1);
}

function numberedLabel(label: string, rawIndex: string, total: number): string {
	const index: number = Number(rawIndex);
	return total > 1 ? `${label} (${index}/${total})` : label;
}
