/** What every write in the recorded demo returns, and what the UI shows for it. */
export const DEMO_READ_ONLY_MESSAGE: string =
	'This is a read-only UI demo with example data. Run `sqt playground` to try it with ClickHouse on your machine.';

/** What a write other than the staged promotion returns while the real engine runs. */
export const DEMO_ONE_ACTION_MESSAGE: string =
	'This demo runs one action on its engine: promoting the staged deployment. Run `sqt playground` to try the rest with ClickHouse on your machine.';

/** The key a visitor's choice of the recording is kept under for this tab. */
export const RECORDING_CHOICE_KEY: string = 'sqltide-demo-recording';

/** How often the engine adds an order to the source table. */
export const ORDER_EVERY_MS: number = 4000;
