import { loadDeploymentEvidence } from '../_api/inventory.ts';
import type { DeploymentEvidence, Storage } from '../_helpers/evidence-types.ts';
import { relationStorage } from '../_api/storage.ts';
import { summaryPayload } from '../_helpers/lifecycle.ts';
import type { DeploymentsPayload, Query } from '../types.ts';

/** `GET /api/deployments`: every reconstructed deployment with its storage totals. */
export async function readDeployments(query: Query, database: string): Promise<DeploymentsPayload> {
	const evidence: DeploymentEvidence = await loadDeploymentEvidence(query, database);
	const storage: Map<string, Storage> = await relationStorage(query, database);
	return { database, deployments: evidence.summaries.map((summary) => summaryPayload(summary, storage)) };
}
