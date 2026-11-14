import type { PreparedBinding, SlotValues } from '../types';

const SLOT: RegExp = /\{\{(d|dt|u|t|s):([^}]+)\}\}/g;
const DEPLOYMENT_ID: RegExp = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z_[0-9a-f]{6}$/;

const pad = (value: number, width: number = 2): string => String(value).padStart(width, '0');

/** A timestamp as sqt writes it: `s3` is `2026-11-14 08:23:10.359`, `T0` is `2026-11-14T08:23:10`. */
function formatTime(at: number, format: string): string {
	const date: Date = new Date(at);
	const day: string = `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
	const time: string = `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
	const digits: number = Number(format.slice(1));
	const fraction: string = digits === 0 ? '' : `.${`${pad(date.getUTCMilliseconds(), 3)}000`.slice(0, digits)}`;
	return `${day}${format.startsWith('T') ? 'T' : ' '}${time}${fraction}`;
}

/** A deployment ID as sqt names one: the second it was created, then six random hex digits. */
function deploymentId(at: number): string {
	const compact: string = formatTime(at, 's0').replace(/[-:]/g, '').replace(' ', 'T');
	const suffix: string = Array.from(crypto.getRandomValues(new Uint8Array(3)), (byte: number): string =>
		byte.toString(16).padStart(2, '0')
	).join('');
	return `${compact}Z_${suffix}`;
}

/** When sqt named a deployment, from its ID; null for an ID sqt did not write. */
export function deploymentTime(id: string): number | null {
	const parts: RegExpMatchArray | null = id.match(DEPLOYMENT_ID);
	if (parts === null) return null;
	const [, year, month, day, hour, minute, second] = parts.map(Number);
	return Date.UTC(year, month - 1, day, hour, minute, second);
}

/**
 * Starts `role` now: it gets new UUIDs; the deployments it creates get IDs named after the moment
 * sqt named them, and its times fall as long after its start as sqt's did.
 */
export function startRole(
	values: SlotValues,
	role: string,
	startedAt: number,
	{ creates, times }: PreparedBinding,
	ids: Record<string, string> = {}
): void {
	values.roleStarts[role] = startedAt;
	values.times[role] = [];
	for (const name of Object.keys(values.uuids)) {
		if (name.startsWith(`${role}.`)) delete values.uuids[name];
	}
	for (const [owner, ordinal, offset] of times) {
		const ownerStart: number | undefined = values.roleStarts[owner];
		if (ownerStart === undefined) throw new Error(`The demo has not run ${owner}, which ${role} needs`);
		(values.times[owner] ??= [])[ordinal] = ownerStart + offset;
	}
	for (const created of creates) {
		// A deployment the page already named, such as the one a plan proposed, keeps its ID.
		const given: string | undefined = ids[created.name];
		const givenAt: number | null = given === undefined ? null : deploymentTime(given);
		if (given !== undefined && givenAt !== null) {
			values.deployments[created.name] = { id: given, at: givenAt };
			continue;
		}
		// IDs sort in the order sqt created them, so a new one is at least a second after the others.
		const latest: number = Math.max(
			-Infinity,
			...Object.entries(values.deployments)
				.filter(([name]) => name !== created.name)
				.map(([, deployment]) => deployment.at)
		);
		const at: number = Math.max(Math.floor((startedAt + created.offset) / 1000) * 1000, latest + 1000);
		values.deployments[created.name] = { id: deploymentId(at), at };
	}
}

/** Prepared text with its slots filled for this tab. */
export function fillSlots(text: string, values: SlotValues): string {
	return text.replaceAll('{{v}}', values.version).replace(SLOT, (whole: string, kind: string, body: string): string => {
		const parts: string[] = body.split(':');
		if (kind === 's') {
			if (values.startTime === null) throw new Error(`The demo has no start time for ${whole}`);
			return formatTime(values.startTime, body);
		}
		if (kind === 'u') {
			values.uuids[body] ??= crypto.randomUUID();
			return values.uuids[body];
		}
		if (kind === 'd' || kind === 'dt') {
			const deployment: { id: string; at: number } | undefined = values.deployments[parts[0]];
			if (deployment === undefined) throw new Error(`The demo has no deployment for ${whole}`);
			return kind === 'd' ? deployment.id : formatTime(deployment.at, parts[1]);
		}
		const at: number | undefined = values.times[parts[0]]?.[Number(parts[1])];
		if (at === undefined) throw new Error(`The demo has no time for ${whole}`);
		return formatTime(at, parts[2]);
	});
}
