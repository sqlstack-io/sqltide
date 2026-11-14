import type { ClientInit } from '@sveltejs/kit';
import { installDemoFetch } from '$lib/demo/main/install-demo-fetch';

// The UI demo build (VITE_SQLTIDE_DEMO=1) answers the API from recordings. Other builds drop this
// call, and with it the recordings.
export const init: ClientInit = () => {
	if (import.meta.env.VITE_SQLTIDE_DEMO === '1') installDemoFetch();
};
