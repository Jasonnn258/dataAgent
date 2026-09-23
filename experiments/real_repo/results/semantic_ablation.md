# Semantic ResolveTarget Ablation (Phase 12B)

tasks: 81 rows | arms: d0, d1, raw | seed_counts: {'chalk': 31, 'zustand': 40, 'express': 19}

| arm | tasks | scored | unresolved | recall@1 | recall@3 | mapping_acc | avg_ms | llm_calls | prompt_tok | compl_tok |
|---|---|---|---|---|---|---|---|---|---|---|
| d0 | 27 | 14 | 0.222 | 0.503 | 0.508 | 0.1 | 0.3 | 0 | 0 | 0 |
| d1 | 27 | 14 | 0.111 | 0.582 | 0.596 | 0.1 | 20313.0 | 27 | 43783 | 20798 |
| raw | 27 | 14 | 0.926 | 0.0 | 0.0 | 0.0 | 0.3 | 0 | 0 | 0 |

## Per-task (scored arms 并排)

| task | repo | type | gold_files | d0 top1 | d1 top1 | raw top1 |
|---|---|---|---|---|---|---|
| chalk-compound-truecolor | chalk | compound | source/vendor/supports-color/index.js | *unresolved* (r@1=0.0) | _supportsColor [llm] r@1=1.0 | *unresolved* (r@1=0.0) |
| chalk-history-color-level | chalk | history | — | assertValidLevel [lexical] r@1= | assertValidLevel [lexical] r@1= | *unresolved* (r@1=) |
| chalk-history-deps | chalk | history | — | ansi [lexical] r@1= | ansi [lexical] r@1= | *unresolved* (r@1=) |
| chalk-impact-ansi256 | chalk | impact | source/index.js | ansi [lexical] r@1=1.0 | ansi [lexical] r@1=1.0 | *unresolved* (r@1=0.0) |
| chalk-impact-supportscolor | chalk | impact | source/vendor/supports-color/index.js | hasNumericForceColor [lexical] r@1=1.0 | hasNumericForceColor [lexical] r@1=1.0 | *unresolved* (r@1=0.0) |
| chalk-locate-ansi256 | chalk | locate | source/index.js | ansi [lexical] r@1=1.0 | ansi [lexical] r@1=1.0 | *unresolved* (r@1=0.0) |
| chalk-locate-color-support | chalk | locate | source/index.js,source/vendor/supports-color/index.js | hasNumericForceColor [lexical] r@1=0.5 | hasNumericForceColor [lexical] r@1=0.5 | *unresolved* (r@1=0.0) |
| chalk-locate-readme | chalk | locate | — | *unresolved* (r@1=) | *unresolved* (r@1=) | *unresolved* (r@1=) |
| chalk-locate-styles-export | chalk | locate | — | styleFunction [lexical] r@1= | styleFunction [lexical] r@1= | *unresolved* (r@1=) |
| chalk-rollback-perf-vs-styles | chalk | rollback | source/index.d.ts,source/index.js,source/utilities.js,source/vendor/ansi-styles/index.d.ts,source/vendor/supports-color/browser.js,source/vendor/supports-color/index.js,test/chalk.js,test/force-color.js,test/instance.js | *unresolved* (r@1=0.0) | stringReplaceAll [llm] r@1=0.111 | *unresolved* (r@1=0.0) |
| express-history-send-encoding | express | history | — | sendfile [lexical] r@1= | sendfile [lexical] r@1= | *unresolved* (r@1=) |
| express-impact-etag | express | impact | — | createETagGenerator [lexical] r@1= | createETagGenerator [lexical] r@1= | *unresolved* (r@1=) |
| express-impact-etag-gen | express | impact | lib/utils.js | createETagGenerator [lexical] r@1=1.0 | createETagGenerator [lexical] r@1=1.0 | *unresolved* (r@1=0.0) |
| express-locate-etag | express | locate | lib/utils.js,lib/response.js | createETagGenerator [lexical] r@1=0.5 | createETagGenerator [lexical] r@1=0.5 | *unresolved* (r@1=0.0) |
| express-locate-router | express | locate | — | *unresolved* (r@1=) | *unresolved* (r@1=) | *unresolved* (r@1=) |
| express-locate-send | express | locate | lib/response.js | sendfile [lexical] r@1=1.0 | sendfile [lexical] r@1=1.0 | *unresolved* (r@1=0.0) |
| express-rollback-send-vs-docs | express | rollback | lib/response.js,test/res.send.js | sendfile [lexical] r@1=0.5 | sendfile [lexical] r@1=0.5 | *unresolved* (r@1=0.0) |
| zustand-compound-default-export | zustand | compound | — | *unresolved* (r@1=) | createImpl [llm] r@1= | *unresolved* (r@1=) |
| zustand-history-extract-react | zustand | history | — | extractConnectionInformation [lexical] r@1= | extractConnectionInformation [lexical] r@1= | *unresolved* (r@1=) |
| zustand-history-v5 | zustand | history | — | *unresolved* (r@1=) | *unresolved* (r@1=) | *unresolved* (r@1=) |
| zustand-impact-createsource | zustand | impact | — | removeStoreFromTrackedConnections [lexical] r@1= | removeStoreFromTrackedConnections [lexical] r@1= | *unresolved* (r@1=) |
| zustand-impact-subscribe | zustand | impact | src/middleware/devtools.ts,tests/subscribe.test.tsx | removeStoreFromTrackedConnections [lexical] r@1=0.5 | removeStoreFromTrackedConnections [lexical] r@1=0.5 | *unresolved* (r@1=0.0) |
| zustand-locate-createstore | zustand | locate | src/vanilla.ts,src/index.ts | removeStoreFromTrackedConnections [lexical] r@1=0.0 | removeStoreFromTrackedConnections [lexical] r@1=0.0 | *unresolved* (r@1=0.0) |
| zustand-locate-middleware | zustand | locate | — | shouldDispatchFromDevtools [lexical] r@1= | shouldDispatchFromDevtools [lexical] r@1= | *unresolved* (r@1=) |
| zustand-locate-react-bindings | zustand | locate | — | TestUseShallowSimple [lexical] r@1= | TestUseShallowSimple [lexical] r@1= | TestUseShallowSimple [lexical] r@1= |
| zustand-locate-shallow | zustand | locate | src/shallow.ts | TestUseShallowSimple [lexical] r@1=0.0 | TestUseShallowSimple [lexical] r@1=0.0 | TestUseShallowSimple [lexical] r@1=0.0 |
| zustand-rollback-v5-vs-docs | zustand | rollback | .eslintrc.json,.github/workflows/test-multiple-builds.yml,.github/workflows/test-multiple-versions.yml,.github/workflows/test-old-typescript.yml,babel.config.js,package.json,pnpm-lock.yaml,rollup.config.js,src/context.ts,src/index.ts,src/middleware/devtools.ts,src/middleware/immer.ts,src/middleware/persist.ts,src/react.ts,src/react/shallow.ts,src/shallow.ts,src/traditional.ts,src/vanilla.ts,src/vanilla/shallow.ts,tests/basic.test.tsx,tests/context.test.tsx,tests/devtools.test.tsx,tests/ssr.test.tsx,tests/types.test.tsx,tests/vanilla/basic.test.ts,tests/vanilla/shallow.test.tsx | createJSONStorage [lexical] r@1=0.038 | createJSONStorage [lexical] r@1=0.038 | *unresolved* (r@1=0.0) |

> 指标：target_recall@k = top-k 候选文件覆盖 gold 文件比例；feature_mapping_accuracy = top-1 feature 名命中gold 符号（有符号 gold 的任务才计入）；unresolved = 零候选走确定性退路。raw 臂 = 12A 原状（只播 Next.js 形状种子）。