# ChangeUnit Label Ablation (Phase 12C)

## 标签质量（人工 gold 47 单元）

| units | c0_label_acc | c1_label_acc | c1_intent_acc | c1_feature_map | feature 覆盖 |
|---|---|---|---|---|---|
| 47 | 0.021 | 0.511 | 0.766 | 1.0 | 0.149 |

> C0 无 intent 输出（n/a）；c1_feature_map = candidate_features 命中单元自身符号∩feature 词表（LLM 可见 summary 里的符号 —— 这是管线 floor-check，不是难例 NLU 测试）

## 回退计划质量（C1 = 图验证 feature 重打分 +2.0，只加不减）

| arm | tasks | rollback_prec | rollback_rec | preservation | collateral | llm_calls | avg_ms |
|---|---|---|---|---|---|---|---|
| c1r | 4 | 0.034 | 0.394 | 0.333 | 1.333 | 0 | 22275.4 |

## 逐任务

| task | arm | status | rollback_files 数 | scores |
|---|---|---|---|---|
| chalk-rollback-perf-vs-styles | c1r | failed | 0 | prec=0.0 rec=0.0 pres=1.0 |
| chalk-compound-truecolor | c1r | failed | 0 | prec=0.0 rec=0.0 pres=None |
| zustand-rollback-v5-vs-docs | c1r | success | 128 | prec=0.117 rec=0.577 pres=0.0 |
| express-rollback-send-vs-docs | c1r | failed | 102 | prec=0.02 rec=1.0 pres=0.0 |

## gold 单元逐条（C0 vs C1 标签）

| unit | gold | c0 | c1 label | c1 intent | feat hit |
|---|---|---|---|---|---|
| 5729845f-U1 | ui/perf | ui | ui | perf | True |
| e247220e-U1 | other/release | mixed | other | release | None |
| e247220e-U2 | other/release | mixed | other | release | None |
| e247220e-U3 | other/release | deps | deps | release | None |
| e247220e-U4 | other/release | mixed | other | release | None |
| e247220e-U5 | other/release | mixed | other | release | None |
| e247220e-U6 | other/release | mixed | other | release | None |
| e247220e-U7 | other/release | mixed | other | release | None |
| e247220e-U8 | other/release | mixed | other | release | None |
| e247220e-U9 | other/release | mixed | other | release | None |
| e247220e-U10 | other/release | mixed | other | release | None |
| e247220e-U11 | deps/release | mixed | deps | release | None |
| e247220e-U12 | deps/release | mixed | deps | release | None |
| e247220e-U13 | deps/release | mixed | deps | release | None |
| e247220e-U14 | deps/release | mixed | deps | release | None |
| e247220e-U15 | deps/release | mixed | deps | release | None |
| e247220e-U16 | deps/release | mixed | deps | release | None |
| e247220e-U17 | deps/release | mixed | deps | release | None |
| e247220e-U18 | deps/release | mixed | deps | release | None |
| e247220e-U19 | other/release | mixed | other | release | None |
| e247220e-U20 | other/release | mixed | other | release | None |
| e247220e-U21 | other/release | mixed | other | release | None |
| e247220e-U22 | other/release | mixed | other | release | None |
| e247220e-U23 | other/release | mixed | other | release | None |
| e247220e-U24 | other/release | mixed | api | refactor | None |
| e247220e-U25 | other/release | mixed | api | release | None |
| e247220e-U26 | other/release | mixed | api | release | True |
| e247220e-U27 | other/release | mixed | api | release | None |
| e247220e-U28 | other/release | mixed | api | release | True |
| e247220e-U29 | other/release | mixed | api | release | True |
| e247220e-U30 | other/release | mixed | api | release | True |
| e247220e-U31 | other/release | mixed | api | release | True |
| e247220e-U32 | other/release | mixed | api | release | None |
| e247220e-U33 | other/release | mixed | api | release | True |
| e247220e-U35 | other/release | mixed | api | test | None |
| e247220e-U36 | other/release | mixed | api | test | None |
| e247220e-U37 | other/release | mixed | api | test | None |
| e247220e-U38 | other/release | mixed | api | test | None |
| e247220e-U39 | other/release | mixed | api | test | None |
| e247220e-U40 | other/release | mixed | api | test | None |
| e247220e-U41 | other/release | mixed | api | test | None |
| e247220e-U42 | other/release | mixed | api | test | None |
| e247220e-U43 | other/release | mixed | api | test | None |
| e247220e-U44 | other/release | mixed | api | test | None |
| 18e5985b-U1 | docs/docs | title | title | docs | None |
| 18e5985b-U2 | other/bugfix | mixed | api | bugfix | None |
| 18e5985b-U3 | other/test | mixed | other | test | None |
