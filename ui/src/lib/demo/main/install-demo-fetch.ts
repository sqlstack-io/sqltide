import { installDemoFetch as install } from '../_api/demo-fetch';

/** Answers the UI's API from the recorded responses; only the UI demo build calls this. */
export function installDemoFetch(): void {
	install();
}
