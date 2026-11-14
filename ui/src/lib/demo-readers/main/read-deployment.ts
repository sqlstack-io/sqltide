import { loadDeploymentEvidence } from '../_api/inventory.ts';
import type {
	DeploymentEvidence,
	DeploymentSummary,
	PromotionPreview,
	Storage
} from '../_helpers/evidence-types.ts';
import { deploymentIdOf, logicalNameOf, modelRelationNames } from '../_helpers/naming.ts';
import {
	buildPublishBindingRequest,
	incompleteMessage,
	replacementPreview,
	staleUpstreamMessage
} from '../_helpers/promotion.ts';
import { relationStorage } from '../_api/storage.ts';
import { storageOf, storageTotal, summaryPayload } from '../_helpers/lifecycle.ts';
import type {
	DeploymentDetailPayload,
	DeploymentModelPayload,
	PromotionPreviewPayload,
	Query
} from '../types.ts';

function previewPayload(preview: PromotionPreview, blockedReason: string | null): PromotionPreviewPayload {
	return {
		classification: preview.initialPublish ? 'initial_publish' : 'promotion',
		additions: preview.additions.map((binding) => ({ ...binding })),
		replacements: preview.replacements.map(({ binding, fromPhysicalName }) => ({
			database: binding.database,
			logicalName: binding.logicalName,
			fromPhysicalName,
			toPhysicalName: binding.physicalName
		})),
		removals: preview.removals.map((removal) => ({ ...removal })),
		blockedReason
	};
}

/**
 * `GET /api/deployments/{id}`: one deployment with each model's staged versus live relation,
 * what promoting it would change and why it would be refused. `null` when it does not exist.
 */
export async function readDeployment(
	query: Query,
	database: string,
	deploymentId: string
): Promise<DeploymentDetailPayload | null> {
	const evidence: DeploymentEvidence = await loadDeploymentEvidence(query, database);
	const deployment: DeploymentSummary | undefined = evidence.summaries.find((item) => item.deploymentId === deploymentId);
	if (deployment === undefined) return null;
	const storage: Map<string, Storage> = await relationStorage(query, database);
	const liveByLogical: Map<string, string> = new Map(
		evidence.inspected.activeBindings.map((binding) => [binding.logicalName, binding.physicalName])
	);
	const models: DeploymentModelPayload[] = modelRelationNames(deployment.physicalRelationNames).map((stagedRelation) => {
		const logicalName: string = logicalNameOf(stagedRelation);
		const liveRelation: string | null = liveByLogical.get(logicalName) ?? null;
		const staged: Storage = storageOf(storage, stagedRelation);
		return {
			logicalName,
			stagedRelation,
			stagedRows: staged.rows,
			stagedBytes: staged.bytes,
			liveRelation,
			liveDeploymentId: liveRelation === null ? null : deploymentIdOf(liveRelation),
			liveRows: liveRelation === null ? null : storageOf(storage, liveRelation).rows,
			isActive: liveRelation === stagedRelation,
			isNew: liveRelation === null
		};
	});
	let preview: PromotionPreview | null = null;
	if (deployment.state === 'staged' || deployment.state === 'active') {
		const candidate: PromotionPreview = replacementPreview(
			buildPublishBindingRequest(evidence, deploymentId),
			evidence.inspected.activeBindings
		);
		const changes: boolean =
			candidate.additions.length > 0 || candidate.replacements.length > 0 || candidate.removals.length > 0;
		if (deployment.state === 'staged' || changes) preview = candidate;
	}
	// The same refusal `sqt deployment promote` applies, shown before it runs.
	const blockedReason: string | null =
		preview === null
			? null
			: staleUpstreamMessage(evidence, deploymentId, buildPublishBindingRequest(evidence, deploymentId));
	const orphaned: string[] = preview?.orphaned.map((relation) => relation.physicalName) ?? [];
	return {
		database,
		...summaryPayload(deployment, storage),
		models,
		promotionPreview:
			deployment.state === 'incomplete'
				? {
						classification: 'promotion',
						additions: [],
						replacements: [],
						removals: [],
						blockedReason: incompleteMessage(deploymentId)
					}
				: preview === null
					? null
					: previewPayload(preview, blockedReason),
		wouldOrphan: {
			relationNames: orphaned,
			relationCount: orphaned.length,
			bytes: storageTotal(orphaned, storage).bytes
		}
	};
}
