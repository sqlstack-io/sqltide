import { requestDeploymentPromotion, requestDeploymentRollback } from '$lib/api/_api/deployment-actions';
import type { PromoteResult } from '$lib/api/types';

/** Make a deployment live: promote a staged one, or roll back to a superseded one. */
export async function publishDeployment(
	deploymentId: string,
	operation: 'promote' | 'rollback'
): Promise<PromoteResult> {
	return operation === 'rollback'
		? requestDeploymentRollback(deploymentId)
		: requestDeploymentPromotion(deploymentId);
}
