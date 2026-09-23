# Real Repository Benchmark (Phase 12A)

tasks: 27 | scored: 18 | pending gold: 9 | failed: 8

## Per-task results

| task | repo | type | status | scored | key metrics |
|---|---|---|---|---|---|
| chalk-locate-color-support | chalk | locate | failed | yes | file_precision=0.0 file_recall=0.0 symbol_hit=False unresolved=True |
| chalk-locate-ansi256 | chalk | locate | failed | yes | file_precision=0.0 file_recall=0.0 symbol_hit=False unresolved=True |
| chalk-locate-styles-export | chalk | locate | failed | no | *gold_source=pending* |
| chalk-impact-supportscolor | chalk | impact | success | yes | caller_precision=0.5 caller_recall=1.0 |
| chalk-impact-ansi256 | chalk | impact | success | yes | caller_precision=0.5 caller_recall=1.0 |
| chalk-history-color-level | chalk | history | success | yes | commit_recall=0.667 |
| chalk-rollback-perf-vs-styles | chalk | rollback | failed | yes | rollback_precision=0.0 rollback_recall=0.0 preservation_rate=1.0 collateral_damage=0 |
| chalk-compound-truecolor | chalk | compound | failed | yes | file_precision=0.0 file_recall=0.0 commit_recall=0.0 rollback_precision=0.0 rollback_recall=0.0 policy_action_match=1 |
| chalk-locate-readme | chalk | locate | failed | no | *gold_source=pending* |
| chalk-history-deps | chalk | history | success | no | *gold_source=pending* |
| zustand-locate-createstore | zustand | locate | success | yes | file_precision=0.0 file_recall=0.0 symbol_hit=True unresolved=False |
| zustand-locate-shallow | zustand | locate | success | yes | file_precision=0.0 file_recall=0.0 symbol_hit=True unresolved=False |
| zustand-locate-middleware | zustand | locate | success | no | *gold_source=pending* |
| zustand-impact-subscribe | zustand | impact | success | yes | caller_precision=0.167 caller_recall=0.5 |
| zustand-impact-createsource | zustand | impact | success | no | *gold_source=pending* |
| zustand-history-extract-react | zustand | history | success | yes | commit_recall=1.0 |
| zustand-history-v5 | zustand | history | success | yes | commit_recall=0.5 |
| zustand-rollback-v5-vs-docs | zustand | rollback | success | yes | rollback_precision=0.117 rollback_recall=0.577 preservation_rate=0.0 collateral_damage=3 |
| zustand-compound-default-export | zustand | compound | success | no | *gold_source=pending* |
| zustand-locate-react-bindings | zustand | locate | success | no | *gold_source=pending* |
| express-locate-send | express | locate | success | yes | file_precision=1.0 file_recall=1.0 symbol_hit=False unresolved=False |
| express-locate-etag | express | locate | success | yes | file_precision=1.0 file_recall=0.5 symbol_hit=False unresolved=False |
| express-impact-etag-gen | express | impact | success | yes | caller_precision=1.0 caller_recall=1.0 |
| express-history-send-encoding | express | history | success | yes | commit_recall=1.0 |
| express-locate-router | express | locate | success | no | *gold_source=pending* |
| express-impact-etag | express | impact | failed | no | *gold_source=pending* |
| express-rollback-send-vs-docs | express | rollback | failed | yes | rollback_precision=0.02 rollback_recall=1.0 preservation_rate=0.0 collateral_damage=1 |

## Summary by task type

| type | n | file_precision | file_recall | symbol_hit | caller_precision | caller_recall | route_precision | route_recall | commit_recall | rollback_precision | rollback_recall | preservation_rate | collateral_damage | policy_action_match | unresolved |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| compound | 1 | 0.0 | 0.0 |  |  |  |  |  | 0.0 | 0.0 | 0.0 |  |  | 1.0 |  |
| history | 4 |  |  |  |  |  |  |  | 0.792 |  |  |  |  |  |  |
| impact | 4 |  |  |  | 0.542 | 0.875 |  |  |  |  |  |  |  |  |  |
| locate | 6 | 0.333 | 0.25 | 0.333 |  |  |  |  |  |  |  |  |  |  | 0.333 |
| rollback | 3 |  |  |  |  |  |  |  |  | 0.046 | 0.526 | 0.333 | 1.333 |  |  |

