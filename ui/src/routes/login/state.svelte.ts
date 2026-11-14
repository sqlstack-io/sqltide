import { goto } from '$app/navigation';
import { base } from '$app/paths';
import { login } from '$lib/auth/main/login';
import { initializeApp } from '$lib/api/main/project/initialize-app';
import type { LoginController, LoginFormState } from './types';

export function createLoginState(): LoginController {
	const form: LoginFormState = $state({
		username: '',
		password: '',
		submitting: false,
		error: null
	});

	async function submit(): Promise<void> {
		form.submitting = true;
		form.error = null;
		try {
			await login(form.username, form.password);
			await initializeApp();
			await goto(`${base}/`);
		} catch (error) {
			form.error = error instanceof Error ? error.message : String(error);
		} finally {
			form.submitting = false;
		}
	}

	return { form, submit };
}
