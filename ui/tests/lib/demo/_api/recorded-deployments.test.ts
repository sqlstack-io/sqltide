import { describe, expect, it } from 'vitest';

import { recordedDeploymentIds } from '$lib/demo/_api/recorded-deployments';
import recordings from '$lib/demo/recordings.json';

describe('recorded deployments', () => {
	it('given the recording when read then the published and staged deployments differ and both are listed', () => {
		const { published, staged }: { published: string; staged: string } = recordedDeploymentIds();

		const states: Record<string, string> = Object.fromEntries(
			recordings.responses['/api/deployments'].body.deployments.map((deployment) => [deployment.deploymentId, deployment.state])
		);
		expect([states[published], states[staged]]).toEqual(['active', 'staged']);
	});
});
