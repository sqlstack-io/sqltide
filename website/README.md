# SQLTide website

The homepage at `/` and 46 documentation pages at `/docs/` build together as an
Astro/Starlight static site. The documentation baseline is the published **SQLTide 0.60.0**
package, not unreleased runtime integration work.

## Develop and validate

Requires Node **22.23.4+**, Python 3.12, and an installed `sqltide==0.60.0` for example checks.

```sh
npm ci
npm run dev
python scripts/check_examples.py
npm run build
npm run check:links
python scripts/check_production.py
npx playwright install chromium
npm test
```

`SQT` can select the installed baseline CLI. `CHROME_PATH` can select an existing Chromium binary.
The example check compiles files extracted from the actual direct quickstart, virtual walkthrough
and two-model hero fixture in temporary directories. It does not connect to a warehouse.

Browser tests build a separate review copy on port 4174, exercise routes, search, keyboard access,
copying, theme persistence, motion controls and touch menus, and run accessibility checks across
light/dark desktop and Pixel 7 touch-emulated layouts. Search requires a built site; it is not
available in Astro's development server.

## Layout and provenance

- `public/index.html`, `public/styles.css`, `public/site.js`: semantic homepage adapted from the
  Fensu family implementation, with SQLTide content and approved aqua accent.
- `src/components/`, `src/styles/`: family docs shell, local fonts, restrained code themes,
  80px header, separate wordmark-aligned SQLStack byline, and family footer. The default Starlight
  footer/pagination remains intact. Reference implementations inspected: SQLBuild at `c2ccbade7`,
  SQLClean at `8131c8d`, Fensu at `f0753909`, and their live product sites.
