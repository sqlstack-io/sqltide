export const MATERIALIZED_VIEW_NAME_PREFIX: string = 'mv__';
export const VIEW_ENGINE: string = 'View';
export const DEPLOYMENT_STATUS_INCOMPLETE: string = 'incomplete';
/** Object types a promotion binds a stable view to. */
export const PUBLISHED_OBJECT_TYPES: readonly string[] = ['table', 'view'];

export const INVOCATIONS_TABLE: string = '_sqltide_invocations';
export const RUN_EVENTS_TABLE: string = '_sqltide_run_events';
/** `defaults.run_presumed_failed_after` when the project does not set it. */
export const DEFAULT_PRESUMED_FAILED_AFTER_SECONDS: number = 600;
/** Recent events per run that status derivation reads. */
export const RUN_EVENT_WINDOW_SIZE: number = 400;
