const group = (label, base, pages) => ({ label, items: pages.map(([slug, title]) => ({ label: title, slug: `docs/${base}${slug}`.replace(/\/$/, '').replace(/\/index$/, '') })) });
export const sidebar = [
  group('Getting started', '', [['', 'Introduction'], ['quickstart', 'Quickstart'], ['upgrade', 'From StreamBuild to SQLTide']]),
  group('Build a pipeline', 'concepts/', [['project-configuration', 'Project configuration'], ['pipelines', 'Sources and pipelines'], ['models', 'Typed SQL models'], ['retention', 'Retention policies'], ['declarations', 'Constants and enums'], ['macros', 'Python macros'], ['testing', 'SQL testing'], ['audits', 'Data-quality audits']]),
  group('Operate and deploy', 'concepts/', [['direct-mode', 'Direct builds'], ['deployments', 'Staged deployments'], ['replay-lineage', 'Replay and lineage'], ['destructive-operations', 'Destructive operations'], ['architecture', 'ClickHouse architecture'], ['authentication-and-access', 'Authentication'], ['access-control', 'Access control'], ['sensors', 'Sensors'], ['providers', 'Providers']]),
  group('Development UI', 'ui/', [['overview', 'Overview'], ['pipelines', 'Pipelines'], ['warehouse-health', 'Warehouse health'], ['sources', 'Sources'], ['topics', 'Topics'], ['messages', 'Messages'], ['plans', 'Plans'], ['runs', 'Runs'], ['quality', 'Quality']]),
  group('CLI reference', 'cli/', [['index', 'Command overview'], ...['discover', 'compile', 'plan', 'build', 'dev', 'test', 'audit', 'deployment', 'doctor', 'repair', 'reconcile', 'janitor', 'destruction', 'admin'].map(name => [name, name === 'destruction' ? 'sqt destroy / reset-target' : `sqt ${name}`])]),
  group('Examples', 'examples/', [['orders-demo', 'Kafka commerce demo'], ['staged-workflow', 'Stage a pipeline change']]),
];
export const docsRoutes = sidebar.flatMap(group => group.items.map(item => `/${item.slug.replace(/\/$/, '').replace(/\/index$/, '')}/`));
export const redirects = Object.fromEntries(docsRoutes.filter(route => route !== '/docs/').map(route => [route.replace('/docs', '').replace(/\/$/, ''), route]));
Object.assign(redirects, {
  '/index': '/docs/', '/docs/index': '/docs/',
  '/ui/sources-and-messages': '/docs/ui/sources/',
  '/ui/plans-and-runs': '/docs/ui/plans/',
});