- `src/content/docs/docs/`: content originally imported from
  [streambuild-docs](https://github.com/sqlstack-io/streambuild-docs) at
  `1d040820adc9ae13a91d1dadc2c120f10d3c8499`, then reviewed and revised against 0.60.0 source,
  CLI help, tests and executable examples. Old product UI screenshots were not carried over as
  evidence of the current UI.
- `src/navigation.mjs`: task-oriented learning/operations/UI/CLI/examples groups and local
  old-path aliases. `src/mintlify-content.mjs` resolves imported components and links without
  rewriting code samples. New pages should use `/docs/` links.
- `public/assets/logo-light.png`, `logo-dark.png` and matching `src/assets/` files are unchanged
  approved `derivatives/SQLTide-light.png` and `SQLTide-dark.png` artwork. The original approved
  lockup is retained as `public/assets/sqltide-original.png`. No generated replacement logo is used.
  Approved derivative SHA-256: light `eef777dbe106da583c41dd752ac2c860c8cb9b5b13f905eb2652bc1eee935f86`;
  dark `7a46fac47fd46104fc4bfc46027d847446799b516259b2bdabf2803ea6b15786`.
- The SQLStack symbols and SQLTide favicon are unchanged from
  [sqlstack-io/sqlstack](https://github.com/sqlstack-io/sqlstack/tree/9b37b9e128d36930e1fe0c5880606525100b228b/assets/icons).
  Font licences live with the fonts. The adapted Starlight search component retains its MIT licence
  in `STARLIGHT-LICENSE.txt`.

Pagefind and `llms.txt`, `llms-small.txt`, `llms-full.txt` are generated from the docs. The literal
production origin is `https://sqltide.io`, including the static homepage canonical and sitemap.

## Content corrections, not just a rename

- The old quickstart's `pipelines/orders` fails current discovery. The new local quickstart uses
  `pipelines/pl__orders`, distinct source/model names, loopback-only ClickHouse, and does not delete
  an existing container or database to make setup succeed.
- Installation, configuration filenames, Python imports, environment variables, metadata and CLI
  examples use SQLTide. Historical upgrade/state concerns are isolated in the upgrade guide.
- New Kafka examples explicitly configure `sqltide_` groups. Version 0.60.0's default remains
  legacy-prefixed; existing groups/offsets must not be renamed or reset. No unfinished default-
  migration behavior is claimed.
- Added typed retention, constants/enums, PostgreSQL refresh sources, directional dependency
  policy, changed direct selection and target-specific destructive-drop ceilings. PostgreSQL
  source examples are compile-checked, not claimed to be end-to-end PostgreSQL validation.
- Direct builds, inclusive replay, UTC start bounds, downstream selection and staged promotion
  have distinct guarantees. No exactly-once, whole-graph atomicity, automatic rollback, generic
  warehouse support, invented performance numbers, or unfinished lint/format integration is claimed.
- Removed the old upgrade advice to drop metadata. Current migration and lock ownership must be
  respected instead.

## Homepage hero and review alternatives

```sh
npm run build:review
npm run preview
```

The approved production homepage at `/` shows the virtual lifecycle: read a plan, build a
separate version, inspect it beside the current version, then explicitly promote its stable
consumer views. `/concepts/ingestion-replay/` and `/concepts/sql-plan/` are archived exploration
alternatives available only in the opt-in review build, not production routes.

This opt-in build marks **every page** noindex/nofollow, with a blocking review robots file. The
ordinary production build cleans the output and includes neither alternative route. The production
homepage includes the selected interactive hero, with an informative static first state even when
JavaScript is disabled or reduced motion is requested. Homepage copy follows
write/validate → plan → build/inspect → promote. Replay, testing and the local UI
support that lifecycle; direct mode has only a small secondary note. The public-facing illustration
has a concise label and visible per-view promotion limit, not a validation-report accordion.
Reproducible setup and commands live in `src/content/docs/docs/examples/staged-workflow.mdx`;
fixture provenance and measured results stay here and in review artifacts. Playback is user-initiated, finite, pausable and
step-able; reduced-motion users get a static explanation with manual steps. Without JavaScript,
the deciding state remains readable.

The complete model fixture is under `examples/hero/`. Real 0.60.0/ClickHouse validation checked:
changed filter, read-only two-model/four-relation plan, downstream rebuild, preserved adopted input,
inclusive UTC window endpoints, omitted pre-window derived output, and subsequent live filtering
through both materialized views. Illustrative offsets are not throughput measurements. Concurrent
cutoff overlap is explained from the runtime contract, not presented as a measured benchmark.

### Virtual lifecycle evidence

The same fixture is compiled in both modes. For virtual mode, add this project configuration:

```toml
[defaults]
pipeline_mode = "virtual"
```

The real published 0.60.0/ClickHouse 24.8 walkthrough first built and promoted deployment
`20261101T120000Z_a` with `WHERE amount_cents >= 0`, then changed the filter to `>= 100`:

```sh
sqt plan --select order_totals --full-refresh --deployment-id 20261101T130000Z_b
sqt build --select order_totals --full-refresh --deployment-id 20261101T130000Z_b
sqt deployment show 20261101T130000Z_b
sqt deployment diff 20261101T130000Z_b
sqt deployment promote 20261101T130000Z_b
```

- Planning left the catalog and stable view definition unchanged. There is no separate `prepare`
  CLI command: connected execution preparation happens inside `build` before confirmation.
- Build created four suffixed relations for the two-model candidate. The stable
  `tbl__order_report` still read A's five rows, while B's physical table had three filtered rows.
- Two new source rows (75 and 300 cents) reached both deployments: A had seven rows, B had four.
- Explicit promotion made the unchanged consumer query read B's four rows. A's physical graph and
  all seven source rows remained. First-ever build was also checked: no stable consumer view was
  created until the initial promotion.
- The returned capability flags were `per_relation_atomic_replace: true` and
  `graph_atomic_publish: false`. This is not whole-graph atomicity, a zero-downtime guarantee, or
  automatic rollback. Retained graphs remain live; explicit rollback is not point-in-time recovery.

Two baseline limitations were reproduced and offered as separate runtime follow-ups, not patched
in website work: the same filter-only virtual plan without `--full-refresh` fails with
`Population root 'mv__order_totals' has no desired table`; `deployment audit` fails against this
fixture's mapped physical `event_*` source columns with unknown `_replay_partition`. The hero
therefore demonstrates the verified full-refresh path and `show`/`diff` plus direct staged queries,
not a claimed successful deployment audit or an enforced readiness gate.

The staged getting-started guide was also executed from its four authored snippets against the
published 0.60.0 CLI and ClickHouse 24.8. Initial publication served both orders; a changed filter
built separately and excluded the small order. After two live inserts, the previous version had
four rows and the new one had two. Promotion switched the unchanged consumer query to those two
rows while retaining all four source rows and the previous version. This check caught and removed
an invalid first-build `diff`: its implicit `active` endpoint requires existing stable bindings.
Promotion executes directly without a confirmation prompt or an `--auto-approve` option; only
the build was auto-approved for automation. Raw evidence is retained in the review artifacts,
not embedded in the public homepage.

## Deployment and migration boundary

Confirmed hosting: **Cloudflare Pages Git integration**, repository `sqlstack-io/sqltide`, branch
`main`, root `website`, build `npm run build`, output `dist`, Node 22.23.4 or newer. No custom
origin environment-variable contract or GitHub deployment job is needed. `_headers` makes the
unfingerprinted homepage CSS/JS revalidate; Astro-generated assets remain fingerprinted.

`.github/workflows/website.yml` validates production indexing, built links, released examples and
browser behavior. Verify calls it as a required job; failure, cancellation or skipping cannot make
the aggregate green. Existing native/static jobs are not reduced. Changes to this workflow or
Verify itself explicitly retain full integration/E2E/browser scope.

Control of **docs.streambuild.dev is not established**. Local aliases preserve old paths, query
strings and fragments on this site; they do not redirect traffic from another host. An old-host
cutover requires its owner's separate DNS/hosting/redirect action. This does not block building or
validating the confirmed new site.
