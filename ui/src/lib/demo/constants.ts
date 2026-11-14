/** What every write in the recorded demo returns, and what the UI shows for it. */
export const DEMO_READ_ONLY_MESSAGE: string =
	'This is a read-only UI demo with example data. Run `sqt playground` to try it with ClickHouse on your machine.';

/** The names the Plan page can select in the demo's project; every combination of them is prepared. */
export const DEMO_SELECTABLE: readonly string[] = ['pipeline:pl__orders', 'order_report', 'large_orders'];

/** Follows the names a plan request has nothing prepared for. */
export const DEMO_UNPREPARED_SELECTION_MESSAGE: string =
	'It prepares every combination of pipeline:pl__orders, order_report and large_orders. Run `sqt playground` to plan anything with ClickHouse on your machine.';

/** What the API answers once an action has left the steps the demo prepared. */
export const DEMO_OFF_STORY_MESSAGE: string =
	'The demo prepares the story’s steps ahead of time, and the last action left them. Use Start over in the live engine panel, or run `sqt playground` to try anything with ClickHouse on your machine.';

/** What a write the browser demo does not run returns, such as cleanup or a destroy. */
export const DEMO_NOT_IN_BROWSER_MESSAGE: string =
	'This action isn’t part of the browser demo. Run `sqt playground` to try it with ClickHouse on your machine.';

/**
 * The GETs the real engine answers: sqt's plans and builds for the story, and every page that
 * shows the warehouse. The rest (auth, the project's definitions, its status) come from the
 * recording at once, so the page and its loading banner show while the engine starts.
 */
export const DEMO_ENGINE_GETS: RegExp = /^\/api\/(plan|build\/current|state|deployments|runs)(\/|$)/;

/** The key a visitor's choice of the recording is kept under for this tab. */
export const RECORDING_CHOICE_KEY: string = 'sqltide-demo-recording';

/** How often the engine adds an order to the source table. */
export const ORDER_EVERY_MS: number = 4000;
