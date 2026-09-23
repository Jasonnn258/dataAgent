# Real Repository Benchmark (Phase 12A)

tasks: 27 | scored: 15 | pending gold: 12 | failed: 8

## Per-task results

| task | repo | type | status | scored | key metrics |
|---|---|---|---|---|---|
| chalk-locate-color-support | chalk | locate | failed | yes | file_precision=0.0 file_recall=0.0 symbol_hit=False unresolved=True |
| chalk-locate-ansi256 | chalk | locate | failed | yes | file_precision=0.0 file_recall=0.0 symbol_hit=False unresolved=True |
| chalk-locate-styles-export | chalk | locate | failed | no | *gold_source=pending* |
| chalk-impact-supportscolor | chalk | impact | success | yes | caller_precision=0.5 caller_recall=1.0 |
| chalk-impact-ansi256 | chalk | impact | success | yes | caller_precision=0.5 caller_recall=1.0 |
| chalk-history-color-level | chalk | history | success | yes | commit_recall=0.667 |
| chalk-rollback-perf-vs-styles | chalk | rollback | failed | no | *gold_source=pending* |
| chalk-compound-truecolor | chalk | compound | failed | no | *gold_source=pending* |
| chalk-locate-readme | chalk | locate | failed | no | *gold_source=pending* |
| chalk-history-deps | chalk | history | success | no | *gold_source=pending* |
| zustand-locate-createstore | zustand | locate | success | yes | file_precision=0.0 file_recall=0.0 symbol_hit=True unresolved=False |
| zustand-locate-shallow | zustand | locate | success | yes | file_precision=0.0 file_recall=0.0 symbol_hit=True unresolved=False |
| zustand-locate-middleware | zustand | locate | success | no | *gold_source=pending* |
| zustand-impact-subscribe | zustand | impact | success | yes | caller_precision=0.167 caller_recall=0.5 |
| zustand-impact-createsource | zustand | impact | success | no | *gold_source=pending* |
| zustand-history-extract-react | zustand | history | success | yes | commit_recall=1.0 |
| zustand-history-v5 | zustand | history | success | yes | commit_recall=0.5 |
| zustand-rollback-v5-vs-docs | zustand | rollback | success | no | *gold_source=pending* |
| zustand-compound-default-export | zustand | compound | success | no | *gold_source=pending* |
| zustand-locate-react-bindings | zustand | locate | success | no | *gold_source=pending* |
| express-locate-send | express | locate | success | yes | file_precision=1.0 file_recall=1.0 symbol_hit=False unresolved=False |
| express-locate-etag | express | locate | success | yes | file_precision=1.0 file_recall=0.5 symbol_hit=False unresolved=False |
| express-impact-etag-gen | express | impact | success | yes | caller_precision=1.0 caller_recall=1.0 |
| express-history-send-encoding | express | history | success | yes | commit_recall=1.0 |
| express-locate-router | express | locate | success | no | *gold_source=pending* |
| express-impact-etag | express | impact | failed | no | *gold_source=pending* |
| express-rollback-send-vs-docs | express | rollback | failed | yes | rollback_precision=0.0 rollback_recall=0.0 preservation_rate=1.0 collateral_damage=0 |

## Summary by task type

| type | n | file_precision | file_recall | symbol_hit | caller_precision | caller_recall | route_precision | route_recall | commit_recall | rollback_precision | rollback_recall | preservation_rate | collateral_damage | policy_action_match | unresolved |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| history | 4 |  |  |  |  |  |  |  | 0.792 |  |  |  |  |  |  |
| impact | 4 |  |  |  | 0.542 | 0.875 |  |  |  |  |  |  |  |  |  |
| locate | 6 | 0.333 | 0.25 | 0.333 |  |  |  |  |  |  |  |  |  |  | 0.333 |
| rollback | 1 |  |  |  |  |  |  |  |  | 0.0 | 0.0 | 1.0 | 0.0 |  |  |

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
- failed skills: `{'resolve_target': 'failed', 'change_unit_analysis': 'failed'}`

### chalk-compound-truecolor (chalk, compound)
- error: ``
- failed skills: `{'resolve_target': 'failed', 'change_unit_analysis': 'failed'}`

### chalk-locate-readme (chalk, locate)
- error: ``
- failed skills: `{'resolve_target': 'failed'}`

### express-impact-etag (express, impact)
- error: ``
- failed skills: `{'impact_analysis': 'failed'}`

### express-rollback-send-vs-docs (express, rollback)
- error: ``
- failed skills: `{'resolve_target': 'failed', 'change_unit_analysis': 'failed'}`


## Execution metrics

- avg latency: 8.374 ms
- avg evidence per task: 17.593
- total llm calls: 0