## Failure cases

### chalk-locate-color-support (chalk, locate)
- error: ``
- failed skills: `{'resolve_target': 'failed'}`

### chalk-locate-ansi256 (chalk, locate)
- error: ``
- failed skills: `{'resolve_target': 'failed'}`

### chalk-locate-styles-export (chalk, locate)
- error: ``
- failed skills: `{'resolve_target': 'failed'}`

### chalk-rollback-perf-vs-styles (chalk, rollback)
- error: ``
- failed skills: `{'resolve_target': 'failed'}`

### chalk-compound-truecolor (chalk, compound)
- error: ``
- failed skills: `{'resolve_target': 'failed'}`

### chalk-locate-readme (chalk, locate)
- error: ``
- failed skills: `{'resolve_target': 'failed'}`

### express-impact-etag (express, impact)
- error: ``
- failed skills: `{'impact_analysis': 'failed'}`

### express-rollback-send-vs-docs (express, rollback)
- error: ``
- failed skills: `{'resolve_target': 'failed'}`


## Execution metrics

- avg latency: 166.326 ms
- avg evidence per task: 99.815
- total llm calls: 0

## Execution loop — analysis plan (sandbox, Phase 13)

tasks: 5 | scored: 4 | stopped_at: `{'build_execution_plan': 3, 'build_rollback_patch': 2}`

| task | repo | status | stopped_at | gates | changed | key exec metrics |
|---|---|---|---|---|---|---|
| chalk-rollback-perf-vs-styles | chalk | - | build_execution_plan | -/- | 0 | exec_applied=0 exec_verified=0 exec_precision=0.0 exec_recall=0.0 exec_preservation=1.0 exec_collateral=0 |
| chalk-compound-truecolor | chalk | - | build_execution_plan | -/- | 0 | exec_applied=0 exec_verified=0 exec_precision=0.0 exec_recall=0.0 |
| zustand-rollback-v5-vs-docs | zustand | CONFLICT | build_rollback_patch | PASS/- | 0 | exec_applied=0 exec_verified=0 exec_precision=0.0 exec_recall=0.0 exec_preservation=1.0 exec_collateral=0 |
| zustand-compound-default-export | zustand | - | build_execution_plan | -/- | 0 | *gold_source=pending* |
| express-rollback-send-vs-docs | express | CONFLICT | build_rollback_patch | HUMAN_REVIEW/- | 0 | exec_applied=0 exec_verified=0 exec_precision=0.0 exec_recall=0.0 exec_preservation=1.0 exec_collateral=0 |

**aggregates**: exec_applied=0.0 | exec_verified=0.0 | exec_precision=0.0 | exec_recall=0.0 | exec_preservation=1.0 | exec_collateral=0.0 (avg_changed_files=0.0)

## Execution loop — oracle plan (analysis bypassed)

tasks: 4 | scored: 4 | stopped_at: `{'post_gate': 1, 'build_rollback_patch': 3}`

| task | repo | status | stopped_at | gates | changed | key exec metrics |
|---|---|---|---|---|---|---|
| chalk-rollback-perf-vs-styles | chalk | VERIFIED | post_gate | PASS/PASS | 10 | exec_applied=1 exec_verified=1 exec_precision=0.9 exec_recall=1.0 exec_preservation=0.5 exec_collateral=1 |
| chalk-compound-truecolor | chalk | CONFLICT | build_rollback_patch | PASS/- | 0 | exec_applied=0 exec_verified=0 exec_precision=0.0 exec_recall=0.0 |
| zustand-rollback-v5-vs-docs | zustand | CONFLICT | build_rollback_patch | PASS/- | 0 | exec_applied=0 exec_verified=0 exec_precision=0.0 exec_recall=0.0 exec_preservation=1.0 exec_collateral=0 |
| express-rollback-send-vs-docs | express | CONFLICT | build_rollback_patch | PASS/- | 0 | exec_applied=0 exec_verified=0 exec_precision=0.0 exec_recall=0.0 exec_preservation=1.0 exec_collateral=0 |

**aggregates**: exec_applied=0.25 | exec_verified=0.25 | exec_precision=0.225 | exec_recall=0.25 | exec_preservation=0.833 | exec_collateral=0.333 (avg_changed_files=2.5)
