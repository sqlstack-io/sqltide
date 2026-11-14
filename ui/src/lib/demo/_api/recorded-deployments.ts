import recordings from '../recordings.json';

/** The deployment the recorded story starts with published, and the staged one the visitor promotes. */
export function recordedDeploymentIds(): { published: string; staged: string } {
	const published: string | undefined = recordings.responses['/api/deployments'].body.deployments.find(
		(deployment) => deployment.state === 'active'
	)?.deploymentId;
	if (published === undefined) throw new Error('The demo recording has no published deployment.');
	return { published, staged: recordings.promotion.deploymentId };
}
