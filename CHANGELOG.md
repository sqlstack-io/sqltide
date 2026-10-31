# Changelog

## [0.47.0](https://github.com/sqlstack-io/sqltide/compare/v0.46.0...v0.47.0) (2026-10-31)


### Features

* serialize and stage compile artifacts in Rust ([#277](https://github.com/sqlstack-io/sqltide/issues/277)) ([167b743](https://github.com/sqlstack-io/sqltide/commit/167b743ce3103af542e38cc926fb64141d7d02df))

## [0.46.0](https://github.com/sqlstack-io/sqltide/compare/v0.45.0...v0.46.0) (2026-10-31)


### Features

* analyze model SQL trees in Rust ([#274](https://github.com/sqlstack-io/sqltide/issues/274)) ([27c919f](https://github.com/sqlstack-io/sqltide/commit/27c919f5bef3b29f6848dfd6e30636ceb904caba))
* rewrite and analyze generated SQL in Rust ([#275](https://github.com/sqlstack-io/sqltide/issues/275)) ([83606f7](https://github.com/sqlstack-io/sqltide/commit/83606f7195e70094b26d85f0cd63ff2de245db25))

## [0.45.0](https://github.com/sqlstack-io/sqltide/compare/v0.44.0...v0.45.0) (2026-10-31)


### ⚠ BREAKING CHANGES

* sqltide no longer depends on the polyglot-sql package.

### Features

* build the Polyglot SQL engine into the native extension ([#272](https://github.com/sqlstack-io/sqltide/issues/272)) ([afbaada](https://github.com/sqlstack-io/sqltide/commit/afbaadaa8d8f8b6ddc2ab6bf05884f621da46ab0))

## [0.44.0](https://github.com/sqlstack-io/sqltide/compare/v0.43.0...v0.44.0) (2026-10-31)


### Features

* scan SQL lexically in Rust ([#270](https://github.com/sqlstack-io/sqltide/issues/270)) ([196e530](https://github.com/sqlstack-io/sqltide/commit/196e530cd278c6bc9634999220a4af23956bc79e))

## [0.43.0](https://github.com/sqlstack-io/sqltide/compare/v0.42.0...v0.43.0) (2026-10-31)


### Features

* parse MODEL headers natively in Rust ([#268](https://github.com/sqlstack-io/sqltide/issues/268)) ([3b8adf7](https://github.com/sqlstack-io/sqltide/commit/3b8adf70cd304b4f970020e6d35a9d7ebb24b78d))

## [0.42.0](https://github.com/sqlstack-io/sqltide/compare/v0.41.10...v0.42.0) (2026-10-31)


### ⚠ BREAKING CHANGES

* install sqltide instead of streambuild and run sqt instead of stb. Rename streambuild_project.toml and streambuild_local.toml to sqltide_project.toml and sqltide_local.toml, STREAMBUILD_* environment variables to SQLTIDE_*, and streambuild imports to sqltide. Trusted proxies send X-SQLTide-User by default. Upgrade every writer of a target together.

### Features

* rename StreamBuild to SQLTide ([#266](https://github.com/sqlstack-io/sqltide/issues/266)) ([7e8c1be](https://github.com/sqlstack-io/sqltide/commit/7e8c1be202ce5cf559ff8cc5be4bb3719a8d95a1))


### Maintenance

* move repository links to the sqlstack-io organization ([#263](https://github.com/sqlstack-io/sqltide/issues/263)) ([3fbb1cc](https://github.com/sqlstack-io/sqltide/commit/3fbb1cc4591d9bb1b4c6f0b42f502de701bdfbbd))
* stop tracking the root vitest cache ([#265](https://github.com/sqlstack-io/sqltide/issues/265)) ([3c2cd17](https://github.com/sqlstack-io/sqltide/commit/3c2cd17c6a3554e4ea349efdc61b5408099c3922))

## [0.41.10](https://github.com/chio-labs/sqltide/compare/v0.41.9...v0.41.10) (2026-10-26)


### Bug Fixes

* **plan:** rebuild one owner per drifted source and colour SQL diffs ([#261](https://github.com/chio-labs/sqltide/issues/261)) ([d245544](https://github.com/chio-labs/sqltide/commit/d2455448e6f25e06af221b6be1b5426db632f448))

## [0.41.9](https://github.com/chio-labs/sqltide/compare/v0.41.8...v0.41.9) (2026-10-26)


### Bug Fixes

* **plan:** select drifted Kafka sources in changed builds and show them in the plan ([#259](https://github.com/chio-labs/sqltide/issues/259)) ([64bb4a1](https://github.com/chio-labs/sqltide/commit/64bb4a13ee4d8a31d4118f8ed190faef8a16180a))

## [0.41.8](https://github.com/chio-labs/sqltide/compare/v0.41.7...v0.41.8) (2026-10-26)


### Bug Fixes

* **direct:** recreate managed Kafka sources when only their settings change ([#257](https://github.com/chio-labs/sqltide/issues/257)) ([e467978](https://github.com/chio-labs/sqltide/commit/e4679785c0211cdc9b6574674d696dcc59ce8762))

## [0.41.7](https://github.com/chio-labs/sqltide/compare/v0.41.6...v0.41.7) (2026-10-26)


### Bug Fixes

* **clickhouse:** land Kafka broker timestamps with millisecond precision ([#255](https://github.com/chio-labs/sqltide/issues/255)) ([5fcd9eb](https://github.com/chio-labs/sqltide/commit/5fcd9eb8e7677574b5eb9515fd4da32655c57308))

## [0.41.6](https://github.com/chio-labs/sqltide/compare/v0.41.5...v0.41.6) (2026-09-17)


### Bug Fixes

* add bounded replay picker and safe UI caching ([#253](https://github.com/chio-labs/sqltide/issues/253)) ([ee7f004](https://github.com/chio-labs/sqltide/commit/ee7f004facdd5ea17979ce4b9be2cd7ebd1126c4))

## [0.41.5](https://github.com/chio-labs/sqltide/compare/v0.41.4...v0.41.5) (2026-09-17)


### Bug Fixes

* improve replay startup and controls ([#251](https://github.com/chio-labs/sqltide/issues/251)) ([d80d420](https://github.com/chio-labs/sqltide/commit/d80d420004e59a0ca15fe66acb660240ea0475cf))

## [0.41.4](https://github.com/chio-labs/sqltide/compare/v0.41.3...v0.41.4) (2026-09-15)


### Bug Fixes

* clarify warehouse connection recovery ([#249](https://github.com/chio-labs/sqltide/issues/249)) ([44ea747](https://github.com/chio-labs/sqltide/commit/44ea7473283cd300f820902f7a0e04c0149ff427))

## [0.41.3](https://github.com/chio-labs/sqltide/compare/v0.41.2...v0.41.3) (2026-09-11)


### Bug Fixes

* retry incomplete ClickHouse catalog snapshots ([#247](https://github.com/chio-labs/sqltide/issues/247)) ([f622cf9](https://github.com/chio-labs/sqltide/commit/f622cf934354c704cb913c971885080d3b663dd3))

## [0.41.2](https://github.com/chio-labs/sqltide/compare/v0.41.1...v0.41.2) (2026-09-11)


### Documentation

* document public repository hygiene ([#245](https://github.com/chio-labs/sqltide/issues/245)) ([2853524](https://github.com/chio-labs/sqltide/commit/2853524ce21b0dbbd8427d413c797d01f1ebed12))

## [0.41.1](https://github.com/chio-labs/sqltide/compare/v0.41.0...v0.41.1) (2026-09-08)


### Bug Fixes

* refresh public fixture ([3b747bb](https://github.com/chio-labs/sqltide/commit/3b747bb0ec917aab0bdf259f630b67097e046fbc))

## [0.41.0](https://github.com/chio-labs/sqltide/compare/v0.40.2...v0.41.0) (2026-09-08)


### ⚠ BREAKING CHANGES

* remove the direct-build preflight phase and retention coverage state

### Features

* --events JSONL stream and durable _sqltide_run_events timeline ([9a2a7b1](https://github.com/chio-labs/sqltide/commit/9a2a7b1fa224cf55b11a28c1318e6e7f4561f8d0))
* **adapter:** prove refreshable views against clickhouse and report their state ([7c1f916](https://github.com/chio-labs/sqltide/commit/7c1f916be21f4baa7ce5721722e3cc4ece8204ab))
* **adapter:** realize scheduled postgres sources as refreshable views ([75d00a4](https://github.com/chio-labs/sqltide/commit/75d00a49f07b9fc529fda5e2ca24494af6094d60))
* **adapter:** send configured ClickHouse session settings ([a42d7f1](https://github.com/chio-labs/sqltide/commit/a42d7f15c14cc079a4023655a7bcc6b65a9c5e8f))
* add a tick timeline to the sensor detail page ([e895636](https://github.com/chio-labs/sqltide/commit/e895636accd402fe0208f43a1bbe07c24ece0f3c))
* add append-only metadata history ([3900ef8](https://github.com/chio-labs/sqltide/commit/3900ef8acd279c4f1e7459cc9331d26fa6349d19))
* add authentication policy and durable sensors ([a47db7f](https://github.com/chio-labs/sqltide/commit/a47db7f3ee257feca64529cfae5eec2838cb8332))
* add authored source freshness policies ([dee9f6b](https://github.com/chio-labs/sqltide/commit/dee9f6bdbd2054d507db194fb477e29f89aa5bc6))
* add build safety guardrails ([cd66cf2](https://github.com/chio-labs/sqltide/commit/cd66cf21388bb469cc6fdec57dd3cfb587f6585b))
* add build safety guardrails ([d49805c](https://github.com/chio-labs/sqltide/commit/d49805c0ed44609523f7a19addc0bf90296a0ff5))
* add changed model selection and complete UI states ([#197](https://github.com/chio-labs/sqltide/issues/197)) ([10f7896](https://github.com/chio-labs/sqltide/commit/10f78964faa86164cec91314b54e5eaf2ee0821d))
* add deployment CLI resource family ([2d4e44d](https://github.com/chio-labs/sqltide/commit/2d4e44dac5f756390d5015b0f0e5e88138866b94))
* add description to the MODEL() header ([74c1ceb](https://github.com/chio-labs/sqltide/commit/74c1ceb28574b52b31c0b07236d7318baf79ddd9))
* add durable UI run execution model ([8ecadd4](https://github.com/chio-labs/sqltide/commit/8ecadd48083ac443ae148d990aeb2043944fb590))
* add lineage activity telemetry ([a8c93b5](https://github.com/chio-labs/sqltide/commit/a8c93b5298a79bfddfc8adcc26ff9fb41023049e))
* add lineage activity telemetry ([4d09092](https://github.com/chio-labs/sqltide/commit/4d0909283b910a137c15a8964e711bf1a5a31e65))
* add per-pipeline build modes ([d8cbb1f](https://github.com/chio-labs/sqltide/commit/d8cbb1fb6307aa9ec0107b249c5ef2a3217ceace))
* add per-pipeline build modes ([54cf89f](https://github.com/chio-labs/sqltide/commit/54cf89f6a0df26c7665fa16b850a295103f9671f))
* add pipeline safeguards and Kafka observability ([7b12cf9](https://github.com/chio-labs/sqltide/commit/7b12cf9fcd6f52cf71d663c4022906346278599b))
* add pipeline safeguards and Kafka observability ([56ce16d](https://github.com/chio-labs/sqltide/commit/56ce16d39e0995066921c3d777de071ccf44e56e))
* add quality identities and audit scheduling ([ae691a2](https://github.com/chio-labs/sqltide/commit/ae691a253b8b4e36d46d5ed023fa71227851ed0a))
* add quality identities and audit scheduling ([4286e26](https://github.com/chio-labs/sqltide/commit/4286e26b3f2bdad66d6ac3fe0fd89407cf4693d2))
* add recorded pipeline destruction and target reset ([#178](https://github.com/chio-labs/sqltide/issues/178)) ([6b2a257](https://github.com/chio-labs/sqltide/commit/6b2a257c3614144c728bdc19bbaa368f234e4a47))
* add source message browser and topics inventory ([b09c039](https://github.com/chio-labs/sqltide/commit/b09c03903fa197b34b03e9e4904cba0cb875ca5b))
* add source message browser and topics inventory ([0cfb097](https://github.com/chio-labs/sqltide/commit/0cfb097bc1f5876095d92ba82b8fd70d23d29bf4))
* add sqt dev ([2eb7619](https://github.com/chio-labs/sqltide/commit/2eb76193d39bdbb2767172d710b196acfe04bd2c))
* add warehouse health diagnostics ([#170](https://github.com/chio-labs/sqltide/issues/170)) ([6830bd6](https://github.com/chio-labs/sqltide/commit/6830bd60b06928c3ba8454ab3de9c2b75b3f3f73))
* align sensors and users pages with the list design language ([7db3dd0](https://github.com/chio-labs/sqltide/commit/7db3dd0946cf7a1fc9e45898741aefcdcd7cf6d4))
* allow deleting inactive pipelines ([#236](https://github.com/chio-labs/sqltide/issues/236)) ([b71852e](https://github.com/chio-labs/sqltide/commit/b71852ed3d6112f72df7f96dfcb6e6b09cee6f47))
* allow directional cross-pipeline references ([#202](https://github.com/chio-labs/sqltide/issues/202)) ([6d01f19](https://github.com/chio-labs/sqltide/commit/6d01f191f6e75057577fdbfac5e033e329f7ddf2))
* build selected pipelines from inventory ([#211](https://github.com/chio-labs/sqltide/issues/211)) ([ee0df63](https://github.com/chio-labs/sqltide/commit/ee0df63c04fd909f14c89268ace8197615b7ed09))
* checks history, dagster-aligned runs table, plan preload ([dba5fb3](https://github.com/chio-labs/sqltide/commit/dba5fb39a8f5de8ce03aaccd65b81626e6b27763))
* clarify observability and catalog drift ([#217](https://github.com/chio-labs/sqltide/issues/217)) ([03d126f](https://github.com/chio-labs/sqltide/commit/03d126f96be8fafbae52479c6ff2d8b02f09d868))
* **compiler:** discover scheduled postgres refresh sources ([2895846](https://github.com/chio-labs/sqltide/commit/289584655f856825bc1b7e3026f669c98d6f6ce5))
* complete virtual deployment lifecycle ([affdb79](https://github.com/chio-labs/sqltide/commit/affdb79f26d8e5891a927bb2f2b248434d1d644b))
* complete virtual deployment lifecycle ([142134f](https://github.com/chio-labs/sqltide/commit/142134f24cc6459baf52b604fc82ce8116ebbb97))
* dagster-style runs page, snapshot refresh, macro descriptions, and gap cleanup ([d4003af](https://github.com/chio-labs/sqltide/commit/d4003af24d7d65abf9820efb0f37444a241f1ccf))
* declare generic audits in the MODEL() header; delete schema.yml ([7bd77a1](https://github.com/chio-labs/sqltide/commit/7bd77a1f59466f010f80b61cb50c5f5d832cf402))
* derive Kafka source names with macros ([2a6afcb](https://github.com/chio-labs/sqltide/commit/2a6afcb4eb25c2ec340a4777bab9663da5bccf3f))
* derive Kafka source names with macros ([9f28e03](https://github.com/chio-labs/sqltide/commit/9f28e03f0a463f335ef6fcadf04115565c203a77))
* **destruction:** add scoped recovery controls ([#186](https://github.com/chio-labs/sqltide/issues/186)) ([237c2a7](https://github.com/chio-labs/sqltide/commit/237c2a768dba6fff77c32c6474c41da79a1eb373))
* dev UI - SvelteKit frontend, server-backed API client, and build glue ([96a5f97](https://github.com/chio-labs/sqltide/commit/96a5f970851e82b343c47b95102f8bbce6dc008f))
* dev_server core - compile state, status, reload, definitions ([3b91a9a](https://github.com/chio-labs/sqltide/commit/3b91a9ae8b98375a79f404e378bbaab4fcde1ef9))
* dev_server live state - the /api/state warehouse overlay ([cd7c42e](https://github.com/chio-labs/sqltide/commit/cd7c42e753e6a8c21d15c4d352940eebbe01dc33))
* dev_server plan, checks, and run history endpoints ([e9ff32f](https://github.com/chio-labs/sqltide/commit/e9ff32f18d2c2aace5640e49f3e00d8b3f52b87b))
* enforce global pipeline naming uniqueness ([0e7ae2a](https://github.com/chio-labs/sqltide/commit/0e7ae2ac9afaa11eea23a3666f86e53aeefb5e65))
* enforce observability non-authority ([e6096c8](https://github.com/chio-labs/sqltide/commit/e6096c80b0cacbe46f6ce5c04ad53f5e92391725))
* estimate replay progress from offsets ([#221](https://github.com/chio-labs/sqltide/issues/221)) ([757401b](https://github.com/chio-labs/sqltide/commit/757401b2cb93051f5afd6cd2917d137abae69b0d))
* execute from the UI — plan Execute, live run page, lineage run panel ([dab48e1](https://github.com/chio-labs/sqltide/commit/dab48e18230527b306bbc0e9e58672f8540d4cc0))
* explain dead letters in the sensor detail panel ([6160f1c](https://github.com/chio-labs/sqltide/commit/6160f1cf0f674d8469afdad51a8b81046ebed5db))
* expose deployment promote, cleanup and diff over the dev API ([03e47f7](https://github.com/chio-labs/sqltide/commit/03e47f7cf40e9f3dfb906f84e8dfa97c577499e7))
* give each sensor a dedicated detail page ([8166bb5](https://github.com/chio-labs/sqltide/commit/8166bb5e56a60a9bdd4957ea79ce6ca370a08546))
* give sqt dev a terminal voice — startup banner and live activity feed ([52b757f](https://github.com/chio-labs/sqltide/commit/52b757fffd643876c57b09a242d3ff581f9c3147))
* harden retention and scope model references ([#194](https://github.com/chio-labs/sqltide/issues/194)) ([a9d66bc](https://github.com/chio-labs/sqltide/commit/a9d66bc564bb825a4b77c09a43d78cefd5195561))
* honest plan numbers, sqt --version, and remaining dev UI gaps ([e39283f](https://github.com/chio-labs/sqltide/commit/e39283f7b7d6dec0d8db7a2c6099512666f21cd3))
* improve CLI plan and error presentation ([eb5cbcb](https://github.com/chio-labs/sqltide/commit/eb5cbcb387e27ed7c11998f5ac4c8a52d8fb323c))
* improve CLI plan and error presentation ([d66d494](https://github.com/chio-labs/sqltide/commit/d66d49478a6e035808b7b8d0db0166c57bc9d4cf))
* improve UI loading and audit scheduling ([a1e663f](https://github.com/chio-labs/sqltide/commit/a1e663f603924a2ce362593f412162e6ea214e0f))
* improve UI loading and scheduler status ([8ac5134](https://github.com/chio-labs/sqltide/commit/8ac5134f0ef529d32dc8e86d25edc0c2e3e15d50))
* improve UI responsiveness and audit operations ([7c94d91](https://github.com/chio-labs/sqltide/commit/7c94d910062ff94441624a9f1e88649c7be9aae1))
* make plan UI mode aware ([90c5f7d](https://github.com/chio-labs/sqltide/commit/90c5f7d3060399caff8d796caa03f774b3e3d4f8))
* make Plan UI mode aware ([91da81d](https://github.com/chio-labs/sqltide/commit/91da81d271c0d6573fe7ca219d8606c73ffe1a34))
* make the tick timeline a zoomable time axis ([d348006](https://github.com/chio-labs/sqltide/commit/d348006366fed08ce424b8d8212c89f71a0f29f0))
* move dead letters into the sensor detail panel ([e84bbb7](https://github.com/chio-labs/sqltide/commit/e84bbb7dbc858781eb1297d0c9abb83e5d7509e1))
* POST /api/build — single-flight subprocess execution with live feed ([9dfe78b](https://github.com/chio-labs/sqltide/commit/9dfe78bcde1af3a640154c524ee8f86cde8dc1b6))
* promote, clean up and diff deployments from the UI ([9ccb04e](https://github.com/chio-labs/sqltide/commit/9ccb04e6f20df2a06cfa07e7a3d21c11799344e1))
* publish append-only SQLTide manifests ([#223](https://github.com/chio-labs/sqltide/issues/223)) ([f4696d8](https://github.com/chio-labs/sqltide/commit/f4696d846740b2a9f8b71b0eeaca97604060bd61))
* rebuild deterministic commerce demo ([#169](https://github.com/chio-labs/sqltide/issues/169)) ([37b4ac0](https://github.com/chio-labs/sqltide/commit/37b4ac07d55990007878a039e5c8ff6c4d4798b2))
* redesign users and sensors pages ([f128d7e](https://github.com/chio-labs/sqltide/commit/f128d7e5c80a5bc34bd4eebc1f1a73e303adeccf))
* remove the direct-build preflight phase and retention coverage state ([1ea97c9](https://github.com/chio-labs/sqltide/commit/1ea97c9a517162e140739ff50d00c4e405908e92))
* render Plan before warehouse planning ([1848e87](https://github.com/chio-labs/sqltide/commit/1848e87c88dfd4b15285aca4916f356dfffd6d41))
* render plan page before planning ([6f4cc65](https://github.com/chio-labs/sqltide/commit/6f4cc65d92d677a33fa5fdff5e5ec77053f82bb8))
* **replay:** add phase-scoped execution settings ([73fd0ab](https://github.com/chio-labs/sqltide/commit/73fd0aba5397f8b52bf760e5ff7c284607525dce))
* **replay:** add phase-scoped execution settings ([179f258](https://github.com/chio-labs/sqltide/commit/179f2588f78d772275bc386ccc04c9e81238d29f))
* **retention:** add schema-aware defaults ([#192](https://github.com/chio-labs/sqltide/issues/192)) ([d29e149](https://github.com/chio-labs/sqltide/commit/d29e149a38da72e58dde3b78ac4c438a78c0002c))
* scheduled Postgres refresh sources (CHI-56) ([ea5a030](https://github.com/chio-labs/sqltide/commit/ea5a030425f633c334b14460f77494b327196abb))
* scope Kafka consumer groups by target ([9190b7f](https://github.com/chio-labs/sqltide/commit/9190b7f6de2676e003bd7ac65975008982c3f215))
* scope Kafka consumer groups by target ([c1ac642](https://github.com/chio-labs/sqltide/commit/c1ac642d7614f00b30b6bf3224083d24ee0b5ee5))
* separate destructive pipeline actions ([#219](https://github.com/chio-labs/sqltide/issues/219)) ([de4813e](https://github.com/chio-labs/sqltide/commit/de4813eea0a5a3375329f0c9cbd8a628e8dddf62))
* show deployment relations and orphans in the physical view ([0c5ae4a](https://github.com/chio-labs/sqltide/commit/0c5ae4a7fac4e32467cfe3e3f4e70a54e9b791ed))
* show executed SQL in run timelines ([3fdab8c](https://github.com/chio-labs/sqltide/commit/3fdab8cfbf00166d9227de8caa6d2046a88bf90e))
* show live run and audit cycle progress ([85f3073](https://github.com/chio-labs/sqltide/commit/85f3073c71d4866d88d5d878391844aaf8fa3545))
* show the switchover model by model on the run page ([e07489f](https://github.com/chio-labs/sqltide/commit/e07489fc3260cafa4b2771c532c5a8460fb1da40))
* show the switchover model by model on the run page ([f9edef7](https://github.com/chio-labs/sqltide/commit/f9edef77a7ccb6fb230d0d542e83b719b2e302e0))
* stop ClickHouse queries when cancelling UI builds ([#237](https://github.com/chio-labs/sqltide/issues/237)) ([e8b34d6](https://github.com/chio-labs/sqltide/commit/e8b34d68c08415589d1da7e2fb1fb5c8aba78325))
* support bounded direct start times ([2980270](https://github.com/chio-labs/sqltide/commit/298027024e28344f02f98b3e8ae3144f29da4e8b))
* support bounded direct start times ([f550181](https://github.com/chio-labs/sqltide/commit/f5501813b23e5e021dc5c0b766a090ad7c44eb00))
* surface virtual deployments in the dev UI ([0ad9ede](https://github.com/chio-labs/sqltide/commit/0ad9ede7ca732b7f9872d2684512faac081196d1))
* **ui:** expandable/modal error viewer for runs and deployments ([1486b15](https://github.com/chio-labs/sqltide/commit/1486b1584a8183d3c0f24570efe2a75d8a4bf5fe))
* **ui:** expandable/modal error viewer for runs and deployments (CHI-52) ([06f9fb3](https://github.com/chio-labs/sqltide/commit/06f9fb3d4886b3ccca3fa052cc9a8ce575f4de64))
* **ui:** improve run history presentation ([#190](https://github.com/chio-labs/sqltide/issues/190)) ([6f91d72](https://github.com/chio-labs/sqltide/commit/6f91d726bd4ad194fa85b91670908851abbb5dc4))


### Bug Fixes

* adopt Console-style payload view, stable columns, and match highlighting ([37a1791](https://github.com/chio-labs/sqltide/commit/37a1791eb236a0359c9a6f7bbe28e17760ef6ff9))
* align deployment inventory columns ([0d1ab42](https://github.com/chio-labs/sqltide/commit/0d1ab42666fe6be7910fa659e0146df25b5d8e10))
* align dev UI with persisted model state ([cda4898](https://github.com/chio-labs/sqltide/commit/cda4898ad97e70ebd6edccbc4f7780d2c0831613))
* align execution UI with runtime state ([b7d3206](https://github.com/chio-labs/sqltide/commit/b7d3206133fee2874fe604b51cd9258c2b44ce9f))
* align execution UI with runtime state ([a8a9529](https://github.com/chio-labs/sqltide/commit/a8a952911ce5e046a06b2a20cc8f4a6bbb5def5b))
* allow disabled authentication on shared bind addresses ([c36e35f](https://github.com/chio-labs/sqltide/commit/c36e35f163dc5938aba19f9e199981e226213231))
* allow disabled authentication on shared bind addresses ([c617431](https://github.com/chio-labs/sqltide/commit/c6174312419a3ec444cc0626840e13452eda40af))
* always show broker timestamp and make message columns sortable ([dbbbfa3](https://github.com/chio-labs/sqltide/commit/dbbbfa3e708080f6c18f80617b84dddbcb7579ff))
* **audits:** defer unmaterialized relations ([353c848](https://github.com/chio-labs/sqltide/commit/353c8481650bf4693858faa35d88abc59aaba9a3))
* **audits:** reconcile stale builds across releases ([c017af1](https://github.com/chio-labs/sqltide/commit/c017af1a14b972e9cc1cbe597ce6df279b801e8f))
* **auth:** resolve proxy identities that a competing writer just linked ([a12e2ac](https://github.com/chio-labs/sqltide/commit/a12e2ac0f77751b9cf8e7278406657378b63302d))
* batch manual audit execution ([#227](https://github.com/chio-labs/sqltide/issues/227)) ([673005c](https://github.com/chio-labs/sqltide/commit/673005c243e8739b7fac50aab1c00a000fe3c914))
* clarify initial deployment publishing ([d981ab4](https://github.com/chio-labs/sqltide/commit/d981ab4973328a2204fd38bd78a2023d50e8da16))
* clarify promotion run events ([7ad5165](https://github.com/chio-labs/sqltide/commit/7ad51650798c33870bcc628b4b3b3555eba7a787))
* clarify stalled run recovery ([1aae953](https://github.com/chio-labs/sqltide/commit/1aae9538067c5b066fc0366148401976957f275d))
* clarify stalled run recovery ([04df4ae](https://github.com/chio-labs/sqltide/commit/04df4ae80912de4623f83cb28bd8abd74275706d))
* **clickhouse:** test against the ClickHouse version production runs ([3f82e9d](https://github.com/chio-labs/sqltide/commit/3f82e9d058519415d94c6b8eb3fd2fea69a1c03f))
* **cli:** resolve --select as a global name list with pipeline/model sugar ([2a695ed](https://github.com/chio-labs/sqltide/commit/2a695ed63398fe41265e0c4a2ef3c9eefdff3fdf))
* **cli:** stop claiming --start-time is virtual only ([d706b06](https://github.com/chio-labs/sqltide/commit/d706b065e95556bdd6f65ce9d96358c51699fcd6))
* **cli:** stop claiming --start-time is virtual only ([21a3805](https://github.com/chio-labs/sqltide/commit/21a3805813fa3883171aecd0f2497643dcfc2940))
* **compiler:** conjoin replay predicates into the outer WHERE clause ([a093028](https://github.com/chio-labs/sqltide/commit/a093028d5078a776d1d57364e3bdd26fe9e08099))
* **compile:** reject prewhere in table models ([94ec8c4](https://github.com/chio-labs/sqltide/commit/94ec8c46594b48664ee000edac8c4965d067fced))
* **compile:** reject prewhere in table models ([facb2c6](https://github.com/chio-labs/sqltide/commit/facb2c63fcf2c93c5b41df39f98923ab8de2ca1f))
* **compiler:** preserve author bytes through replay and shadow SQL rewrites ([89d4bd9](https://github.com/chio-labs/sqltide/commit/89d4bd9a1c67a3016c719d92eb594e3edcfa6455))
* **compiler:** preserve authored SQL bytes in executed database templ… ([a10208e](https://github.com/chio-labs/sqltide/commit/a10208ec54f4c178f18120e1a593ba9e1e33691d))
* **compiler:** preserve authored SQL bytes in executed database templates ([7e2f5ae](https://github.com/chio-labs/sqltide/commit/7e2f5aedad1ce252b2d1b073ba199cb9b19c6131))
* **compiler:** reject raw model relations and scope union CTE visibility ([84a7245](https://github.com/chio-labs/sqltide/commit/84a724568cc598dad938671492d21f285f0189ec))
* **compiler:** stop consuming the retained tree when resolving aliased refs ([3900348](https://github.com/chio-labs/sqltide/commit/3900348dfcc91fbf35b0db0b0ce2f12b8a95e85f))
* complete plan replay window controls ([c9d6478](https://github.com/chio-labs/sqltide/commit/c9d647830184e80f7ff238b34006b0911a9f16de))
* default topics page to managed topics and link topic names ([2075c4e](https://github.com/chio-labs/sqltide/commit/2075c4e65884de43d4daf908195dcbdef4ac369d))
* **destruction:** launch safely into live runs ([#184](https://github.com/chio-labs/sqltide/issues/184)) ([c400df0](https://github.com/chio-labs/sqltide/commit/c400df05f6e64b56e48c570b3ead39246457e1ac))
* **destruction:** move frozen plans to refreshable page ([#182](https://github.com/chio-labs/sqltide/issues/182)) ([52e5526](https://github.com/chio-labs/sqltide/commit/52e5526c4b9b81c9b8dcbeb7b4031c11812e8d65))
* **destruction:** reload reviewed safety policy ([#188](https://github.com/chio-labs/sqltide/issues/188)) ([3c33295](https://github.com/chio-labs/sqltide/commit/3c332951e797a18f0419fe9e018db8dcddbab35c))
* dev UI — dead controls, shallow-routing filters, and fabricated data ([fc144a7](https://github.com/chio-labs/sqltide/commit/fc144a761162765394414de8b9c9e551367966da))
* **dev-server:** keep the state overlay across warehouse refreshes ([0d4b6a4](https://github.com/chio-labs/sqltide/commit/0d4b6a4797846be4348ee23b004b36199f82b731))
* **dev-server:** keep the state overlay across warehouse refreshes ([8d0b1a4](https://github.com/chio-labs/sqltide/commit/8d0b1a4a08743ff10c63c0a6e2667fd280e76f23))
* **dev-server:** only force a snapshot rebuild on explicit refresh ([b5ab66c](https://github.com/chio-labs/sqltide/commit/b5ab66cc14da3695111b96d3354ab47c6250ee2f))
* **dev-server:** serialize nested connection settings ([dd7aada](https://github.com/chio-labs/sqltide/commit/dd7aadac75e7c72ff79e9ff029dbe672621e3d85))
* **dev-server:** serialize nested connection settings ([e569ac6](https://github.com/chio-labs/sqltide/commit/e569ac6f3df3f3d587189c89e4c892604d5b9d91))
* distinguish unbuilt Kafka sources from consumer errors ([#225](https://github.com/chio-labs/sqltide/issues/225)) ([b272015](https://github.com/chio-labs/sqltide/commit/b272015144c32f6066f0f201d4f27ca59f462da3))
* drain sensor event backlogs ([#172](https://github.com/chio-labs/sqltide/issues/172)) ([f704572](https://github.com/chio-labs/sqltide/commit/f7045725d0a669132a3c601a4e8ed4e999aee288))
* **e2e:** expect the sqlbuild-style select list the run dialog builds ([6b5474c](https://github.com/chio-labs/sqltide/commit/6b5474c09293146bf1835903a2e5b6c5aa60d359))
* expose named target to sensor events ([#157](https://github.com/chio-labs/sqltide/issues/157)) ([20416a1](https://github.com/chio-labs/sqltide/commit/20416a19594efc04490ab0c809936130a096d448))
* filter offset replays at physical source ([#238](https://github.com/chio-labs/sqltide/issues/238)) ([fc3751d](https://github.com/chio-labs/sqltide/commit/fc3751d0134e22bd1b105a3851a95774049e00c2))
* gate topics navigation on the first inventory load ([72dfd5a](https://github.com/chio-labs/sqltide/commit/72dfd5a2a1560d7316cf7285dbb85a8ad10372c9))
* guard empty Release Please outputs ([d90266a](https://github.com/chio-labs/sqltide/commit/d90266a6418ea7e3429c7ffcaba9d4a1252c9f00))
* guard empty Release Please outputs ([41249c7](https://github.com/chio-labs/sqltide/commit/41249c78626c2f1b8ae8da6442d53e5719978d94))
* harden API and missing run states ([2879b67](https://github.com/chio-labs/sqltide/commit/2879b671b9f006a61f38ae5e7bd37be243e491e5))
* harden authentication and authorization boundaries ([2a6a0c6](https://github.com/chio-labs/sqltide/commit/2a6a0c6270db3b68cc22575e6d3122f3daf8a6fd))
* harden lineage rebuild safety ([adda653](https://github.com/chio-labs/sqltide/commit/adda65363bbb445195da0e2038554caffdb744ed))
* harden lineage rebuild safety ([cd849ae](https://github.com/chio-labs/sqltide/commit/cd849ae0c4caa6e13baf9c23f189a52baf1f4ebc))
* honest timestamps, live runs list, and Ctrl+C leaves a record ([571f730](https://github.com/chio-labs/sqltide/commit/571f730c3594a651200a2acc3ba6565b8d52c91f))
* humanize run event timeline ([2e6b0ac](https://github.com/chio-labs/sqltide/commit/2e6b0acf24c70b2bf5d0c297e5b7ec02e191c2e0))
* keep dev app startup warmer within comment policy ([1758fe6](https://github.com/chio-labs/sqltide/commit/1758fe64e1b32022732de9494a000c27439807a9))
* keep dev app startup warmer within comment policy ([655aa51](https://github.com/chio-labs/sqltide/commit/655aa51afc23f16b91367a1f712c499541fdeaf4))
* keep release lockfile synchronized ([b01957c](https://github.com/chio-labs/sqltide/commit/b01957c88527ac4c3cab344a24850ef8f3a932ba))
* keep release lockfile synchronized ([550e3a8](https://github.com/chio-labs/sqltide/commit/550e3a86bbed2e983f8db7557e2d4fa7e3446a23))
* keep snapshot refresh responsive ([#206](https://github.com/chio-labs/sqltide/issues/206)) ([9d9b51d](https://github.com/chio-labs/sqltide/commit/9d9b51d4595a1968d1186dbe9665d5f875a8be1d))
* keep the dev UI available through warehouse outages ([8be2cdd](https://github.com/chio-labs/sqltide/commit/8be2cdd5d01ba8f87e7b30d976264d7345dab562))
* keep the dev UI available through warehouse outages ([a838c9c](https://github.com/chio-labs/sqltide/commit/a838c9cbb65e12ab658e5b7c46dc26849e198559))
* keep the topics inventory across navigations ([0da24e5](https://github.com/chio-labs/sqltide/commit/0da24e5162dfd67c974e2fb69d8532eeb4e68f9b))
* label the overview source card row count as rows ([7f7a326](https://github.com/chio-labs/sqltide/commit/7f7a326a90fa5a08b39ffaa6039615488e4e1e2b))
* label the overview source card row count as rows ([e0b6140](https://github.com/chio-labs/sqltide/commit/e0b61404531ea56dcae90836d8ed1616e9011880))
* **lifecycle:** restore association-driven destruction ([#180](https://github.com/chio-labs/sqltide/issues/180)) ([ac47e83](https://github.com/chio-labs/sqltide/commit/ac47e83e53f766bbfc52d122c99c65a96c187c41))
* make audit warning alerts durable ([#155](https://github.com/chio-labs/sqltide/issues/155)) ([852058a](https://github.com/chio-labs/sqltide/commit/852058a64638fbaf4109030a8c7ea57487e51908))
* make core UI usable on mobile ([36e878c](https://github.com/chio-labs/sqltide/commit/36e878cba836d83e8a0500cd696e952637a66b96))
* make physical lineage responsive at scale ([#232](https://github.com/chio-labs/sqltide/issues/232)) ([2e396c8](https://github.com/chio-labs/sqltide/commit/2e396c84c00e3970726cbc83015d3dd6d631e7a9))
* make sensor alerts immediately actionable ([#163](https://github.com/chio-labs/sqltide/issues/163)) ([3d62149](https://github.com/chio-labs/sqltide/commit/3d621493028a0ff2273652cbee0c07fe80958f62))
* make the orders demo quickstart reliable ([#167](https://github.com/chio-labs/sqltide/issues/167)) ([0fbf256](https://github.com/chio-labs/sqltide/commit/0fbf256032092ab5dd5c7b66d65693a03ef12e29))
* measure virtual-mode models by the relation they are bound to ([b7d35a7](https://github.com/chio-labs/sqltide/commit/b7d35a71c7417858b2053da87f272993dd3e7b8a))
* normalize warehouse table typography ([#229](https://github.com/chio-labs/sqltide/issues/229)) ([814bef6](https://github.com/chio-labs/sqltide/commit/814bef654f31fdcdc3eab433adac06765d6412de))
* **observability:** persist full run errors and enlarge the error dialog ([7449695](https://github.com/chio-labs/sqltide/commit/7449695d3d71118096a33eaf3ce91e9a687a0c5c))
* **observability:** persist full run errors and enlarge the error dialog ([8ae5cc9](https://github.com/chio-labs/sqltide/commit/8ae5cc946e21bd06edb825020d18dafbb87fb3fb))
* **observability:** skip run-statement persistence when the adapter renders none ([d8367e3](https://github.com/chio-labs/sqltide/commit/d8367e33595a469b23ae98e19c4abe2dffc181ff))
* offer rollback on superseded deployments ([ce8b879](https://github.com/chio-labs/sqltide/commit/ce8b87996628ae9de3587d9ce547cfd342712429))
* package dev UI assets in distributions ([5416422](https://github.com/chio-labs/sqltide/commit/5416422c445de907408709999a4c5d07d4c4c674))
* package dev UI assets in distributions ([8649bf6](https://github.com/chio-labs/sqltide/commit/8649bf6a86852f971c8dc7d20d0b90c3ea06effd))
* paginate the message list and default to broker timestamp order ([cf29102](https://github.com/chio-labs/sqltide/commit/cf29102f0283d5bef96c12cf8dda3f0719143d1e))
* pin development Python to 3.12 ([b02d6f2](https://github.com/chio-labs/sqltide/commit/b02d6f2e1896d357c40fd31921f7d425d0e99ca1))
* preserve lineage activity and viewport ([4bdd6f8](https://github.com/chio-labs/sqltide/commit/4bdd6f86f0904d820628d1c46c00fda85dbedff2))
* preserve lineage activity and viewport ([6b02e84](https://github.com/chio-labs/sqltide/commit/6b02e84f688c6d883af8df1cbcd0c78cb37829c5))
* preserve refreshable view scheduling ([#204](https://github.com/chio-labs/sqltide/issues/204)) ([b5d36df](https://github.com/chio-labs/sqltide/commit/b5d36df2fcf8fefb8ebda66c3a2698d0bfad542e))
* preserve UTC replay start times ([aa97154](https://github.com/chio-labs/sqltide/commit/aa9715407167ec9e336811231115e0662fa09ec3))
* prevent managed source replay overlap ([#200](https://github.com/chio-labs/sqltide/issues/200)) ([59662b2](https://github.com/chio-labs/sqltide/commit/59662b2eb1a14b981c56f8aad574f671feaa190f))
* raise the full-record cap to 16 MiB ([e14b0c4](https://github.com/chio-labs/sqltide/commit/e14b0c4f6589bcfa9103157214bf4c8c51660a0c))
* refresh lockfile project version ([1595195](https://github.com/chio-labs/sqltide/commit/15951956e632d31b4beacc423c27349c69ca296f))
* **release:** satisfy protected release merges ([#140](https://github.com/chio-labs/sqltide/issues/140)) ([9861f29](https://github.com/chio-labs/sqltide/commit/9861f29ebc4c6d3cd2d02b4d544bccc7a214fa49))
* **release:** use checked out release head ([#142](https://github.com/chio-labs/sqltide/issues/142)) ([a367019](https://github.com/chio-labs/sqltide/commit/a3670195adbd276a0d4cab98bde7f7314da99637))
* **replay:** filter non-lineage roots at source ([73b6121](https://github.com/chio-labs/sqltide/commit/73b6121b31f82af129fee6dec201acfd047318ad))
* **replay:** filter non-lineage roots at source ([8528672](https://github.com/chio-labs/sqltide/commit/8528672a10c0c1f7ec761e7221b3c92e2fbb62f5))
* reset offsets for fresh source landings ([969d958](https://github.com/chio-labs/sqltide/commit/969d9580d7daa7596a8d0828f30ea5ab8cd4cedd))
* reset offsets for fresh source landings ([2c48f56](https://github.com/chio-labs/sqltide/commit/2c48f5695066b2c0c291b99fbe232b5584658da0))
* reuse Kafka metadata clients ([#214](https://github.com/chio-labs/sqltide/issues/214)) ([ebae1b0](https://github.com/chio-labs/sqltide/commit/ebae1b0eedd7f0ef8e1cda8232f3158c8aae07ae))
* scope active build conflicts ([4b5b268](https://github.com/chio-labs/sqltide/commit/4b5b268fb91304bc38754a66253e4334fffb8a61))
* scope direct source preparation to selection ([93a86d8](https://github.com/chio-labs/sqltide/commit/93a86d81e5904e9687000b5fb503e0aa7684698e))
* scope direct source preparation to selection ([4801dc2](https://github.com/chio-labs/sqltide/commit/4801dc21cd8fcafa3bb30d8b3e5f243450c080a6))
* serialize immutable mappings in fingerprints ([0a385bb](https://github.com/chio-labs/sqltide/commit/0a385bb8c8006a2f279d1d3e1ede273c1f1f8f16))
* serialize immutable mappings in fingerprints ([07143da](https://github.com/chio-labs/sqltide/commit/07143da2f846097cca0dd09bb10c412b7c2c30d2))
* show full run ID on detail page ([77d0c7e](https://github.com/chio-labs/sqltide/commit/77d0c7e52ba42167f88495f568755ca3fe85dfe6))
* show full run ID on detail page ([aab43b5](https://github.com/chio-labs/sqltide/commit/aab43b552033258b1259d6c9435057fe4e21d01a))
* sqlbuild-style --select (name lists + bare pipeline names) and safe selection ([e1bd258](https://github.com/chio-labs/sqltide/commit/e1bd258121979c3f2a903b730ba23a22dfbeeac1))
* stabilise message browser layout and adopt debounced auto-search ([617d4bb](https://github.com/chio-labs/sqltide/commit/617d4bb84cc7154c63eb066937cd9d56e23a9c27))
* stabilize scheduler and run state ([d6022ad](https://github.com/chio-labs/sqltide/commit/d6022ad07d50b63ce1faa83b2bfc0881780d09d7))
* stabilize UI loading transitions ([90f4529](https://github.com/chio-labs/sqltide/commit/90f4529135727d74a151e90cee0aaf681d79b6d7))
* stabilize UI loading transitions ([c61ef28](https://github.com/chio-labs/sqltide/commit/c61ef28fb91c16bc2106314f8779c02858fa9b8e))
* support direct metadata reconciliation ([#210](https://github.com/chio-labs/sqltide/issues/210)) ([73d425a](https://github.com/chio-labs/sqltide/commit/73d425a6da3bb5af0369e01fd7580f37614796b9))
* surface pending warehouse outages ([b9914f0](https://github.com/chio-labs/sqltide/commit/b9914f0ec01c81967d15866b3c3c01eeaa1537a3))
* surface pending warehouse outages ([77ac6d9](https://github.com/chio-labs/sqltide/commit/77ac6d9618c72df728b31e4cf7b0545aeb821ad1))
* **ui:** accept --select lists and bare pipeline names; generate one --select ([36fd92a](https://github.com/chio-labs/sqltide/commit/36fd92a42ca96a87065abac44426d2b390c0107e))
* **ui:** clarify dead-letter retry progress ([#160](https://github.com/chio-labs/sqltide/issues/160)) ([d182d94](https://github.com/chio-labs/sqltide/commit/d182d94fa830f2eb8fe479ddc77ef68820ef901a))
* **ui:** defer project shell until bootstrap completes ([9edc3ea](https://github.com/chio-labs/sqltide/commit/9edc3eac75e7fd29e2e07853559bac5981bd5de6))
* **ui:** derive plan command locally and gate stale plan behind loading ([f9d30a6](https://github.com/chio-labs/sqltide/commit/f9d30a62395ce17975c4c2655ccde582fdb5709e))
* **ui:** derive plan command locally and gate stale plan behind loading ([15f9ba5](https://github.com/chio-labs/sqltide/commit/15f9ba51d7ea23ab6e16a7b3f26f5ccbd24fa41a))
* **ui:** improve run and deployment loading ([#149](https://github.com/chio-labs/sqltide/issues/149)) ([b1ecff7](https://github.com/chio-labs/sqltide/commit/b1ecff7ec391bbbb74a65469e10892221e828a67))
* **ui:** keep history reads responsive ([#151](https://github.com/chio-labs/sqltide/issues/151)) ([2295f85](https://github.com/chio-labs/sqltide/commit/2295f85fd4bbefe7f5c6ab45eb951fbcbfa575b7))
* **ui:** keep sensor loading responsive ([#153](https://github.com/chio-labs/sqltide/issues/153)) ([df1ffd8](https://github.com/chio-labs/sqltide/commit/df1ffd82c8f22683e3831ae41809d3380664e37b))
* **ui:** never render a stale plan behind a plan error ([019c32a](https://github.com/chio-labs/sqltide/commit/019c32a33e5b3ce8fa37a668997814f0b8b27c53))
* **ui:** prevent runs startup refresh loop ([62290c3](https://github.com/chio-labs/sqltide/commit/62290c326b1e51975f563a4e5a6c9f303ba5aacb))
* **ui:** refine quality and run history ([#144](https://github.com/chio-labs/sqltide/issues/144)) ([36a1452](https://github.com/chio-labs/sqltide/commit/36a14521e1c3a739e28dd6cb931ca0a88a52b1b2))
* **ui:** require cached definitions for conditional reads ([fb37d33](https://github.com/chio-labs/sqltide/commit/fb37d33b5b6f00fd7ddabd1b85a514f407099ecd))
* **ui:** show loading spinner instead of compile flash on live run de… ([1b745c6](https://github.com/chio-labs/sqltide/commit/1b745c615f80682f4358236fb3a18245c0cfd2e2))
* **ui:** show loading spinner instead of compile flash on live run detail ([42348b7](https://github.com/chio-labs/sqltide/commit/42348b7a75351144cf2f93cd77b9ef978e971035))
* **ui:** suppress transient telemetry warnings ([#136](https://github.com/chio-labs/sqltide/issues/136)) ([1b5fb27](https://github.com/chio-labs/sqltide/commit/1b5fb27e298aa91a6e6a7f944202172948b9eefb))
* **ui:** tolerate definitions cache limits ([#146](https://github.com/chio-labs/sqltide/issues/146)) ([fcdd830](https://github.com/chio-labs/sqltide/commit/fcdd8307dcbe9870a6709bf747115ac30616f9ac))
* update public defaults and examples ([0160843](https://github.com/chio-labs/sqltide/commit/01608432ac0614ff7dc3f153f22d1d57ae6b6046))
* warm broker metadata caches at dev server startup ([96ed64b](https://github.com/chio-labs/sqltide/commit/96ed64b6b13cd75274db6bb2dfb624c11fe73fbf))


### Performance Improvements

* **auth:** cache resolved request identities ([3c07f21](https://github.com/chio-labs/sqltide/commit/3c07f21b424f548eba7264c14d68c46a311495e4))
* **compiler:** collect every model tree fact in one traversal ([6895828](https://github.com/chio-labs/sqltide/commit/68958289c9827f29c3bd92dd3f4a80e4ef403b35))
* **compiler:** reach SQLBuild compile parity by removing unread analysis work ([d7c19df](https://github.com/chio-labs/sqltide/commit/d7c19df9e406015f03fd6570125c430b3cf162f0))
* **compiler:** resolve each model tree in one traversal ([6d72c72](https://github.com/chio-labs/sqltide/commit/6d72c72bb714f12d8a82b826d27fc6e96e03ff96))
* **compiler:** resolve references by substitution instead of rendering ([1b2c9da](https://github.com/chio-labs/sqltide/commit/1b2c9daeb9ad566188d6bc656f1c6b48769e5b87))
* **compiler:** restore the dropped SQLBuild scanner skip and stop re-walking trees ([9851165](https://github.com/chio-labs/sqltide/commit/9851165ff5806a449be123120aa58929b5cc9487))
* **compiler:** restore the reference scanner skip and stop deep copying trees ([2ff4298](https://github.com/chio-labs/sqltide/commit/2ff42985944671ae11b8702a6431087996314242))
* **compiler:** stop rendering canonical SQL that nothing reads ([3e18dea](https://github.com/chio-labs/sqltide/commit/3e18deaccf183c2bedd661eac6b0ba537c0cbd4a))
* **compiler:** walk each parsed model tree once per purpose ([da19fa6](https://github.com/chio-labs/sqltide/commit/da19fa64fff4ce45733d82ce4cc3988f82524ace))
* **dev-server:** build the warehouse overlay off the shared query lock ([85641d7](https://github.com/chio-labs/sqltide/commit/85641d7dead011e75ad81472d94173f4a59bfa8d))
* **dev-server:** build the warehouse overlay off the shared query lock ([83022af](https://github.com/chio-labs/sqltide/commit/83022af3842b11d31eac35298ee9ef191e7cf9bf))
* **dev-server:** serve one background-refreshed warehouse overlay ([2bb6dd8](https://github.com/chio-labs/sqltide/commit/2bb6dd8bfbb5a97f5392173150c5658d3007b2fa))
* **dev-server:** serve one background-refreshed warehouse overlay ([117dfdf](https://github.com/chio-labs/sqltide/commit/117dfdff1e8c0ea363bef0f6ec7561605447d8bb))
* **plan:** isolate reads and defer replay counts ([1c88cd2](https://github.com/chio-labs/sqltide/commit/1c88cd2ef1b30a65f4f7acbeab74ae9711cf2d77))
* reduce snapshot refresh warehouse work ([#208](https://github.com/chio-labs/sqltide/issues/208)) ([868ba80](https://github.com/chio-labs/sqltide/commit/868ba80b9392029f05e01cdaefe476bb297461f6))
* **ui:** deduplicate live refresh requests ([7374c6a](https://github.com/chio-labs/sqltide/commit/7374c6a5b6321540d9a94d2c812c7ad10e9fd914))
* **ui:** initialize the project in one request ([683e4a3](https://github.com/chio-labs/sqltide/commit/683e4a38778dd34748163950a29cae2986d0e6eb))
* **ui:** initialize the project in one request ([f1c2533](https://github.com/chio-labs/sqltide/commit/f1c2533d3eb5d4fe180a5ca90d140ae5985c2ebc))
* **ui:** render before secondary warehouse data loads ([7f0b28c](https://github.com/chio-labs/sqltide/commit/7f0b28c76c6b7060057d39b657b867791a5dc4d1))
* **ui:** render before secondary warehouse data loads ([aec3cf7](https://github.com/chio-labs/sqltide/commit/aec3cf78a61620e6e66efcb745a093915f9b85cc))
* **ui:** split auth and cache definitions ([d13d720](https://github.com/chio-labs/sqltide/commit/d13d720758fad304d228369b2017e547135e8451))
* **ui:** standardize cached page navigation ([65331f6](https://github.com/chio-labs/sqltide/commit/65331f68c010730d064f6c7101e67313819a56d6))


### Refactoring

* align direct replay and dev execution ([96a0973](https://github.com/chio-labs/sqltide/commit/96a097387333f0ddb3428b9d956d0faba7ebc8e7))
* align run SQL timeline with formatter and lint conventions ([64fcd42](https://github.com/chio-labs/sqltide/commit/64fcd42ce2ffbf723d86112b1b4c6f8f91db9e34))
* drop the unused RelationStorage copy from the deployment domain ([f61b21d](https://github.com/chio-labs/sqltide/commit/f61b21dd2df8d23c0251fde2f5d1a3519d09c663))
* make direct mode state independent ([54f7ec2](https://github.com/chio-labs/sqltide/commit/54f7ec24eaa6c3b8c6553cbc9e7e7e8e155a365b))
* **release:** centralize auto-merge ([#176](https://github.com/chio-labs/sqltide/issues/176)) ([e249c71](https://github.com/chio-labs/sqltide/commit/e249c71a76fc848c1bcd60caa0cff9721cc19086))
* remove direct ownership semantics ([dc7caa4](https://github.com/chio-labs/sqltide/commit/dc7caa49b54a0c206c50a754b2e23a6a4078a769))
* **tests:** name the ClickHouse image once for every suite ([91c8209](https://github.com/chio-labs/sqltide/commit/91c82095364af322ab5de4f7d484b1ee4c41bce5))
* **ui:** enforce Fensu Svelte architecture ([defbab7](https://github.com/chio-labs/sqltide/commit/defbab71425ab2a4cce9e5f6a528531d71dcf744))
* **ui:** enforce Fensu Svelte architecture ([f2b0363](https://github.com/chio-labs/sqltide/commit/f2b0363355caf5e43c7ec3cf0a7791156c6594c0))
* **ui:** make the error dialog width content-dynamic (match Dagster) ([55b5d10](https://github.com/chio-labs/sqltide/commit/55b5d10768f81f1ec3448274d7f2d2d91cbfce67))


### Build System

* **release:** standardize automated releases ([#138](https://github.com/chio-labs/sqltide/issues/138)) ([2e22c7e](https://github.com/chio-labs/sqltide/commit/2e22c7e7fd682aca1557b08ec9685aea633d6ccd))


### Documentation

* require watching auto-merge completion ([#234](https://github.com/chio-labs/sqltide/issues/234)) ([e15ce2b](https://github.com/chio-labs/sqltide/commit/e15ce2b0fd22217f3f3e1c624e77e6dcd0d856a6))
* show full logo in readme ([#159](https://github.com/chio-labs/sqltide/issues/159)) ([0e2be8a](https://github.com/chio-labs/sqltide/commit/0e2be8ac6b16ea19d57936870571416489ad4016))
* streamline project overview ([a6371d0](https://github.com/chio-labs/sqltide/commit/a6371d01ad6eccefbd4e19b3a4ea74db76af36e5))
* tighten pipeline naming guidance ([e6a6d4c](https://github.com/chio-labs/sqltide/commit/e6a6d4c4535bd39877421f5dc363f835cd1f4f64))
* tighten readme logo framing ([#165](https://github.com/chio-labs/sqltide/issues/165)) ([6147936](https://github.com/chio-labs/sqltide/commit/6147936fcc17908352579c73fe540ad764aa0849))


### Maintenance

* **main:** release 0.10.0 ([6091d04](https://github.com/chio-labs/sqltide/commit/6091d048d2e98d06b77ccdeb2792f80610949862))
* **main:** release 0.10.0 ([1bdea3b](https://github.com/chio-labs/sqltide/commit/1bdea3bea9242d6bbf84ce9a0eba75578445b1f5))
* **main:** release 0.11.0 ([9ef2592](https://github.com/chio-labs/sqltide/commit/9ef259215ba6b43c50d08142d06898ea6eb6b3a6))
* **main:** release 0.11.0 ([2f2bb6c](https://github.com/chio-labs/sqltide/commit/2f2bb6c203323099d9659cca7e0b32347c771bff))
* **main:** release 0.12.0 ([e5600d6](https://github.com/chio-labs/sqltide/commit/e5600d64d41dcf071958fb56535ad5249eb5fda4))
* **main:** release 0.12.0 ([b0c854a](https://github.com/chio-labs/sqltide/commit/b0c854a9db99e90eb0a1fc4ed46c912393efc55f))
* **main:** release 0.12.1 ([84d303a](https://github.com/chio-labs/sqltide/commit/84d303a820fb45f9dc04c82ede5d2b30c1ed62bd))
* **main:** release 0.12.1 ([dea5b87](https://github.com/chio-labs/sqltide/commit/dea5b87e72d3f4bdd25b623dfa182d9a0b27ece5))
* **main:** release 0.12.2 ([ced1ea6](https://github.com/chio-labs/sqltide/commit/ced1ea6ba977820ebbeae9953c437d5d4264e740))
* **main:** release 0.12.2 ([fbd353f](https://github.com/chio-labs/sqltide/commit/fbd353f4d158ec391c5ea00030f62f1455c4f2a2))
* **main:** release 0.12.3 ([bb6836a](https://github.com/chio-labs/sqltide/commit/bb6836a823c61c67c8769c566c361e7ef5ff1a14))
* **main:** release 0.12.3 ([3e8bbfe](https://github.com/chio-labs/sqltide/commit/3e8bbfe46adaccbc553c7381366b20ec9aea04e8))
* **main:** release 0.12.4 ([5079b3f](https://github.com/chio-labs/sqltide/commit/5079b3f34529fe16a01fe208e5d4cabb6ef7e69b))
* **main:** release 0.12.4 ([9ca33f5](https://github.com/chio-labs/sqltide/commit/9ca33f5d3d75cd43cc314e0e0dfce768ee085760))
* **main:** release 0.13.0 ([27c8c1f](https://github.com/chio-labs/sqltide/commit/27c8c1f41d941db2c6451852b44184d1cc198a47))
* **main:** release 0.13.0 ([d3c6158](https://github.com/chio-labs/sqltide/commit/d3c61581d0e81354b867ef66293c5f4c63d310dd))
* **main:** release 0.14.0 ([0067523](https://github.com/chio-labs/sqltide/commit/00675239e9814ef99da7b1e525073fe9032a7b6b))
* **main:** release 0.14.0 ([622dc3d](https://github.com/chio-labs/sqltide/commit/622dc3df9e10dea2080aeccfe9c4ea7a06c3c91c))
* **main:** release 0.14.1 ([034e219](https://github.com/chio-labs/sqltide/commit/034e21930a4274d362e6252dfade9eb85275e4ad))
* **main:** release 0.14.1 ([ed2a73b](https://github.com/chio-labs/sqltide/commit/ed2a73b519ad0ff9c922d6791bd9628451e8d69a))
* **main:** release 0.15.0 ([756dd6f](https://github.com/chio-labs/sqltide/commit/756dd6f525d623e8dc79b6f910eb24c7f392ae73))
* **main:** release 0.15.0 ([a1ce07a](https://github.com/chio-labs/sqltide/commit/a1ce07a1e251e47d04476f13d955f3e73c290db6))
* **main:** release 0.16.0 ([b8414e4](https://github.com/chio-labs/sqltide/commit/b8414e47ffa3d538923193bd34e312ea1b01b1ec))
* **main:** release 0.16.0 ([5b6bd9b](https://github.com/chio-labs/sqltide/commit/5b6bd9babd90f2f5157371d13dead99f86fa1a33))
* **main:** release 0.16.1 ([2a2262f](https://github.com/chio-labs/sqltide/commit/2a2262fdeef11756500a4c8acb8bae36d6a7af6d))
* **main:** release 0.16.1 ([a89b98b](https://github.com/chio-labs/sqltide/commit/a89b98b5228769f5a86036268a49026f9be84c27))
* **main:** release 0.16.2 ([fa0f3f4](https://github.com/chio-labs/sqltide/commit/fa0f3f4abcc1651624df610acd88ff4cc682a076))
* **main:** release 0.16.2 ([58eaecb](https://github.com/chio-labs/sqltide/commit/58eaecbb556d5303ce065c8017c556fae7773724))
* **main:** release 0.16.3 ([97bb70b](https://github.com/chio-labs/sqltide/commit/97bb70bd26354e52e2e59f0c1840656815fa9507))
* **main:** release 0.16.3 ([65da2c2](https://github.com/chio-labs/sqltide/commit/65da2c2e473524ca980744e44d342a73de56b3aa))
* **main:** release 0.16.4 ([d7dde90](https://github.com/chio-labs/sqltide/commit/d7dde90a49b174c396ad4d1c04756725a6c0e3ac))
* **main:** release 0.16.4 ([b722d09](https://github.com/chio-labs/sqltide/commit/b722d09934d073059904f7f7b4210ab741082100))
* **main:** release 0.16.5 ([987693d](https://github.com/chio-labs/sqltide/commit/987693d9a6123bd743568358f3cdf7087ce04f5f))
* **main:** release 0.16.5 ([0ee6e4e](https://github.com/chio-labs/sqltide/commit/0ee6e4e0f0970b101cc8126308bb211e1564498c))
* **main:** release 0.16.6 ([b869edb](https://github.com/chio-labs/sqltide/commit/b869edb46f140861b8507cf57170551f544eed03))
* **main:** release 0.16.6 ([e75a856](https://github.com/chio-labs/sqltide/commit/e75a8562efffdeea464cdeb4c825cf32bd63684d))
* **main:** release 0.17.0 ([f630bc2](https://github.com/chio-labs/sqltide/commit/f630bc25d631ee2f34889cae1a6e75750f6b3459))
* **main:** release 0.17.0 ([c9b05ff](https://github.com/chio-labs/sqltide/commit/c9b05ffa6511ce4b112cd689ec09c4315381c717))
* **main:** release 0.18.0 ([d16521b](https://github.com/chio-labs/sqltide/commit/d16521b8a1da59557c1ade148555c0319edca6b6))
* **main:** release 0.18.0 ([ed0ce96](https://github.com/chio-labs/sqltide/commit/ed0ce96d8ae3a73fbe0e0281ad79730a7eeb0fb4))
* **main:** release 0.18.1 ([f2c507f](https://github.com/chio-labs/sqltide/commit/f2c507fbfc6336953c7def4c29f63c6e13709aa7))
* **main:** release 0.18.1 ([2f9cfa4](https://github.com/chio-labs/sqltide/commit/2f9cfa4d176fb12b09490b6233fd3e0bad585b3f))
* **main:** release 0.19.0 ([b740855](https://github.com/chio-labs/sqltide/commit/b740855f4260b8f730e0cf2181684471a6bc31f0))
* **main:** release 0.19.0 ([a887a28](https://github.com/chio-labs/sqltide/commit/a887a28ff643494bd350883eeb21b4922488fd78))
* **main:** release 0.19.1 ([53a1dee](https://github.com/chio-labs/sqltide/commit/53a1dee0d14c8ce765fa85b887ffcfa36d390508))
* **main:** release 0.19.1 ([9a05197](https://github.com/chio-labs/sqltide/commit/9a05197ae8a80009a293fa96ccf18dd48c89369b))
* **main:** release 0.20.0 ([7507096](https://github.com/chio-labs/sqltide/commit/7507096ac229539ca2bf80589aeadbc18b9d19ad))
* **main:** release 0.20.0 ([9496064](https://github.com/chio-labs/sqltide/commit/949606422487ffbbf2d263639acabf54371d16cc))
* **main:** release 0.21.0 ([64849fa](https://github.com/chio-labs/sqltide/commit/64849fa5d1af03d7b079d76b3be393df7d0c3e41))
* **main:** release 0.21.0 ([f06245f](https://github.com/chio-labs/sqltide/commit/f06245f0ea66c377ca5d1070b2a4254edefd7df8))
* **main:** release 0.21.1 ([813bf4d](https://github.com/chio-labs/sqltide/commit/813bf4d0bbf118d866badcb6eb75c0f1a4400f6d))
* **main:** release 0.21.1 ([67999e9](https://github.com/chio-labs/sqltide/commit/67999e9b36629ec638ad86db4671dd307ff6899c))
* **main:** release 0.21.2 ([51ea738](https://github.com/chio-labs/sqltide/commit/51ea738054597510affa740b02007a3c234a5560))
* **main:** release 0.21.2 ([64db3e8](https://github.com/chio-labs/sqltide/commit/64db3e87b1bdedcdfc8a687843fe2872e3a6b1ec))
* **main:** release 0.21.3 ([6dc4308](https://github.com/chio-labs/sqltide/commit/6dc4308cb2248b5338ca853a247ee305c7349f6f))
* **main:** release 0.21.3 ([12b88e4](https://github.com/chio-labs/sqltide/commit/12b88e4c14924cb12fa44e4bb1d20d64292d5c7d))
* **main:** release 0.22.0 ([bea7c39](https://github.com/chio-labs/sqltide/commit/bea7c39e57739ad2d01fdf13ef86cda0d94a4c63))
* **main:** release 0.22.0 ([4ab30c2](https://github.com/chio-labs/sqltide/commit/4ab30c2f3a0a9091e09104a0bd285d3bb84f8a58))
* **main:** release 0.22.1 ([73d96e5](https://github.com/chio-labs/sqltide/commit/73d96e56a8ca72c7dd5979221909889fcffbf5bd))
* **main:** release 0.22.1 ([f4b7a3a](https://github.com/chio-labs/sqltide/commit/f4b7a3a51ded7c135c4c1f7620335779ba8ce973))
* **main:** release 0.22.2 ([83b03e3](https://github.com/chio-labs/sqltide/commit/83b03e3fb5ecea7ef3c2ef8b8252ca0cd928f2a6))
* **main:** release 0.22.2 ([4413cbc](https://github.com/chio-labs/sqltide/commit/4413cbc57bce017f8881955d1ee93b0895e37ba8))
* **main:** release 0.22.3 ([68d2df2](https://github.com/chio-labs/sqltide/commit/68d2df2755387dcdf1e3ac18bdf2d32871508eb7))
* **main:** release 0.22.3 ([7abc919](https://github.com/chio-labs/sqltide/commit/7abc9199babc19af169095a6f35ec43676714513))
* **main:** release 0.22.4 ([aad3b64](https://github.com/chio-labs/sqltide/commit/aad3b6482d3484989bd525b12b5a519eba2edb2b))
* **main:** release 0.22.4 ([a8cbb7c](https://github.com/chio-labs/sqltide/commit/a8cbb7ce1f841eda22c16554bfc76924e33d2dea))
* **main:** release 0.23.0 ([92381c1](https://github.com/chio-labs/sqltide/commit/92381c1e28c5c863a61e73ff04db24dded5094e6))
* **main:** release 0.23.0 ([da9e791](https://github.com/chio-labs/sqltide/commit/da9e7910eca110424d44e05b9271e1fe6ef8c5ed))
* **main:** release 0.24.0 ([1765c47](https://github.com/chio-labs/sqltide/commit/1765c4734da36b5c76344324d69f15a2335039ea))
* **main:** release 0.24.0 ([f547aa5](https://github.com/chio-labs/sqltide/commit/f547aa5aa6846877f2080e728798ddcdd0dbde23))
* **main:** release 0.24.1 ([d0a0a39](https://github.com/chio-labs/sqltide/commit/d0a0a390db68cae89e0896d4f276bc74d99fc90d))
* **main:** release 0.24.1 ([30622d0](https://github.com/chio-labs/sqltide/commit/30622d0d0b8c39bd943f463aef570e35a434b23b))
* **main:** release 0.24.2 ([4987f1c](https://github.com/chio-labs/sqltide/commit/4987f1c46affc4d71669bce3f5f6e6e31b9904c1))
* **main:** release 0.24.2 ([cb25b1f](https://github.com/chio-labs/sqltide/commit/cb25b1f44e3764aa3760c580df604515ede124a4))
* **main:** release 0.24.3 ([cec8d39](https://github.com/chio-labs/sqltide/commit/cec8d39af471ec2ba4328c97d1ffbd0ba404965b))
* **main:** release 0.24.3 ([8e71b1f](https://github.com/chio-labs/sqltide/commit/8e71b1f666f421ff5bac526c8f15da8ceb6a7ae1))
* **main:** release 0.24.4 ([5bd8abb](https://github.com/chio-labs/sqltide/commit/5bd8abbc83ffb4c0ab0ed7a6e77fa18c1cb18723))
* **main:** release 0.24.4 ([c162cc0](https://github.com/chio-labs/sqltide/commit/c162cc0cf919d774e4fd98511922c91a55b03067))
* **main:** release 0.24.5 ([755dc8e](https://github.com/chio-labs/sqltide/commit/755dc8e68dbafca6b32b0cd76fc2134cbe532083))
* **main:** release 0.24.5 ([b6b0bef](https://github.com/chio-labs/sqltide/commit/b6b0bef67d14562a35ebadb4b4e3cfde62194d7e))
* **main:** release 0.25.0 ([c64f0ab](https://github.com/chio-labs/sqltide/commit/c64f0ab9a77f44c0928c09ed350db784b952afe6))
* **main:** release 0.25.0 ([e70b6b0](https://github.com/chio-labs/sqltide/commit/e70b6b0d8cc40762d43fa5f7e369f629d3a83712))
* **main:** release 0.25.1 ([b7d2c03](https://github.com/chio-labs/sqltide/commit/b7d2c03f3f055355a75ae77dfee4fc76b4f35565))
* **main:** release 0.25.1 ([df6ec90](https://github.com/chio-labs/sqltide/commit/df6ec906a7ea863908a467b155aad2177f9d2baf))
* **main:** release 0.26.0 ([2c98263](https://github.com/chio-labs/sqltide/commit/2c98263ee240bd8302ddc37d1ef62fdd691bac11))
* **main:** release 0.26.0 ([359d47b](https://github.com/chio-labs/sqltide/commit/359d47b492923eb1be148f12a6b13c50c7a75138))
* **main:** release 0.26.1 ([0f445cc](https://github.com/chio-labs/sqltide/commit/0f445cc60be01907b940574d214922e2f665d80a))
* **main:** release 0.26.1 ([643480b](https://github.com/chio-labs/sqltide/commit/643480b3e59c931db6319105817c3874dfa8cbc2))
* **main:** release 0.26.10 ([#156](https://github.com/chio-labs/sqltide/issues/156)) ([cdfa273](https://github.com/chio-labs/sqltide/commit/cdfa273f60c9ec87fd2c051e22627a19079827f0))
* **main:** release 0.26.11 ([#158](https://github.com/chio-labs/sqltide/issues/158)) ([77f0e6e](https://github.com/chio-labs/sqltide/commit/77f0e6ee735ade5f81a43cb4d78be4e94186e26a))
* **main:** release 0.26.12 ([#161](https://github.com/chio-labs/sqltide/issues/161)) ([d014b75](https://github.com/chio-labs/sqltide/commit/d014b75c52c6adec8685d4508d1894ba59df574a))
* **main:** release 0.26.13 ([#162](https://github.com/chio-labs/sqltide/issues/162)) ([c4afeae](https://github.com/chio-labs/sqltide/commit/c4afeaefc357e1478b2851333945f6bb6d2db28c))
* **main:** release 0.26.14 ([#164](https://github.com/chio-labs/sqltide/issues/164)) ([35002f5](https://github.com/chio-labs/sqltide/commit/35002f5234bc748f3e6c81dccb5246d191fa30a0))
* **main:** release 0.26.15 ([#166](https://github.com/chio-labs/sqltide/issues/166)) ([5a1417c](https://github.com/chio-labs/sqltide/commit/5a1417c1c9fe73e926fcb1f43ef6594be9a95fce))
* **main:** release 0.26.16 ([#168](https://github.com/chio-labs/sqltide/issues/168)) ([ca026cc](https://github.com/chio-labs/sqltide/commit/ca026ccb139dd283f7dcea4ae15b176c537aa78e))
* **main:** release 0.26.2 ([#139](https://github.com/chio-labs/sqltide/issues/139)) ([b19473b](https://github.com/chio-labs/sqltide/commit/b19473b3a6b8b48a36522c181046f774b2ac0b3f))
* **main:** release 0.26.3 ([#141](https://github.com/chio-labs/sqltide/issues/141)) ([0edfe39](https://github.com/chio-labs/sqltide/commit/0edfe390275eb055e0bc3d73e6b0d9a5a9d6b52b))
* **main:** release 0.26.4 ([#143](https://github.com/chio-labs/sqltide/issues/143)) ([0fad8a3](https://github.com/chio-labs/sqltide/commit/0fad8a3e1da0d4c3ca56d04983b469fd059567c8))
* **main:** release 0.26.5 ([#145](https://github.com/chio-labs/sqltide/issues/145)) ([ccec504](https://github.com/chio-labs/sqltide/commit/ccec5046f305f63f3382b641afab972ddd28f922))
* **main:** release 0.26.6 ([#147](https://github.com/chio-labs/sqltide/issues/147)) ([61515f8](https://github.com/chio-labs/sqltide/commit/61515f892ec72405897006aca925e2ef8da95f5e))
* **main:** release 0.26.7 ([#150](https://github.com/chio-labs/sqltide/issues/150)) ([a4207ca](https://github.com/chio-labs/sqltide/commit/a4207caccc4dbba7036e407ddf41a1c276a369e1))
* **main:** release 0.26.8 ([#152](https://github.com/chio-labs/sqltide/issues/152)) ([170eb0b](https://github.com/chio-labs/sqltide/commit/170eb0ba821f8bc8b0d853a08172095842a2736a))
* **main:** release 0.26.9 ([#154](https://github.com/chio-labs/sqltide/issues/154)) ([3ea2791](https://github.com/chio-labs/sqltide/commit/3ea2791b919b0c2f637c1349ad0bf14aa16ae993))
* **main:** release 0.27.0 ([#171](https://github.com/chio-labs/sqltide/issues/171)) ([06050d9](https://github.com/chio-labs/sqltide/commit/06050d950011db567a2265c34d3ebe221d95b250))
* **main:** release 0.27.1 ([#175](https://github.com/chio-labs/sqltide/issues/175)) ([8cfdc81](https://github.com/chio-labs/sqltide/commit/8cfdc81aa0d9974fedadebc3538780383a76ff81))
* **main:** release 0.27.2 ([#177](https://github.com/chio-labs/sqltide/issues/177)) ([e1be622](https://github.com/chio-labs/sqltide/commit/e1be6225a0fba525b9edaba3bd49d4afc4498fbc))
* **main:** release 0.28.0 ([#179](https://github.com/chio-labs/sqltide/issues/179)) ([cfc7997](https://github.com/chio-labs/sqltide/commit/cfc79976a0f2749d13467333c07ae1393fb7e9dc))
* **main:** release 0.28.1 ([#181](https://github.com/chio-labs/sqltide/issues/181)) ([a0385f0](https://github.com/chio-labs/sqltide/commit/a0385f0cf5939194a81fa5618d318d405db927af))
* **main:** release 0.28.2 ([#183](https://github.com/chio-labs/sqltide/issues/183)) ([50a787a](https://github.com/chio-labs/sqltide/commit/50a787a39eb2d52fad364a33001ae70e4d02689c))
* **main:** release 0.28.3 ([#185](https://github.com/chio-labs/sqltide/issues/185)) ([b2e8434](https://github.com/chio-labs/sqltide/commit/b2e8434e0307727c8c489870379a9ed0ea81b2ea))
* **main:** release 0.29.0 ([#187](https://github.com/chio-labs/sqltide/issues/187)) ([0901e9b](https://github.com/chio-labs/sqltide/commit/0901e9bd94768f8caaca991ee218515c9a2ba2e7))
* **main:** release 0.29.1 ([#189](https://github.com/chio-labs/sqltide/issues/189)) ([dcab828](https://github.com/chio-labs/sqltide/commit/dcab8281a144b4652e0a24e3a8baf14449d9dc3d))
* **main:** release 0.30.0 ([#191](https://github.com/chio-labs/sqltide/issues/191)) ([18a0e6d](https://github.com/chio-labs/sqltide/commit/18a0e6d03e4fedfa1c7b93d885f077ff5314efc0))
* **main:** release 0.31.0 ([#193](https://github.com/chio-labs/sqltide/issues/193)) ([7f9fb6d](https://github.com/chio-labs/sqltide/commit/7f9fb6d464cff1b8091f53a450cd4cac3409d420))
* **main:** release 0.32.0 ([#196](https://github.com/chio-labs/sqltide/issues/196)) ([853bc59](https://github.com/chio-labs/sqltide/commit/853bc590f7a7fceb4ea7fb86020a8fd6c2cabc54))
* **main:** release 0.33.0 ([#198](https://github.com/chio-labs/sqltide/issues/198)) ([b714613](https://github.com/chio-labs/sqltide/commit/b71461310563f517702633576626b417026423c3))
* **main:** release 0.33.1 ([#201](https://github.com/chio-labs/sqltide/issues/201)) ([7ac5eca](https://github.com/chio-labs/sqltide/commit/7ac5eca94d921cf20b761e0d80c7c6f094568cd9))
* **main:** release 0.34.0 ([#203](https://github.com/chio-labs/sqltide/issues/203)) ([1f083b4](https://github.com/chio-labs/sqltide/commit/1f083b4ed55444b4d9dc2d428b236a7cf3fc0f15))
* **main:** release 0.34.1 ([#205](https://github.com/chio-labs/sqltide/issues/205)) ([2827bed](https://github.com/chio-labs/sqltide/commit/2827bedf4de5c1ea8c0866d04ba19f1865066ebe))
* **main:** release 0.34.2 ([#207](https://github.com/chio-labs/sqltide/issues/207)) ([9eab1a7](https://github.com/chio-labs/sqltide/commit/9eab1a7df2fe5e558dd926b2043392d6bdfd8662))
* **main:** release 0.34.3 ([#209](https://github.com/chio-labs/sqltide/issues/209)) ([b82aaef](https://github.com/chio-labs/sqltide/commit/b82aaefdcb46a4c647a46573850fca2193492a3d))
* **main:** release 0.34.4 ([#212](https://github.com/chio-labs/sqltide/issues/212)) ([4a5ffe7](https://github.com/chio-labs/sqltide/commit/4a5ffe7c8adce4b1c3667a8991060c0618287f09))
* **main:** release 0.35.0 ([#213](https://github.com/chio-labs/sqltide/issues/213)) ([3a126cb](https://github.com/chio-labs/sqltide/commit/3a126cb3c0bf206ee936870926a6ab0f7673897e))
* **main:** release 0.35.1 ([#215](https://github.com/chio-labs/sqltide/issues/215)) ([6565416](https://github.com/chio-labs/sqltide/commit/6565416ce0881e18165628e7f58e2b7868659a68))
* **main:** release 0.36.0 ([#218](https://github.com/chio-labs/sqltide/issues/218)) ([ca59797](https://github.com/chio-labs/sqltide/commit/ca5979733619eafa10024d6c2fa219d109877a37))
* **main:** release 0.37.0 ([#220](https://github.com/chio-labs/sqltide/issues/220)) ([a990643](https://github.com/chio-labs/sqltide/commit/a9906436306436fe892022c25b337486a7412fe9))
* **main:** release 0.38.0 ([#224](https://github.com/chio-labs/sqltide/issues/224)) ([6c94fdb](https://github.com/chio-labs/sqltide/commit/6c94fdb5a1e418aebe5cf7b243153e1bd668c040))
* **main:** release 0.38.1 ([#226](https://github.com/chio-labs/sqltide/issues/226)) ([8b49d1b](https://github.com/chio-labs/sqltide/commit/8b49d1b9eaaa9cc5ae96f52b6d14e349c27a5431))
* **main:** release 0.38.2 ([#228](https://github.com/chio-labs/sqltide/issues/228)) ([9ec366b](https://github.com/chio-labs/sqltide/commit/9ec366bf797e0804d41bd1b4719e1fcb7e4d6431))
* **main:** release 0.38.3 ([#230](https://github.com/chio-labs/sqltide/issues/230)) ([0a25508](https://github.com/chio-labs/sqltide/commit/0a2550845f20334860662f140b3f521ea0a423bc))
* **main:** release 0.38.4 ([#233](https://github.com/chio-labs/sqltide/issues/233)) ([f26b9ea](https://github.com/chio-labs/sqltide/commit/f26b9ea2920d17a5da8d18f8c9a0f6f026d1cfe2))
* **main:** release 0.39.0 ([#235](https://github.com/chio-labs/sqltide/issues/235)) ([276b0c4](https://github.com/chio-labs/sqltide/commit/276b0c402be2d85aec9460999414c2438f327f43))
* **main:** release 0.40.0 ([#239](https://github.com/chio-labs/sqltide/issues/239)) ([b0f092c](https://github.com/chio-labs/sqltide/commit/b0f092ca1e43b45a1c612bb42c206739640138da))
* **main:** release 0.40.1 ([#240](https://github.com/chio-labs/sqltide/issues/240)) ([67238a5](https://github.com/chio-labs/sqltide/commit/67238a56fd14f37d3be2ea6d5bbc7d2c440d5d72))
* **main:** release 0.40.2 ([#242](https://github.com/chio-labs/sqltide/issues/242)) ([b3a470a](https://github.com/chio-labs/sqltide/commit/b3a470a3f10d41c6691e143d7617364dd11841cf))
* **main:** release 0.5.0 ([8ced0fe](https://github.com/chio-labs/sqltide/commit/8ced0fe56abb959c16d5f54baf50ba624d6b353e))
* **main:** release 0.5.0 ([4bc96ae](https://github.com/chio-labs/sqltide/commit/4bc96ae26231458c9ff63445707bfaae3ea24114))
* **main:** release 0.6.0 ([332c7cc](https://github.com/chio-labs/sqltide/commit/332c7cc678fd94bdfecd27fa42b09b0529e45d92))
* **main:** release 0.6.0 ([2d9665a](https://github.com/chio-labs/sqltide/commit/2d9665aeb37a1229d15faceaeefc00c642559846))
* **main:** release 0.7.0 ([e1eee2d](https://github.com/chio-labs/sqltide/commit/e1eee2d8be0b5632d1f71bbb61f2dfe1852ad76d))
* **main:** release 0.7.0 ([cefae06](https://github.com/chio-labs/sqltide/commit/cefae06c9c30191a52db216166d7b1a69b393232))
* **main:** release 0.8.0 ([97b72ef](https://github.com/chio-labs/sqltide/commit/97b72ef027ee4f48222ee01e645f6975df9e3822))
* **main:** release 0.8.0 ([82fdcd3](https://github.com/chio-labs/sqltide/commit/82fdcd3603486049d436896d33f9d383be02fab3))
* **main:** release 0.9.0 ([3df7612](https://github.com/chio-labs/sqltide/commit/3df7612585c9303591e65086c36ad191d8a693c4))
* **main:** release 0.9.0 ([f864e2f](https://github.com/chio-labs/sqltide/commit/f864e2ffb297cffd41868127f9b5274bd19e878c))
* **main:** release 0.9.1 ([7f7085e](https://github.com/chio-labs/sqltide/commit/7f7085e36548345044f41d3571332a5e670062be))
* **main:** release 0.9.1 ([20a58a1](https://github.com/chio-labs/sqltide/commit/20a58a1828dd3e5da3a5b85ef44360f38277cd43))
* **main:** release 0.9.2 ([402f465](https://github.com/chio-labs/sqltide/commit/402f465397f4d241c388a04fb7f68c8c2eb18273))
* **main:** release 0.9.2 ([591985e](https://github.com/chio-labs/sqltide/commit/591985e079649c476ada9884c3051c030305e83b))
* **main:** release 0.9.3 ([11a17e9](https://github.com/chio-labs/sqltide/commit/11a17e9dd8b7af3ac35b8ef99e45418c8b310443))
* **main:** release 0.9.3 ([694a0a9](https://github.com/chio-labs/sqltide/commit/694a0a9369fd1dbd9455571c8fb40ae31149f8b4))
* refresh uv lock ([c7ab980](https://github.com/chio-labs/sqltide/commit/c7ab980438fc14ef35b0ba8ce45e715e9fddf509))
* refresh uv lock ([7acf28d](https://github.com/chio-labs/sqltide/commit/7acf28daa15427c4cdab1ee331c245413f7dac94))
* refresh uv lock ([92eddf9](https://github.com/chio-labs/sqltide/commit/92eddf9a189f102602063bdb78adf9878ff33d77))
* refresh uv lock ([928bbe1](https://github.com/chio-labs/sqltide/commit/928bbe17a2140d61aaaec3afd57897901056e7b1))
* refresh uv lock ([d9fb59e](https://github.com/chio-labs/sqltide/commit/d9fb59e1688d1759fa11b8e6bfdd813d9e92429b))
* refresh uv lock ([051e5c8](https://github.com/chio-labs/sqltide/commit/051e5c8b883f9bb4b6de04f574e19f6fb1fb9e45))
* refresh uv lock ([e5a5817](https://github.com/chio-labs/sqltide/commit/e5a5817d51a8b788fa706888ce55a9e85c43a5dc))
* refresh uv lock ([60cde04](https://github.com/chio-labs/sqltide/commit/60cde044c0923820f0028582e50e54b6884350e1))
* refresh uv lock ([de6a6d8](https://github.com/chio-labs/sqltide/commit/de6a6d830fbbc8e593acad17cf4c2fc4f951f886))
* refresh uv lock ([8c887f8](https://github.com/chio-labs/sqltide/commit/8c887f8b9fecb9c087048871fde367ce6e14eae1))
* refresh uv lock ([546b610](https://github.com/chio-labs/sqltide/commit/546b61038b5512cccddf778507a161ec6f115b23))
* refresh uv lock ([894752d](https://github.com/chio-labs/sqltide/commit/894752d9865aef2968e628db4ced70b442b49207))
* refresh uv lock ([db846c5](https://github.com/chio-labs/sqltide/commit/db846c5143b6614d823504d6c457bb92a8e45c04))
* refresh uv lock ([7d98530](https://github.com/chio-labs/sqltide/commit/7d98530f3483e9f89f0bb32444419e63f99a95fb))
* refresh uv lock ([062ed47](https://github.com/chio-labs/sqltide/commit/062ed4734f16b0500cd9771dbd24ef1f799e3539))
* refresh uv lock ([8a6513f](https://github.com/chio-labs/sqltide/commit/8a6513f393d2165ece0d0f89539c981753aa8ea7))
* refresh uv lock ([d00e5e4](https://github.com/chio-labs/sqltide/commit/d00e5e4db0b5e831a1b575095f69af671d35ad4c))
* refresh uv lock ([d8fd8eb](https://github.com/chio-labs/sqltide/commit/d8fd8eb71446daed2d992f9f1d3ae9deb78795ef))
* refresh uv lock ([e44ba53](https://github.com/chio-labs/sqltide/commit/e44ba5356d4a0055cee55cc0b77092582c39779f))
* refresh uv lock ([f7cbd18](https://github.com/chio-labs/sqltide/commit/f7cbd18fbe7fc83bd4bca0cf3099c817e99dd232))
* refresh uv lock ([4b49863](https://github.com/chio-labs/sqltide/commit/4b498639c931b7dae2b68700eab3f0427abf322a))
* refresh uv lock ([4f809ee](https://github.com/chio-labs/sqltide/commit/4f809ee6dcf988bc54323ed9952e127915027927))
* refresh uv lock ([2b30d79](https://github.com/chio-labs/sqltide/commit/2b30d79af6c41dcfeda8d02b961374399f1fb5ec))
* refresh uv lock ([a9a0878](https://github.com/chio-labs/sqltide/commit/a9a0878d42a2f0a2b228c44430e569d55bb4f554))
* refresh uv lock ([c61e3e3](https://github.com/chio-labs/sqltide/commit/c61e3e3944a65178bb68ec8fba2fe21a9fac0d8e))
* refresh uv lock ([cb24270](https://github.com/chio-labs/sqltide/commit/cb242704506284cc2553c8d190ed65d3ade520a4))
* refresh uv lock ([3203e84](https://github.com/chio-labs/sqltide/commit/3203e847e7ab8bb01ca3564f5092b3328834529d))
* refresh uv lock ([d88e064](https://github.com/chio-labs/sqltide/commit/d88e0646aed2161f41bc958e35254626c7c2e6fb))
* refresh uv lock ([bacb808](https://github.com/chio-labs/sqltide/commit/bacb8080e901fd4b9ba35208c6eb8ab28125b407))
* refresh uv lock ([5a0e8f8](https://github.com/chio-labs/sqltide/commit/5a0e8f8e5acfeff06b0791e5a4e025267d2fe383))
* refresh uv lock ([400de02](https://github.com/chio-labs/sqltide/commit/400de028dae0ac6a75f60c5c59d8f7f5f7262c73))
* refresh uv lock ([f250e1b](https://github.com/chio-labs/sqltide/commit/f250e1bfed92a5ff9c255decafed8ad74361c72b))
* refresh uv lock ([6562a26](https://github.com/chio-labs/sqltide/commit/6562a26a1ad4f6125100879d3238f9e159bbc10c))
* refresh uv lock ([36ac763](https://github.com/chio-labs/sqltide/commit/36ac763b5ac69d09e549cc6f686bb348534feeb5))
* refresh uv lock ([5419749](https://github.com/chio-labs/sqltide/commit/5419749d86dd5bc46e0f898437cf194edb8d2810))
* refresh uv lock ([7109ad9](https://github.com/chio-labs/sqltide/commit/7109ad9e9c7f71fc7dce6712f33a860419c1fa4f))
* refresh uv lock ([5cf197c](https://github.com/chio-labs/sqltide/commit/5cf197c4ccda2ac23ba540c0b8fc92cd80792915))
* refresh uv lock ([df5afa5](https://github.com/chio-labs/sqltide/commit/df5afa5409a4d6a8991df250769163bd121c6882))
* refresh uv lock ([90de13c](https://github.com/chio-labs/sqltide/commit/90de13c56cb60a1c8f266a995e1c24961bd4538a))
* refresh uv lock ([153184b](https://github.com/chio-labs/sqltide/commit/153184b981b7dc7d779fa0f5878d7ab9d46a8092))
* refresh uv lock ([133b340](https://github.com/chio-labs/sqltide/commit/133b3402370523891ce6118333abece6b8c5e228))
* refresh uv lock ([cd2605f](https://github.com/chio-labs/sqltide/commit/cd2605fbe47cf3662cedf52eb62c0327e2c867eb))
* refresh uv lock ([d1ac1c3](https://github.com/chio-labs/sqltide/commit/d1ac1c3015b25ddcf6a676f9090701d87ec5fb3b))
* refresh uv lock ([2a8fa7d](https://github.com/chio-labs/sqltide/commit/2a8fa7d3559d23638e23eaf76dbfdf7d2a77674d))
* stop tracking local agent skills ([f5c488c](https://github.com/chio-labs/sqltide/commit/f5c488c5ed57404f5ca2701e5de294cbb6d3b921))
* sync lockfile version ([59d8ba3](https://github.com/chio-labs/sqltide/commit/59d8ba3710d45c54bf8d254c4a94307c6f8b1285))
* **ui:** refresh npm lockfile ([4217531](https://github.com/chio-labs/sqltide/commit/4217531224feaab93117888391140f8b66bde5fa))
* upgrade fensu and align release version ([2138b03](https://github.com/chio-labs/sqltide/commit/2138b03f6ff7dc34a936113da6ded5c804ffef0f))

## [0.40.2](https://github.com/chio-labs/sqltide/compare/v0.40.1...v0.40.2) (2026-09-08)


### Bug Fixes

* update public defaults and examples ([0160843](https://github.com/chio-labs/sqltide/commit/01608432ac0614ff7dc3f153f22d1d57ae6b6046))

## [0.40.1](https://github.com/chio-labs/sqltide/compare/v0.40.0...v0.40.1) (2026-09-07)


### Bug Fixes

* filter offset replays at physical source ([#238](https://github.com/chio-labs/sqltide/issues/238)) ([1f30beb](https://github.com/chio-labs/sqltide/commit/1f30beb5f64af81dcdb31a186b66922c4ee64f84))

## [0.40.0](https://github.com/chio-labs/sqltide/compare/v0.39.0...v0.40.0) (2026-09-07)


### Features

* stop ClickHouse queries when cancelling UI builds ([#237](https://github.com/chio-labs/sqltide/issues/237)) ([aa66b76](https://github.com/chio-labs/sqltide/commit/aa66b76a4a160cd980f1e87fd9b8146510bca23e))

## [0.39.0](https://github.com/chio-labs/sqltide/compare/v0.38.4...v0.39.0) (2026-09-06)


### Features

* allow deleting inactive pipelines ([#236](https://github.com/chio-labs/sqltide/issues/236)) ([f208a97](https://github.com/chio-labs/sqltide/commit/f208a971aeaf2f41ac78230a6c7b30c4b89ff335))


### Documentation

* require watching auto-merge completion ([#234](https://github.com/chio-labs/sqltide/issues/234)) ([1f5f50c](https://github.com/chio-labs/sqltide/commit/1f5f50c65a8a7f11d4ae2add544b6b50444ace03))

## [0.38.4](https://github.com/chio-labs/sqltide/compare/v0.38.3...v0.38.4) (2026-08-31)


### Bug Fixes

* make physical lineage responsive at scale ([#232](https://github.com/chio-labs/sqltide/issues/232)) ([0d978e7](https://github.com/chio-labs/sqltide/commit/0d978e7c0322f128702b75e9a2c107af1e05ab46))

## [0.38.3](https://github.com/chio-labs/sqltide/compare/v0.38.2...v0.38.3) (2026-08-29)


### Bug Fixes

* normalize warehouse table typography ([#229](https://github.com/chio-labs/sqltide/issues/229)) ([af2be5e](https://github.com/chio-labs/sqltide/commit/af2be5e5157068da118aac584d3a73024855d500))

## [0.38.2](https://github.com/chio-labs/sqltide/compare/v0.38.1...v0.38.2) (2026-08-28)


### Bug Fixes

* batch manual audit execution ([#227](https://github.com/chio-labs/sqltide/issues/227)) ([b9ea3d2](https://github.com/chio-labs/sqltide/commit/b9ea3d2d019640bc35697af5de342645d6fcc97a))

## [0.38.1](https://github.com/chio-labs/sqltide/compare/v0.38.0...v0.38.1) (2026-08-28)


### Bug Fixes

* distinguish unbuilt Kafka sources from consumer errors ([#225](https://github.com/chio-labs/sqltide/issues/225)) ([70da2f9](https://github.com/chio-labs/sqltide/commit/70da2f95d2c93608df273bfbe67570ad3f0b294c))

## [0.38.0](https://github.com/chio-labs/sqltide/compare/v0.37.0...v0.38.0) (2026-08-28)


### Features

* publish append-only SQLTide manifests ([#223](https://github.com/chio-labs/sqltide/issues/223)) ([549a556](https://github.com/chio-labs/sqltide/commit/549a5566221f6c4b8c531ec72f63e5e6c0240533))

## [0.37.0](https://github.com/chio-labs/sqltide/compare/v0.36.0...v0.37.0) (2026-08-27)


### Features

* estimate replay progress from offsets ([#221](https://github.com/chio-labs/sqltide/issues/221)) ([ff032ff](https://github.com/chio-labs/sqltide/commit/ff032ff8b2c40b1eabead50348248fe4f0d8b6c6))
* separate destructive pipeline actions ([#219](https://github.com/chio-labs/sqltide/issues/219)) ([653a976](https://github.com/chio-labs/sqltide/commit/653a9764f0ead6dfb5faea2a8170ae022bda7b34))

## [0.36.0](https://github.com/chio-labs/sqltide/compare/v0.35.1...v0.36.0) (2026-08-27)


### Features

* clarify observability and catalog drift ([#217](https://github.com/chio-labs/sqltide/issues/217)) ([61bb13e](https://github.com/chio-labs/sqltide/commit/61bb13e2452fe17ec64715f51aa6dc6d2309ba65))

## [0.35.1](https://github.com/chio-labs/sqltide/compare/v0.35.0...v0.35.1) (2026-08-27)


### Bug Fixes

* reuse Kafka metadata clients ([#214](https://github.com/chio-labs/sqltide/issues/214)) ([c714ea3](https://github.com/chio-labs/sqltide/commit/c714ea3565f8a4867b3391031bd7c7f95628848e))

## [0.35.0](https://github.com/chio-labs/sqltide/compare/v0.34.4...v0.35.0) (2026-08-27)


### Features

* build selected pipelines from inventory ([#211](https://github.com/chio-labs/sqltide/issues/211)) ([3710347](https://github.com/chio-labs/sqltide/commit/3710347fa75affca2eb6ee971a290018fb11c1dc))

## [0.34.4](https://github.com/chio-labs/sqltide/compare/v0.34.3...v0.34.4) (2026-08-27)


### Bug Fixes

* support direct metadata reconciliation ([#210](https://github.com/chio-labs/sqltide/issues/210)) ([ee626f3](https://github.com/chio-labs/sqltide/commit/ee626f3a7743fa3544fdcc901e1114c4b90187cc))

## [0.34.3](https://github.com/chio-labs/sqltide/compare/v0.34.2...v0.34.3) (2026-08-26)


### Performance Improvements

* reduce snapshot refresh warehouse work ([#208](https://github.com/chio-labs/sqltide/issues/208)) ([86121d8](https://github.com/chio-labs/sqltide/commit/86121d8a4fd0052d72d516c09d472d77d82b2916))

## [0.34.2](https://github.com/chio-labs/sqltide/compare/v0.34.1...v0.34.2) (2026-08-26)


### Bug Fixes

* keep snapshot refresh responsive ([#206](https://github.com/chio-labs/sqltide/issues/206)) ([0f8be78](https://github.com/chio-labs/sqltide/commit/0f8be7823f742b64504d4ab573983fe7df3df2c5))

## [0.34.1](https://github.com/chio-labs/sqltide/compare/v0.34.0...v0.34.1) (2026-08-26)


### Bug Fixes

* preserve refreshable view scheduling ([#204](https://github.com/chio-labs/sqltide/issues/204)) ([fb52c7f](https://github.com/chio-labs/sqltide/commit/fb52c7ff01ac954bfee5150fbbc7c7580ef65551))

## [0.34.0](https://github.com/chio-labs/sqltide/compare/v0.33.1...v0.34.0) (2026-08-26)


### Features

* allow directional cross-pipeline references ([#202](https://github.com/chio-labs/sqltide/issues/202)) ([93c55d4](https://github.com/chio-labs/sqltide/commit/93c55d477a3e4901ba186b10e5e7104926e4f827))

## [0.33.1](https://github.com/chio-labs/sqltide/compare/v0.33.0...v0.33.1) (2026-08-26)


### Bug Fixes

* prevent managed source replay overlap ([#200](https://github.com/chio-labs/sqltide/issues/200)) ([7643159](https://github.com/chio-labs/sqltide/commit/7643159ef3a2565fdd7c5d2072901cc75ca06339))

## [0.33.0](https://github.com/chio-labs/sqltide/compare/v0.32.0...v0.33.0) (2026-08-25)


### Features

* add changed model selection and complete UI states ([#197](https://github.com/chio-labs/sqltide/issues/197)) ([4190da6](https://github.com/chio-labs/sqltide/commit/4190da6d76296f3260babfc2d4669052727886f6))

## [0.32.0](https://github.com/chio-labs/sqltide/compare/v0.31.0...v0.32.0) (2026-08-25)


### Features

* harden retention and scope model references ([#194](https://github.com/chio-labs/sqltide/issues/194)) ([aa801fb](https://github.com/chio-labs/sqltide/commit/aa801fb080a45529fc337f5b1a3c97e188f28c80))

## [0.31.0](https://github.com/chio-labs/sqltide/compare/v0.30.0...v0.31.0) (2026-08-25)


### Features

* **retention:** add schema-aware defaults ([#192](https://github.com/chio-labs/sqltide/issues/192)) ([c71824d](https://github.com/chio-labs/sqltide/commit/c71824db926a2152b39a422c851b96838eaf22ea))

## [0.30.0](https://github.com/chio-labs/sqltide/compare/v0.29.1...v0.30.0) (2026-08-25)


### Features

* **ui:** improve run history presentation ([#190](https://github.com/chio-labs/sqltide/issues/190)) ([0467d71](https://github.com/chio-labs/sqltide/commit/0467d71850f28b0549c73a8c1a79c4c859b3aa36))

## [0.29.1](https://github.com/chio-labs/sqltide/compare/v0.29.0...v0.29.1) (2026-08-25)


### Bug Fixes

* **destruction:** reload reviewed safety policy ([#188](https://github.com/chio-labs/sqltide/issues/188)) ([5684228](https://github.com/chio-labs/sqltide/commit/568422867aad1e6fa42677b57503170d7eb69104))

## [0.29.0](https://github.com/chio-labs/sqltide/compare/v0.28.3...v0.29.0) (2026-08-25)


### Features

* **destruction:** add scoped recovery controls ([#186](https://github.com/chio-labs/sqltide/issues/186)) ([973f887](https://github.com/chio-labs/sqltide/commit/973f8871afe96940c52c022c5cd8bbc41bbd4041))

## [0.28.3](https://github.com/chio-labs/sqltide/compare/v0.28.2...v0.28.3) (2026-08-25)


### Bug Fixes

* **destruction:** launch safely into live runs ([#184](https://github.com/chio-labs/sqltide/issues/184)) ([9d75f79](https://github.com/chio-labs/sqltide/commit/9d75f792f6aaa359cc1a9d9c2a03a22b3d65e24b))

## [0.28.2](https://github.com/chio-labs/sqltide/compare/v0.28.1...v0.28.2) (2026-08-25)


### Bug Fixes

* **destruction:** move frozen plans to refreshable page ([#182](https://github.com/chio-labs/sqltide/issues/182)) ([4f75b5c](https://github.com/chio-labs/sqltide/commit/4f75b5c7e2da7ac27e5f518eca70604932cc28a5))

## [0.28.1](https://github.com/chio-labs/sqltide/compare/v0.28.0...v0.28.1) (2026-08-25)


### Bug Fixes

* **lifecycle:** restore association-driven destruction ([#180](https://github.com/chio-labs/sqltide/issues/180)) ([15feed1](https://github.com/chio-labs/sqltide/commit/15feed1283a35d9671efc1edb58f7b2ef4f9a2e8))

## [0.28.0](https://github.com/chio-labs/sqltide/compare/v0.27.2...v0.28.0) (2026-08-24)


### Features

* add recorded pipeline destruction and target reset ([#178](https://github.com/chio-labs/sqltide/issues/178)) ([46c465e](https://github.com/chio-labs/sqltide/commit/46c465e5ee8712ef39cd7dc3f2ab5447f60170e2))

## [0.27.2](https://github.com/chio-labs/sqltide/compare/v0.27.1...v0.27.2) (2026-08-24)


### Refactoring

* **release:** centralize auto-merge ([#176](https://github.com/chio-labs/sqltide/issues/176)) ([7e5e79a](https://github.com/chio-labs/sqltide/commit/7e5e79a01a15537e39f65740c5eafc477d967194))

## [0.27.1](https://github.com/chio-labs/sqltide/compare/v0.27.0...v0.27.1) (2026-08-24)


### Bug Fixes

* drain sensor event backlogs ([#172](https://github.com/chio-labs/sqltide/issues/172)) ([960e404](https://github.com/chio-labs/sqltide/commit/960e4043ebdec9b19b933727432dc2adc9842bab))

## [0.27.0](https://github.com/chio-labs/sqltide/compare/v0.26.16...v0.27.0) (2026-08-23)


### Features

* add warehouse health diagnostics ([#170](https://github.com/chio-labs/sqltide/issues/170)) ([3e1e009](https://github.com/chio-labs/sqltide/commit/3e1e0095c4c1510e3204d1952930dd964537f54f))
* rebuild deterministic commerce demo ([#169](https://github.com/chio-labs/sqltide/issues/169)) ([de48907](https://github.com/chio-labs/sqltide/commit/de48907fc828149743efab0f8083e70df73c9bd6))

## [0.26.16](https://github.com/chio-labs/sqltide/compare/v0.26.15...v0.26.16) (2026-08-23)


### Bug Fixes

* make the orders demo quickstart reliable ([#167](https://github.com/chio-labs/sqltide/issues/167)) ([aa1bd51](https://github.com/chio-labs/sqltide/commit/aa1bd51d3ef2e32501a3af88f775f5282e49c6f0))

## [0.26.15](https://github.com/chio-labs/sqltide/compare/v0.26.14...v0.26.15) (2026-08-23)


### Documentation

* tighten readme logo framing ([#165](https://github.com/chio-labs/sqltide/issues/165)) ([7a06917](https://github.com/chio-labs/sqltide/commit/7a06917db6d1203c0818570b61b55e005ab534ec))

## [0.26.14](https://github.com/chio-labs/sqltide/compare/v0.26.13...v0.26.14) (2026-08-22)


### Bug Fixes

* make sensor alerts immediately actionable ([#163](https://github.com/chio-labs/sqltide/issues/163)) ([e7a9b94](https://github.com/chio-labs/sqltide/commit/e7a9b94d7f185946cc5468f7fd8a2f2c87fb0468))

## [0.26.13](https://github.com/chio-labs/sqltide/compare/v0.26.12...v0.26.13) (2026-08-22)


### Bug Fixes

* **ui:** clarify dead-letter retry progress ([#160](https://github.com/chio-labs/sqltide/issues/160)) ([796d270](https://github.com/chio-labs/sqltide/commit/796d270fd7796fcec2e9893ae328cfd2cd9ad32c))

## [0.26.12](https://github.com/chio-labs/sqltide/compare/v0.26.11...v0.26.12) (2026-08-22)


### Documentation

* show full logo in readme ([#159](https://github.com/chio-labs/sqltide/issues/159)) ([dadc955](https://github.com/chio-labs/sqltide/commit/dadc955c1ee993404c1f08f6ece73e97b38c008a))

## [0.26.11](https://github.com/chio-labs/sqltide/compare/v0.26.10...v0.26.11) (2026-08-22)


### Bug Fixes

* expose named target to sensor events ([#157](https://github.com/chio-labs/sqltide/issues/157)) ([608614e](https://github.com/chio-labs/sqltide/commit/608614e8a8a0dde9dbeb929300acb7e5e04d6a32))

## [0.26.10](https://github.com/chio-labs/sqltide/compare/v0.26.9...v0.26.10) (2026-08-22)


### Bug Fixes

* make audit warning alerts durable ([#155](https://github.com/chio-labs/sqltide/issues/155)) ([5908e6b](https://github.com/chio-labs/sqltide/commit/5908e6b1e1d0ab89d1328ea5b057ff56af81ebf4))

## [0.26.9](https://github.com/chio-labs/sqltide/compare/v0.26.8...v0.26.9) (2026-08-22)


### Bug Fixes

* **ui:** keep sensor loading responsive ([#153](https://github.com/chio-labs/sqltide/issues/153)) ([a0d9799](https://github.com/chio-labs/sqltide/commit/a0d9799f4e3db1c99109ef742ba549f011b59328))

## [0.26.8](https://github.com/chio-labs/sqltide/compare/v0.26.7...v0.26.8) (2026-08-22)


### Bug Fixes

* **ui:** keep history reads responsive ([#151](https://github.com/chio-labs/sqltide/issues/151)) ([5179374](https://github.com/chio-labs/sqltide/commit/517937411cae093281646a481307d2f7e59506c0))

## [0.26.7](https://github.com/chio-labs/sqltide/compare/v0.26.6...v0.26.7) (2026-08-22)


### Bug Fixes

* **ui:** improve run and deployment loading ([#149](https://github.com/chio-labs/sqltide/issues/149)) ([9f163b8](https://github.com/chio-labs/sqltide/commit/9f163b84010989d7154addaabd4f4d7974eeacdb))

## [0.26.6](https://github.com/chio-labs/sqltide/compare/v0.26.5...v0.26.6) (2026-08-22)


### Bug Fixes

* **ui:** tolerate definitions cache limits ([#146](https://github.com/chio-labs/sqltide/issues/146)) ([8b57aa1](https://github.com/chio-labs/sqltide/commit/8b57aa10cd4d10527b4e31fc0c373c2c85d66e8b))

## [0.26.5](https://github.com/chio-labs/sqltide/compare/v0.26.4...v0.26.5) (2026-08-22)


### Bug Fixes

* **ui:** refine quality and run history ([#144](https://github.com/chio-labs/sqltide/issues/144)) ([7bf59e6](https://github.com/chio-labs/sqltide/commit/7bf59e6d59071e41a92b77235f41e0a8240a261d))

## [0.26.4](https://github.com/chio-labs/sqltide/compare/v0.26.3...v0.26.4) (2026-08-22)


### Bug Fixes

* **release:** use checked out release head ([#142](https://github.com/chio-labs/sqltide/issues/142)) ([11131e0](https://github.com/chio-labs/sqltide/commit/11131e0ce2c5550ddbe66bcdfee43a2d3ea47d0c))

## [0.26.3](https://github.com/chio-labs/sqltide/compare/v0.26.2...v0.26.3) (2026-08-22)


### Bug Fixes

* **release:** satisfy protected release merges ([#140](https://github.com/chio-labs/sqltide/issues/140)) ([f302d4c](https://github.com/chio-labs/sqltide/commit/f302d4cf608bcb5693baaf735fe8eb4ba210c3d1))

## [0.26.2](https://github.com/chio-labs/sqltide/compare/v0.26.1...v0.26.2) (2026-08-22)


### Build System

* **release:** standardize automated releases ([#138](https://github.com/chio-labs/sqltide/issues/138)) ([c8d1128](https://github.com/chio-labs/sqltide/commit/c8d11280a656fa7c70b7c6df1c9815467087c91e))

## [0.26.1](https://github.com/chio-labs/sqltide/compare/v0.26.0...v0.26.1) (2026-08-21)


### Bug Fixes

* **ui:** suppress transient telemetry warnings ([#136](https://github.com/chio-labs/sqltide/issues/136)) ([276a5e3](https://github.com/chio-labs/sqltide/commit/276a5e3dfd30eef9c1559fa6bb30817453babee6))

## [0.26.0](https://github.com/chio-labs/sqltide/compare/v0.25.1...v0.26.0) (2026-08-21)


### Features

* improve UI responsiveness and audit operations ([abadd65](https://github.com/chio-labs/sqltide/commit/abadd65a056581a0adb7e9f8904fcf8e9d0221d4))
* show live run and audit cycle progress ([0aa14b5](https://github.com/chio-labs/sqltide/commit/0aa14b5592ab4f00592204872390a3716ab976ff))


### Bug Fixes

* **audits:** defer unmaterialized relations ([5ab6371](https://github.com/chio-labs/sqltide/commit/5ab6371cdbecd89b81d4d79544c19e588471705e))
* **audits:** reconcile stale builds across releases ([c973de2](https://github.com/chio-labs/sqltide/commit/c973de2b74780eed8119028b4a0f0a6b611c8e36))
* **ui:** defer project shell until bootstrap completes ([001286c](https://github.com/chio-labs/sqltide/commit/001286cf04f269cb088ba616dc393990e64ab378))
* **ui:** prevent runs startup refresh loop ([1c1569b](https://github.com/chio-labs/sqltide/commit/1c1569b4368037cc9814606fbfd1a5b5e93b794c))
* **ui:** require cached definitions for conditional reads ([2d2f38f](https://github.com/chio-labs/sqltide/commit/2d2f38fb42126b7a985a888d4491906b9f2c9826))


### Performance Improvements

* **auth:** cache resolved request identities ([995cb07](https://github.com/chio-labs/sqltide/commit/995cb07341c6e6a31850c0df398d9a4afa9fd5c3))
* **plan:** isolate reads and defer replay counts ([ffaa5c5](https://github.com/chio-labs/sqltide/commit/ffaa5c5b9f10af4cdadacff9feec53797f89c9fd))
* **ui:** deduplicate live refresh requests ([3f5467d](https://github.com/chio-labs/sqltide/commit/3f5467d964dd65b2877bcb5c09d885becef7a938))
* **ui:** split auth and cache definitions ([e4a35cb](https://github.com/chio-labs/sqltide/commit/e4a35cba11cc3deb4f0302f906830b692527ec9f))
* **ui:** standardize cached page navigation ([428fbed](https://github.com/chio-labs/sqltide/commit/428fbed691dcf24df8c0314384ac867689abba90))

## [0.25.1](https://github.com/chio-labs/sqltide/compare/v0.25.0...v0.25.1) (2026-08-21)


### Bug Fixes

* **compile:** reject prewhere in table models ([6b52b2a](https://github.com/chio-labs/sqltide/commit/6b52b2aac8658618d0342fca95990f2b2d499436))
* **replay:** filter non-lineage roots at source ([eb3cb12](https://github.com/chio-labs/sqltide/commit/eb3cb12af4d368d79b9a612ad5bb9da1fda60bb7))

## [0.25.0](https://github.com/chio-labs/sqltide/compare/v0.24.5...v0.25.0) (2026-08-21)


### Features

* **replay:** add phase-scoped execution settings ([83d746e](https://github.com/chio-labs/sqltide/commit/83d746e3e14da4d8a365bfc6ef0ac9e4c93d427d))

## [0.24.5](https://github.com/chio-labs/sqltide/compare/v0.24.4...v0.24.5) (2026-08-20)


### Performance Improvements

* **ui:** initialize the project in one request ([70d6124](https://github.com/chio-labs/sqltide/commit/70d6124462d0f160c9d9aa651c15207156871cd8))

## [0.24.4](https://github.com/chio-labs/sqltide/compare/v0.24.3...v0.24.4) (2026-08-20)


### Performance Improvements

* **ui:** render before secondary warehouse data loads ([4bc9ee6](https://github.com/chio-labs/sqltide/commit/4bc9ee65f065e4bb23b5c0f2169268a59c4a6d9c))

## [0.24.3](https://github.com/chio-labs/sqltide/compare/v0.24.2...v0.24.3) (2026-08-20)


### Performance Improvements

* **dev-server:** build the warehouse overlay off the shared query lock ([63f504f](https://github.com/chio-labs/sqltide/commit/63f504fa4541d89b29e72f26510282dad238106b))

## [0.24.2](https://github.com/chio-labs/sqltide/compare/v0.24.1...v0.24.2) (2026-08-20)


### Bug Fixes

* **dev-server:** keep the state overlay across warehouse refreshes ([908bc3b](https://github.com/chio-labs/sqltide/commit/908bc3b026316ad96a47bf78dae6ac6b466c90e8))
* **dev-server:** only force a snapshot rebuild on explicit refresh ([3efc5a5](https://github.com/chio-labs/sqltide/commit/3efc5a512f9ac63760f72b4417e0d84c3b09de8c))

## [0.24.1](https://github.com/chio-labs/sqltide/compare/v0.24.0...v0.24.1) (2026-08-20)


### Bug Fixes

* **dev-server:** serialize nested connection settings ([5834f79](https://github.com/chio-labs/sqltide/commit/5834f793f47aa7d6138cdea114028480709884d3))

## [0.24.0](https://github.com/chio-labs/sqltide/compare/v0.23.0...v0.24.0) (2026-08-20)


### Features

* **adapter:** send configured ClickHouse session settings ([0d672d4](https://github.com/chio-labs/sqltide/commit/0d672d4116ff99e47db33388eec6e47b3cc81600))


### Bug Fixes

* **cli:** stop claiming --start-time is virtual only ([e5c9582](https://github.com/chio-labs/sqltide/commit/e5c95828793c6fec55345769d36e62278667adc1))
* **compiler:** stop consuming the retained tree when resolving aliased refs ([18feb0d](https://github.com/chio-labs/sqltide/commit/18feb0da59fb6fa018a546658ac25cd09c592dc2))


### Performance Improvements

* **compiler:** collect every model tree fact in one traversal ([664b33f](https://github.com/chio-labs/sqltide/commit/664b33f7fb95a2f2085cc7dd702e29c0a56b31e4))
* **compiler:** reach SQLBuild compile parity by removing unread analysis work ([65bce29](https://github.com/chio-labs/sqltide/commit/65bce29fb218f45ffb7169d8e64e912242a0922e))
* **compiler:** resolve each model tree in one traversal ([fabf1e1](https://github.com/chio-labs/sqltide/commit/fabf1e112ecec5a8f3555e7025098f3e631d5139))
* **compiler:** resolve references by substitution instead of rendering ([a703c01](https://github.com/chio-labs/sqltide/commit/a703c01afec5f5b356a7c22d05592deed2bd7fbc))
* **compiler:** restore the dropped SQLBuild scanner skip and stop re-walking trees ([6e1ca2c](https://github.com/chio-labs/sqltide/commit/6e1ca2cb2a90b9a96178a7c7f1c400e78cb0b1ce))
* **compiler:** restore the reference scanner skip and stop deep copying trees ([3716397](https://github.com/chio-labs/sqltide/commit/3716397ae1c2355e9ce66b40ee6a232177a8b332))
* **compiler:** stop rendering canonical SQL that nothing reads ([39d8582](https://github.com/chio-labs/sqltide/commit/39d8582badc82104b4d8128e43d32acf9db53138))
* **compiler:** walk each parsed model tree once per purpose ([a9df82f](https://github.com/chio-labs/sqltide/commit/a9df82f7da176690c63de455ef8209cb4ced820d))
* **dev-server:** serve one background-refreshed warehouse overlay ([3c5e703](https://github.com/chio-labs/sqltide/commit/3c5e70343ec85afae776626077168645c266e057))

## [0.23.0](https://github.com/chio-labs/sqltide/compare/v0.22.4...v0.23.0) (2026-08-19)


### Features

* **adapter:** prove refreshable views against clickhouse and report their state ([ea6a4e9](https://github.com/chio-labs/sqltide/commit/ea6a4e938cdd23872a9ba7077f8667fbd36a9432))
* **adapter:** realize scheduled postgres sources as refreshable views ([0a206b4](https://github.com/chio-labs/sqltide/commit/0a206b42c7170fd1263e43ac52ef5f28e559a3f8))
* **compiler:** discover scheduled postgres refresh sources ([ddafdd4](https://github.com/chio-labs/sqltide/commit/ddafdd4973920777813fd9836c3cdd7df24463f5))
* scheduled Postgres refresh sources (CHI-56) ([c6f85d1](https://github.com/chio-labs/sqltide/commit/c6f85d172bac9ef048e6988f43c4273a29361a2b))


### Bug Fixes

* **auth:** resolve proxy identities that a competing writer just linked ([e40ebda](https://github.com/chio-labs/sqltide/commit/e40ebdad16692bd5562fc38d60ca39f773c3f04e))
* **clickhouse:** test against the ClickHouse version production runs ([f8fe102](https://github.com/chio-labs/sqltide/commit/f8fe1024c3b6a37fcf7cfdad6bbd8a2ecc004b24))
* **e2e:** expect the sqlbuild-style select list the run dialog builds ([c0fd059](https://github.com/chio-labs/sqltide/commit/c0fd059bff308c0d551b34d73ad294c7b1b184cd))
* **ui:** never render a stale plan behind a plan error ([709379d](https://github.com/chio-labs/sqltide/commit/709379d2b31b79758633a0dc8317e7aaad8060ca))

## [0.22.4](https://github.com/chio-labs/sqltide/compare/v0.22.3...v0.22.4) (2026-08-19)


### Bug Fixes

* **ui:** derive plan command locally and gate stale plan behind loading ([53fb74d](https://github.com/chio-labs/sqltide/commit/53fb74d1b11dbf1d6070ebef0483a9134013afd5))
* **ui:** show loading spinner instead of compile flash on live run detail ([927d2d4](https://github.com/chio-labs/sqltide/commit/927d2d44f76d657e942910382b8cbd6729f35d22))

## [0.22.3](https://github.com/chio-labs/sqltide/compare/v0.22.2...v0.22.3) (2026-08-19)


### Bug Fixes

* **cli:** resolve --select as a global name list with pipeline/model sugar ([85aaa52](https://github.com/chio-labs/sqltide/commit/85aaa521f945c2e2c014490271d4f3626f3e7494))
* sqlbuild-style --select (name lists + bare pipeline names) and safe selection ([1cc750f](https://github.com/chio-labs/sqltide/commit/1cc750f26b267348bb377db0613ea0e433d76e97))
* **ui:** accept --select lists and bare pipeline names; generate one --select ([8c1f81d](https://github.com/chio-labs/sqltide/commit/8c1f81d901d7e5517346095e2f47568df3c1c9c9))

## [0.22.2](https://github.com/chio-labs/sqltide/compare/v0.22.1...v0.22.2) (2026-08-18)


### Bug Fixes

* **compiler:** conjoin replay predicates into the outer WHERE clause ([fd1db10](https://github.com/chio-labs/sqltide/commit/fd1db10b04d86683db119997eca4541e6ccafa44))
* **compiler:** preserve author bytes through replay and shadow SQL rewrites ([8f51d83](https://github.com/chio-labs/sqltide/commit/8f51d83a11762b7a13338492ed27bf8d541b1294))
* **compiler:** preserve authored SQL bytes in executed database templates ([312c716](https://github.com/chio-labs/sqltide/commit/312c716ce955bdf5de2b660791b9480cdd962d1f))
* **compiler:** reject raw model relations and scope union CTE visibility ([dfa0945](https://github.com/chio-labs/sqltide/commit/dfa0945452d157c9f579e476cd6c18b83b9dd12b))

## [0.22.1](https://github.com/chio-labs/sqltide/compare/v0.22.0...v0.22.1) (2026-08-18)


### Bug Fixes

* **observability:** persist full run errors and enlarge the error dialog ([8cc6896](https://github.com/chio-labs/sqltide/commit/8cc6896e2d106afa260a0dbd127bd3b6b3b3ad83))

## [0.22.0](https://github.com/chio-labs/sqltide/compare/v0.21.3...v0.22.0) (2026-08-18)


### Features

* show executed SQL in run timelines ([4d3d10a](https://github.com/chio-labs/sqltide/commit/4d3d10ab3f744291079fabffd5b14411906d9466))
* **ui:** expandable/modal error viewer for runs and deployments (CHI-52) ([66ab83c](https://github.com/chio-labs/sqltide/commit/66ab83cb558f5ebf91c7fa88d65ab5bde8138464))


### Bug Fixes

* **observability:** skip run-statement persistence when the adapter renders none ([74b68d3](https://github.com/chio-labs/sqltide/commit/74b68d30777744a326920d56ecd7c7c94408ff4c))

## [0.21.3](https://github.com/chio-labs/sqltide/compare/v0.21.2...v0.21.3) (2026-08-18)


### Bug Fixes

* surface pending warehouse outages ([adede0c](https://github.com/chio-labs/sqltide/commit/adede0ca29ef803dfa19b9e32c49f4d2d191d110))

## [0.21.2](https://github.com/chio-labs/sqltide/compare/v0.21.1...v0.21.2) (2026-08-18)


### Bug Fixes

* keep the dev UI available through warehouse outages ([e9bc7dd](https://github.com/chio-labs/sqltide/commit/e9bc7dd6deebedeeb91f325c6afdf44fe983aa5c))

## [0.21.1](https://github.com/chio-labs/sqltide/compare/v0.21.0...v0.21.1) (2026-08-17)


### Bug Fixes

* allow disabled authentication on shared bind addresses ([b511d93](https://github.com/chio-labs/sqltide/commit/b511d93ba56cdcefdead297d09066e1fecd959e8))

## [0.21.0](https://github.com/chio-labs/sqltide/compare/v0.20.0...v0.21.0) (2026-08-16)


### Features

* add a tick timeline to the sensor detail page ([ce105ba](https://github.com/chio-labs/sqltide/commit/ce105baa82b37ce2d3f108122c21410b3ded1c66))
* add authentication policy and durable sensors ([d516bce](https://github.com/chio-labs/sqltide/commit/d516bce8a046eee667ebad2d36dbc36bbb82f8bf))
* align sensors and users pages with the list design language ([64d2120](https://github.com/chio-labs/sqltide/commit/64d212023511690bf88e9d81371b38ec72d167fe))
* explain dead letters in the sensor detail panel ([0ff8783](https://github.com/chio-labs/sqltide/commit/0ff8783277c3157618d5ef56a9b862918acb211e))
* give each sensor a dedicated detail page ([1a86ffb](https://github.com/chio-labs/sqltide/commit/1a86ffbe06a070b26ca8c7a9bdd4d168ef977f56))
* make the tick timeline a zoomable time axis ([a5b15f7](https://github.com/chio-labs/sqltide/commit/a5b15f77af69af045765bc935e84aa0dc2672590))
* move dead letters into the sensor detail panel ([f3cc663](https://github.com/chio-labs/sqltide/commit/f3cc66336f77ee3be1196c36abe8d13c1ab9be50))
* redesign users and sensors pages ([9e2c9aa](https://github.com/chio-labs/sqltide/commit/9e2c9aa779f8b5042649f8e144531e01f32367e2))


### Bug Fixes

* harden authentication and authorization boundaries ([378733f](https://github.com/chio-labs/sqltide/commit/378733ff0b1446befa6eeb394b8b9a6df067b47d))

## [0.20.0](https://github.com/chio-labs/sqltide/compare/v0.19.1...v0.20.0) (2026-08-11)


### Features

* make Plan UI mode aware ([370ba8b](https://github.com/chio-labs/sqltide/commit/370ba8b9a429f749dffc7166097bd1561c63be7f))

## [0.19.1](https://github.com/chio-labs/sqltide/compare/v0.19.0...v0.19.1) (2026-08-11)


### Bug Fixes

* show full run ID on detail page ([39f4a3a](https://github.com/chio-labs/sqltide/commit/39f4a3adf2ffb4500f98116011bc725a8d90d471))

## [0.19.0](https://github.com/chio-labs/sqltide/compare/v0.18.1...v0.19.0) (2026-08-11)


### Features

* add build safety guardrails ([193aa2a](https://github.com/chio-labs/sqltide/commit/193aa2a5b6d87a420f8ef754943a98d6ababda87))
* enforce global pipeline naming uniqueness ([0dc5b62](https://github.com/chio-labs/sqltide/commit/0dc5b624e8ce5b7233207b277d45a27606c75092))


### Documentation

* tighten pipeline naming guidance ([1c67487](https://github.com/chio-labs/sqltide/commit/1c67487e7f848ecfd6c91a6ce3da3ae8e52bc14d))

## [0.18.1](https://github.com/chio-labs/sqltide/compare/v0.18.0...v0.18.1) (2026-08-10)


### Bug Fixes

* align deployment inventory columns ([d40f82b](https://github.com/chio-labs/sqltide/commit/d40f82b86ac41978531c9bc91f9417c591fe611f))
* preserve lineage activity and viewport ([c4a259c](https://github.com/chio-labs/sqltide/commit/c4a259c84880e492b60cb901a9bab0296774ea28))

## [0.18.0](https://github.com/chio-labs/sqltide/compare/v0.17.0...v0.18.0) (2026-08-10)


### Features

* render Plan before warehouse planning ([3ed7c05](https://github.com/chio-labs/sqltide/commit/3ed7c052c4273e33e87d0cd73e177ede6382da7f))

## [0.17.0](https://github.com/chio-labs/sqltide/compare/v0.16.6...v0.17.0) (2026-08-09)


### Features

* add lineage activity telemetry ([c390acd](https://github.com/chio-labs/sqltide/commit/c390acda03bb8c29906fa9b1bd799118daa4b08e))

## [0.16.6](https://github.com/chio-labs/sqltide/compare/v0.16.5...v0.16.6) (2026-08-09)


### Bug Fixes

* harden lineage rebuild safety ([518b3af](https://github.com/chio-labs/sqltide/commit/518b3af294e28de6c0bfe34d84d77a7fa5ea4c3e))
* scope active build conflicts ([c73fabb](https://github.com/chio-labs/sqltide/commit/c73fabbad216a3c7cf0aa2dab63e6271b41ea046))


### Documentation

* streamline project overview ([01f3971](https://github.com/chio-labs/sqltide/commit/01f397118ff52b9ef918862659bb7a52447e67a7))

## [0.16.5](https://github.com/chio-labs/sqltide/compare/v0.16.4...v0.16.5) (2026-08-09)


### Bug Fixes

* clarify stalled run recovery ([013ea96](https://github.com/chio-labs/sqltide/commit/013ea96636336098c166e9a64342f18efba377cb))

## [0.16.4](https://github.com/chio-labs/sqltide/compare/v0.16.3...v0.16.4) (2026-08-09)


### Bug Fixes

* align execution UI with runtime state ([600c683](https://github.com/chio-labs/sqltide/commit/600c683b1f03db35422634fdd55957b05c771d17))

## [0.16.3](https://github.com/chio-labs/sqltide/compare/v0.16.2...v0.16.3) (2026-08-09)


### Bug Fixes

* clarify promotion run events ([8f6ae31](https://github.com/chio-labs/sqltide/commit/8f6ae31d23efa5bffa9d610f04ad837f1713253d))
* humanize run event timeline ([d822320](https://github.com/chio-labs/sqltide/commit/d822320a06da8a1f4df97b2bffc331116a8686d4))

## [0.16.2](https://github.com/chio-labs/sqltide/compare/v0.16.1...v0.16.2) (2026-08-09)


### Bug Fixes

* clarify initial deployment publishing ([45408d4](https://github.com/chio-labs/sqltide/commit/45408d4e6ff4f3a18e3048a49ff5610167d749a3))
* stabilize scheduler and run state ([729f975](https://github.com/chio-labs/sqltide/commit/729f9754017fe5e0143204b561511546b0d6e9d1))

## [0.16.1](https://github.com/chio-labs/sqltide/compare/v0.16.0...v0.16.1) (2026-08-09)


### Bug Fixes

* stabilize UI loading transitions ([5e0562e](https://github.com/chio-labs/sqltide/commit/5e0562e5c1868873a862245c50aadaaa4d755fca))

## [0.16.0](https://github.com/chio-labs/sqltide/compare/v0.15.0...v0.16.0) (2026-08-09)


### Features

* improve UI loading and scheduler status ([e6b1446](https://github.com/chio-labs/sqltide/commit/e6b1446e5955747b19508e653b22588b7652a4a4))


### Bug Fixes

* scope direct source preparation to selection ([28206e1](https://github.com/chio-labs/sqltide/commit/28206e1f219912d93c565528585ece6c5023dbfd))

## [0.15.0](https://github.com/chio-labs/sqltide/compare/v0.14.1...v0.15.0) (2026-08-09)


### Features

* scope Kafka consumer groups by target ([fc7f7c4](https://github.com/chio-labs/sqltide/commit/fc7f7c48b982d071b41270b48e5c6a2e2f39bc86))

## [0.14.1](https://github.com/chio-labs/sqltide/compare/v0.14.0...v0.14.1) (2026-08-09)


### Bug Fixes

* serialize immutable mappings in fingerprints ([3f6d68a](https://github.com/chio-labs/sqltide/commit/3f6d68a40b0ff87d610f18621a61f0fe12ccadc0))

## [0.14.0](https://github.com/chio-labs/sqltide/compare/v0.13.0...v0.14.0) (2026-08-09)


### Features

* complete virtual deployment lifecycle ([acfbf58](https://github.com/chio-labs/sqltide/commit/acfbf5835b74113e65bf8135c01fd5e0ce1e0c30))
* derive Kafka source names with macros ([e30223d](https://github.com/chio-labs/sqltide/commit/e30223d4ce955fb554ee0ebe7fd1103625944f63))
* expose deployment promote, cleanup and diff over the dev API ([71f3372](https://github.com/chio-labs/sqltide/commit/71f33723150ad89f1cdb195487cf0e0c9d03a2fb))
* promote, clean up and diff deployments from the UI ([d8231f3](https://github.com/chio-labs/sqltide/commit/d8231f3c3d663e7cfd76697a0e52fef9ed351b56))
* show deployment relations and orphans in the physical view ([e8e6414](https://github.com/chio-labs/sqltide/commit/e8e64142f007f6eee82ed80d68aea69fc8a9eeb3))
* show the switchover model by model on the run page ([1a46901](https://github.com/chio-labs/sqltide/commit/1a469018c5356cd9a2972c313a604ba58f2d3a32))
* surface virtual deployments in the dev UI ([5d64f69](https://github.com/chio-labs/sqltide/commit/5d64f692c71c25a8bebb5e339565315e6b49be3e))


### Bug Fixes

* measure virtual-mode models by the relation they are bound to ([914a48c](https://github.com/chio-labs/sqltide/commit/914a48c203f089594e8c67560f72958ca23215ae))
* offer rollback on superseded deployments ([83c128a](https://github.com/chio-labs/sqltide/commit/83c128a836cbd053ea02b3489d0235bf77a451ea))

## [0.13.0](https://github.com/chio-labs/sqltide/compare/v0.12.4...v0.13.0) (2026-08-08)


### Features

* add per-pipeline build modes ([95b892c](https://github.com/chio-labs/sqltide/commit/95b892cbf3d63b1f172eed1e16a697f1a203a409))

## [0.12.4](https://github.com/chio-labs/sqltide/compare/v0.12.3...v0.12.4) (2026-08-08)


### Bug Fixes

* label the overview source card row count as rows ([7570be8](https://github.com/chio-labs/sqltide/commit/7570be87610918b267a1ef0be960203e978a3d5f))
* reset offsets for fresh source landings ([6a61125](https://github.com/chio-labs/sqltide/commit/6a61125ea6452b936fe4cacb4bfe3f14c811dca1))

## [0.12.3](https://github.com/chio-labs/sqltide/compare/v0.12.2...v0.12.3) (2026-08-08)


### Bug Fixes

* adopt Console-style payload view, stable columns, and match highlighting ([a8c7955](https://github.com/chio-labs/sqltide/commit/a8c79555a297b16597e77b1b7553ff1f900a453b))
* always show broker timestamp and make message columns sortable ([b98ee7e](https://github.com/chio-labs/sqltide/commit/b98ee7ebd8f58086122cfb5457bf6f4b708ea30a))
* paginate the message list and default to broker timestamp order ([a2237da](https://github.com/chio-labs/sqltide/commit/a2237dac9344f0ad2569cbeeee2f606ff4d27c90))
* raise the full-record cap to 16 MiB ([b7ffc03](https://github.com/chio-labs/sqltide/commit/b7ffc03c6f8e590192988283ea1b35cbe55ee007))

## [0.12.2](https://github.com/chio-labs/sqltide/compare/v0.12.1...v0.12.2) (2026-08-08)


### Bug Fixes

* gate topics navigation on the first inventory load ([8712dd4](https://github.com/chio-labs/sqltide/commit/8712dd4edb4a9e50ff6dae9fa6581a430023f712))
* keep dev app startup warmer within comment policy ([e04bcc6](https://github.com/chio-labs/sqltide/commit/e04bcc6d4ddab1c94ba7dbf8b1fc8bc491051255))
* keep the topics inventory across navigations ([d6c22c4](https://github.com/chio-labs/sqltide/commit/d6c22c4171885a9f7ad1248833b799cec254b8bf))

## [0.12.1](https://github.com/chio-labs/sqltide/compare/v0.12.0...v0.12.1) (2026-08-08)


### Bug Fixes

* default topics page to managed topics and link topic names ([3582faf](https://github.com/chio-labs/sqltide/commit/3582faf5568e898c517628ea247828ed89455cf5))
* stabilise message browser layout and adopt debounced auto-search ([4f820dd](https://github.com/chio-labs/sqltide/commit/4f820ddfa20706eb67e6948c25715b001b42c247))
* warm broker metadata caches at dev server startup ([1226094](https://github.com/chio-labs/sqltide/commit/12260948713566cfc0b3450d86ba7f16c997f881))

## [0.12.0](https://github.com/chio-labs/sqltide/compare/v0.11.0...v0.12.0) (2026-08-08)


### Features

* add source message browser and topics inventory ([40afebe](https://github.com/chio-labs/sqltide/commit/40afebe06d82645899671e5843d462efc28bf2f0))

## [0.11.0](https://github.com/chio-labs/sqltide/compare/v0.10.0...v0.11.0) (2026-08-08)


### Features

* add quality identities and audit scheduling ([d9b88ba](https://github.com/chio-labs/sqltide/commit/d9b88ba73c22302eb7f65ce9561d4270d097ff0a))


### Bug Fixes

* guard empty Release Please outputs ([32b9cb6](https://github.com/chio-labs/sqltide/commit/32b9cb691c53862aa87b7bdb43a720b715cecd79))

## [0.10.0](https://github.com/chio-labs/sqltide/compare/v0.9.3...v0.10.0) (2026-08-08)


### Features

* add pipeline safeguards and Kafka observability ([124f735](https://github.com/chio-labs/sqltide/commit/124f7353463e0ac487de9d56f7ce624d03d0908e))

## [0.9.3](https://github.com/chio-labs/sqltide/compare/v0.9.2...v0.9.3) (2026-08-07)


### Bug Fixes

* keep release lockfile synchronized ([ac15b72](https://github.com/chio-labs/sqltide/commit/ac15b721cc476918052eeb73b3be0c835214601d))

## [0.9.2](https://github.com/chio-labs/sqltide/compare/v0.9.1...v0.9.2) (2026-08-07)


### Bug Fixes

* complete plan replay window controls ([37b31d0](https://github.com/chio-labs/sqltide/commit/37b31d03c08a2fa1634ad6be6d452256a421fcf5))
* harden API and missing run states ([fa842f1](https://github.com/chio-labs/sqltide/commit/fa842f1bfa43827982ae7406eeadba23e9fbefe3))
* make core UI usable on mobile ([ced9a42](https://github.com/chio-labs/sqltide/commit/ced9a428cc5c0f3e00b20fdbc5ec01e269c5a6ae))

## [0.9.1](https://github.com/chio-labs/sqltide/compare/v0.9.0...v0.9.1) (2026-08-07)


### Bug Fixes

* align dev UI with persisted model state ([c534296](https://github.com/chio-labs/sqltide/commit/c5342965109b673f583e7a1ee1bba9e5efcf080d))
* pin development Python to 3.12 ([1b83a6e](https://github.com/chio-labs/sqltide/commit/1b83a6e57b713fc589604372068bb72f91842541))
* refresh lockfile project version ([0cb2b24](https://github.com/chio-labs/sqltide/commit/0cb2b24ba5e068744bbcc96f62e1bdec6ba8f2ce))

## [0.9.0](https://github.com/chio-labs/sqltide/compare/v0.8.0...v0.9.0) (2026-08-07)


### Features

* improve CLI plan and error presentation ([74d1b62](https://github.com/chio-labs/sqltide/commit/74d1b6265c5eaf0dbe23f562e204c1679d7aa952))


### Bug Fixes

* package dev UI assets in distributions ([1cb9a84](https://github.com/chio-labs/sqltide/commit/1cb9a84576f6633edcd21a513334b619ef8a651a))

## [0.8.0](https://github.com/chio-labs/sqltide/compare/v0.7.0...v0.8.0) (2026-08-07)


### Features

* add durable UI run execution model ([3146296](https://github.com/chio-labs/sqltide/commit/314629699e3be601ffcc0702914f93bd1c4ed304))

## [0.7.0](https://github.com/chio-labs/sqltide/compare/v0.6.0...v0.7.0) (2026-08-07)


### ⚠ BREAKING CHANGES

* remove the direct-build preflight phase and retention coverage state

### Features

* --events JSONL stream and durable _sqltide_run_events timeline ([b081b7a](https://github.com/chio-labs/sqltide/commit/b081b7a4ffdd9dd7229671c84903a9e81e758927))
* add authored source freshness policies ([0a6e90a](https://github.com/chio-labs/sqltide/commit/0a6e90a9e5768e8905039178c2e6a70ca0bc20f2))
* add deployment CLI resource family ([c9abde7](https://github.com/chio-labs/sqltide/commit/c9abde75ce5d60e9337f075519c0536140a92623))
* add description to the MODEL() header ([d574e88](https://github.com/chio-labs/sqltide/commit/d574e88549b9768acd204468fbc116ca24b7e4b8))
* add sqt dev ([4b2043d](https://github.com/chio-labs/sqltide/commit/4b2043dcd28e9f6665eca4de086ceb9023217ec6))
* checks history, dagster-aligned runs table, plan preload ([8e179b5](https://github.com/chio-labs/sqltide/commit/8e179b52903dadffdf8babbec4189f56c5623d00))
* dagster-style runs page, snapshot refresh, macro descriptions, and gap cleanup ([559e9d4](https://github.com/chio-labs/sqltide/commit/559e9d4085c8f8b13df7275ba3280edfeba4c053))
* declare generic audits in the MODEL() header; delete schema.yml ([efa1ef9](https://github.com/chio-labs/sqltide/commit/efa1ef94c21c1b3137ba41151471ee2e4160588f))
* dev UI - SvelteKit frontend, server-backed API client, and build glue ([e3546b2](https://github.com/chio-labs/sqltide/commit/e3546b20f014de1e8ba504a6a11ae1501c5df5aa))
* dev_server core - compile state, status, reload, definitions ([60a0d93](https://github.com/chio-labs/sqltide/commit/60a0d93da7d73f6b33986d8348f43036e2604746))
* dev_server live state - the /api/state warehouse overlay ([036b947](https://github.com/chio-labs/sqltide/commit/036b947661836a691edd4501054870bf4b9f2234))
* dev_server plan, checks, and run history endpoints ([50f1ecd](https://github.com/chio-labs/sqltide/commit/50f1ecdbbeba4e7200e9138bcb216f543d8211fc))
* execute from the UI — plan Execute, live run page, lineage run panel ([e571f64](https://github.com/chio-labs/sqltide/commit/e571f642d6ff85a7f826176a2439cdb0306d928a))
* give sqt dev a terminal voice — startup banner and live activity feed ([deab811](https://github.com/chio-labs/sqltide/commit/deab811b0f2cb508fa5a02f2ee37e11763a5eaaa))
* honest plan numbers, sqt --version, and remaining dev UI gaps ([e49c965](https://github.com/chio-labs/sqltide/commit/e49c965ff477c2a306daa4e222361481fe1dc212))
* POST /api/build — single-flight subprocess execution with live feed ([6608506](https://github.com/chio-labs/sqltide/commit/6608506e4b44e29c71c768b9138392dcd71b2a6f))
* remove the direct-build preflight phase and retention coverage state ([1bc7fd3](https://github.com/chio-labs/sqltide/commit/1bc7fd365c57992aaf91cf90bbb7bacae7b2331f))


### Bug Fixes

* dev UI — dead controls, shallow-routing filters, and fabricated data ([b354595](https://github.com/chio-labs/sqltide/commit/b354595ac677ce0e8a397b32631cc243e22904f9))
* honest timestamps, live runs list, and Ctrl+C leaves a record ([f351fde](https://github.com/chio-labs/sqltide/commit/f351fde4577915acacdeb847c81abf75309835d8))
* preserve UTC replay start times ([b333bf7](https://github.com/chio-labs/sqltide/commit/b333bf7d76743426368855b2abbb171384149808))

## [0.6.0](https://github.com/chio-labs/sqltide/compare/v0.5.0...v0.6.0) (2026-08-03)


### Features

* add append-only metadata history ([7a2807c](https://github.com/chio-labs/sqltide/commit/7a2807c27c4860b59fba909be3fbd13585d7d632))
* enforce observability non-authority ([ae32f2f](https://github.com/chio-labs/sqltide/commit/ae32f2f03be627ac872f4bf7fb17313eeafa8051))

## [0.5.0](https://github.com/chio-labs/sqltide/compare/v0.4.0...v0.5.0) (2026-08-02)


### Features

* support bounded direct start times ([8468e83](https://github.com/chio-labs/sqltide/commit/8468e8312ed4761c059e480b7477d59682be1f77))

## [0.4.0](https://github.com/chio-labs/sqltide/compare/v0.3.0...v0.4.0) (2026-08-02)


### Features

* enforce executable build workflows ([179a2c7](https://github.com/chio-labs/sqltide/commit/179a2c76f15425980fc7312f4d4f2761f3bd8159))
* expose exact plan workflows ([1d8df01](https://github.com/chio-labs/sqltide/commit/1d8df011238cc220a0fe795ac5774b78aa62411c))
* persist connected plan artifacts ([7ac3f8a](https://github.com/chio-labs/sqltide/commit/7ac3f8aa462eb2a1723001ccdbe5c0805efa20cb))
* unify mode-aware builds ([2b6434f](https://github.com/chio-labs/sqltide/commit/2b6434f44c57a66c2c73b8052cab12ae994a7f78))

## [0.3.0](https://github.com/chio-labs/sqltide/compare/v0.2.0...v0.3.0) (2026-08-01)


### Features

* add terminal view models ([f2b1974](https://github.com/chio-labs/sqltide/commit/f2b1974a5d0f9ad25bbf615675e6cb521e1b3f6a))


### Bug Fixes

* use warehouse time for replay boundaries ([c6d6a78](https://github.com/chio-labs/sqltide/commit/c6d6a7815122bbb80342aae61e3450a7e6b277e0))

## [0.2.0](https://github.com/chio-labs/sqltide/compare/v0.1.0...v0.2.0) (2026-07-31)


### Features

* add adopted-source standard builds ([f14de47](https://github.com/chio-labs/sqltide/commit/f14de47874e52dcff4e9da18bb163541eca97401))
* add managed source TTL configuration ([28a9cec](https://github.com/chio-labs/sqltide/commit/28a9cec67835d108a26b4c9cf4279411873de0af))
* add standard-mode plan with scope, ownership, and replay roots ([5357baa](https://github.com/chio-labs/sqltide/commit/5357baa12da3587d0a4c147bc10bf19e5d6b925a))
* add standard-mode sqt build with ownership and boundary contract ([d8a1a21](https://github.com/chio-labs/sqltide/commit/d8a1a21c45c6525ecea39cf95b869813303e29cd))
* adopt direct mode and SQLBuild model headers ([8d31827](https://github.com/chio-labs/sqltide/commit/8d318276106ce18131f7a548ced14fd89dba9e17))
* complete standard build rerun recovery ([f3d1c53](https://github.com/chio-labs/sqltide/commit/f3d1c539267e443f2b344c96e66e8e69f70d1e78))
* complete standard selected downstream rebuild ([f930cc1](https://github.com/chio-labs/sqltide/commit/f930cc11b68912f7835d0eeb2f71fd5ee55db101))
* gate commands by effective mode ([4c30d0a](https://github.com/chio-labs/sqltide/commit/4c30d0a7ed74e75cf2c9eb13948522c776407b80))
* infer pipeline sources from models ([178beb2](https://github.com/chio-labs/sqltide/commit/178beb290f8a971b3396c86766e814be277cd2e2))
* initialize SQLTide project ([c63ff98](https://github.com/chio-labs/sqltide/commit/c63ff9849303c6ae529fdf8a352febb253ba0a50))
* mature chained SQL tests and macro tests ([d511082](https://github.com/chio-labs/sqltide/commit/d5110827123c93bef52e2992dc5e41edd67103f6))
* migrate remaining SQL analysis to Polyglot ([7750bb1](https://github.com/chio-labs/sqltide/commit/7750bb1e4a463fe26eefc50c3232351a61dd0320))
* migrate replay SQL to Polyglot ([757845f](https://github.com/chio-labs/sqltide/commit/757845f5f326b2bacf2b9f5c6f3b412bff5e9d00))
* rename CLI command to sqt ([4bf2f64](https://github.com/chio-labs/sqltide/commit/4bf2f642e850188fe344e364bfdc20378ec2f6bf))
* unify replay population execution ([9e1a14d](https://github.com/chio-labs/sqltide/commit/9e1a14d202ffa0af88a24b680b35a022d9d0feff))


### Bug Fixes

* lint the standard build package that gitignore had hidden ([d6b673f](https://github.com/chio-labs/sqltide/commit/d6b673f24c014ccda705508d95f925d341df5377))
* remediate migration regressions ([7f5b61b](https://github.com/chio-labs/sqltide/commit/7f5b61b07675bf6de796b5e5ae642288b6326a23))
* repair the CLI output preview script and split it into a package ([f1e79c2](https://github.com/chio-labs/sqltide/commit/f1e79c20e9cbeea3d38c1dd540e5232ea6439544))


### Documentation

* add SQLTide logo ([b21ee99](https://github.com/chio-labs/sqltide/commit/b21ee990f19b44f49756a369423c768b9448a4c6))
* polish publication-facing guidance ([c346c20](https://github.com/chio-labs/sqltide/commit/c346c20647eb23b85de6739708f0f35451d023d6))
