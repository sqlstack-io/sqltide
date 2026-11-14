/** Whether this browser runs the demo's real engine, and if not, the note that says why. */
export type EngineSupport = { real: true } | { real: false; reason: string };

/** The key a visitor's choice of the recording is kept under for this tab. */
export const RECORDING_CHOICE_KEY: string = 'sqltide-demo-recording';

// The smallest module with a 64-bit memory; only browsers with Memory64 accept it.
const MEMORY64_PROBE: Uint8Array<ArrayBuffer> = new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0, 5, 3, 1, 4, 1]);

type BrowserFacts = {
	userAgent: string;
	mobile: boolean | undefined;
	/** The primary pointer is a finger, as on phones and tablets, even when they ask for the desktop site. */
	touchFirst: boolean;
	saveData: boolean | undefined;
	isolated: boolean;
	memory64: boolean;
	chosenRecording: boolean;
};

function browserFacts(): BrowserFacts {
	const navigatorWithHints = navigator as Navigator & {
		userAgentData?: { mobile?: boolean };
		connection?: { saveData?: boolean };
	};
	let chosenRecording: boolean = false;
	try {
		chosenRecording = sessionStorage.getItem(RECORDING_CHOICE_KEY) === '1';
	} catch {
		// storage blocked: nothing was chosen
	}
	return {
		userAgent: navigator.userAgent,
		mobile: navigatorWithHints.userAgentData?.mobile,
		touchFirst: navigator.maxTouchPoints > 0 && globalThis.matchMedia?.('(pointer: coarse)').matches === true,
		saveData: navigatorWithHints.connection?.saveData,
		isolated: globalThis.crossOriginIsolated === true,
		memory64: typeof WebAssembly === 'object' && WebAssembly.validate(MEMORY64_PROBE),
		chosenRecording
	};
}

/** Decides from the browser's facts; the checks run in the order a visitor would expect the note. */
export function engineSupport(facts: BrowserFacts = browserFacts()): EngineSupport {
	const { userAgent } = facts;
	if (facts.chosenRecording) return { real: false, reason: 'You chose the recording.' };
	if (/iPhone|iPad|iPod/.test(userAgent)) {
		return { real: false, reason: 'iOS browsers can’t run the real engine yet, so this is the recording.' };
	}
	if (facts.mobile === true || facts.touchFirst || /Android|Mobi/.test(userAgent)) {
		return { real: false, reason: 'The real engine is a 28 MB download meant for desktops, so phones get the recording.' };
	}
	if (/Safari\//.test(userAgent) && !/Chrome\/|Chromium\/|Edg\//.test(userAgent)) {
		return { real: false, reason: 'Safari can’t run the real engine yet, so this is the recording.' };
	}
	if (facts.saveData === true) {
		return { real: false, reason: 'Your browser asked to save data, so the 28 MB engine isn’t downloaded.' };
	}
	if (!facts.memory64) {
		return {
			real: false,
			reason: 'This browser can’t run the real engine (it needs Chrome or Edge 133+, or Firefox 134+), so this is the recording.'
		};
	}
	if (!facts.isolated) {
		return { real: false, reason: 'This page isn’t cross-origin isolated, which the real engine needs, so this is the recording.' };
	}
	return { real: true };
}
