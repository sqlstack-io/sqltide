import { captureWarehouseTimestamp } from '../_api/warehouse-probes.ts';
import { measureWarehouse } from '../_api/measure-warehouse.ts';
import { sourceStates } from '../_api/source-states.ts';
import { modelStates } from '../_helpers/live-state.ts';
import type { Measurements } from '../_helpers/live-state.ts';
import { readWarehouseHealth } from '../_api/warehouse-health.ts';
import type { JsonObject, Query, StateDefinitions, StatePayload } from '../types.ts';

/**
 * `GET /api/state` recomputed against the engine. `template` is the payload `sqt dev` returned and
 * `definitions` its `/api/definitions`; both describe the compiled project, which does not change.
 * Every warehouse measurement is read again; compile-time comparisons (`drift`, `driftReasons`,
 * `semanticDrift`, `consumerGroupDrift`) are kept from the template. Disk, inode and memory
 * diagnostics are reported as unavailable, and broker lag as unknown, because a browser engine
 * has no host machine or Kafka broker.
 */
export async function readState(
	query: Query,
	database: string,
	template: StatePayload,
	definitions: StateDefinitions
): Promise<StatePayload> {
	const capturedAt: string = await captureWarehouseTimestamp(query);
	const warehouseHealth: JsonObject = await readWarehouseHealth(query, database, capturedAt, template.warehouseHealth);
	const measured: Measurements = await measureWarehouse(query, database, capturedAt, template, definitions);
	return {
		capturedAt,
		warehouseHealth,
		models: modelStates(template, definitions, measured),
		sources: await sourceStates(query, database, template, definitions, measured)
	};
}
