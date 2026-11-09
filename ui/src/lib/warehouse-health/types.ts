export type WarehouseHealthStatus = 'healthy' | 'warning' | 'critical' | 'unknown';

export type WarehouseDiskHealth = {
	name: string;
	path: string | null;
	type: string | null;
	totalBytes: number | null;
	freeBytes: number | null;
	unreservedBytes: number | null;
	keepFreeBytes: number | null;
	status: WarehouseHealthStatus;
};

export type WarehouseMemoryHealth = {
	residentBytes: number | null;
	hostTotalBytes: number | null;
	cgroupUsedBytes: number | null;
	cgroupLimitBytes: number | null;
	basis: 'cgroup' | 'server_rss_host';
	pressureFraction: number | null;
};

export type WarehouseHealth = {
	availability: 'available' | 'partial' | 'unavailable';
	status: WarehouseHealthStatus;
	adapter: string;
	database: string;
	version: string | null;
	uptimeSeconds: number | null;
	measuredAt: string;
	collectionDurationMs: number;
	stale: boolean;
	warnings: string[];
	capacityWarningFraction: number | null;
	capacityCriticalFraction: number | null;
	disks: WarehouseDiskHealth[];
	inodes: {
		total: number | null;
		free: number | null;
		status: WarehouseHealthStatus;
	};
	memory: WarehouseMemoryHealth | null;
	activity: {
		activeQueries: number | null;
		activeMerges: number | null;
		incompleteMutations: number | null;
	} | null;
	kafkaConsumers: {
		configuredTables: number;
		materializedTables: number;
		materializedTableNames: string[];
		pollingTables: number;
		exceptionTables: number;
		stuckConsumers: {
			table: string;
			stuckSeconds: number | null;
			recentErrors: number;
			latestError: string;
		}[];
	} | null;
	deadLetters: {
		modelName: string;
		rowCount: number;
		latestFailedColumns: string[];
		latestPayload: string;
		latestQuarantinedAt: string;
	}[] | null;
	tables: {
		name: string;
		rows: number | null;
		bytesOnDisk: number | null;
		activeParts: number | null;
	}[] | null;
};
