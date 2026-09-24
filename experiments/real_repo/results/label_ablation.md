# ChangeUnit Label Ablation (Phase 12C)

## 标签质量（人工 gold 47 单元）

| units | c0_label_acc | c1_label_acc | c1_intent_acc | c1_feature_map | feature 覆盖 |
|---|---|---|---|---|---|
| 47 | 0.021 | 0.596 | 0.787 | 1.0 | 0.149 |

> C0 无 intent 输出（n/a）；c1_feature_map = candidate_features 命中单元自身符号∩feature 词表（LLM 可见 summary 里的符号 —— 这是管线 floor-check，不是难例 NLU 测试）

## 回退计划质量（C1 = 图验证 feature 重打分 +2.0，只加不减）

| arm | tasks | rollback_prec | rollback_rec | preservation | collateral | llm_calls | avg_ms |
|---|---|---|---|---|---|---|---|
| c0 | 4 | 0.034 | 0.394 | 0.333 | 1.333 | 0 | 1078.1 |
| c1 | 4 | 0.005 | 0.25 | 0.667 | 0.333 | 8 | 77679.9 |

## 逐任务

| task | arm | status | rollback_files 数 | scores |
|---|---|---|---|---|
| chalk-rollback-perf-vs-styles | c0 | failed | 0 | prec=0.0 rec=0.0 pres=1.0 |
| chalk-compound-truecolor | c0 | failed | 0 | prec=0.0 rec=0.0 pres=None |
| chalk-rollback-perf-vs-styles | c1 | success | 0 | prec=0.0 rec=0.0 pres=1.0 |
| chalk-compound-truecolor | c1 | success | 0 | prec=0.0 rec=0.0 pres=None |
| zustand-rollback-v5-vs-docs | c0 | success | 128 | prec=0.117 rec=0.577 pres=0.0 |
| zustand-rollback-v5-vs-docs | c1 | success | 1 | prec=0.0 rec=0.0 pres=1.0 |
| express-rollback-send-vs-docs | c0 | failed | 102 | prec=0.02 rec=1.0 pres=0.0 |
| express-rollback-send-vs-docs | c1 | failed | 102 | prec=0.02 rec=1.0 pres=0.0 |

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
| e247220e-U19 | other/release | mixed | deps | release | None |
| e247220e-U20 | other/release | mixed | deps | release | None |
| e247220e-U21 | other/release | mixed | deps | release | None |
| e247220e-U22 | other/release | mixed | deps | release | None |
| e247220e-U23 | other/release | mixed | deps | release | None |
| e247220e-U24 | other/release | mixed | api | release | None |
| e247220e-U25 | other/release | mixed | api | release | None |
| e247220e-U26 | other/release | mixed | api | release | True |
| e247220e-U27 | other/release | mixed | api | release | None |
| e247220e-U28 | other/release | mixed | api | release | True |
| e247220e-U29 | other/release | mixed | api | release | True |
| e247220e-U30 | other/release | mixed | api | release | True |
| e247220e-U31 | other/release | mixed | api | release | True |
| e247220e-U32 | other/release | mixed | api | release | None |
| e247220e-U33 | other/release | mixed | api | release | True |
| e247220e-U35 | other/release | mixed | other | test | None |
| e247220e-U36 | other/release | mixed | other | test | None |
| e247220e-U37 | other/release | mixed | other | test | None |
| e247220e-U38 | other/release | mixed | other | test | None |
| e247220e-U39 | other/release | mixed | other | test | None |
| e247220e-U40 | other/release | mixed | other | test | None |
| e247220e-U41 | other/release | mixed | other | test | None |
| e247220e-U42 | other/release | mixed | other | test | None |
| e247220e-U43 | other/release | mixed | other | test | None |
| e247220e-U44 | other/release | mixed | other | test | None |
| 18e5985b-U1 | docs/docs | title | title | docs | None |
| 18e5985b-U2 | other/bugfix | mixed | api | bugfix | None |
| 18e5985b-U3 | other/test | mixed | api | test | None |

## 隔离臂补记（c1r：只有标签重打分带 LLM，resolve 保持确定性）

完整数据在 `label_ablation_c1r.{jsonl,md}`。c1 臂 resolve 侧也带 LLM，
与 c0 的差是「标签重打分 + 12B resolve 救援」的复合效应 —— 加跑 c1r
把标签净效应单独剥出来：

| arm | rollback_prec | rollback_rec | preservation | collateral |
|---|---|---|---|---|
| c0  | 0.034 | 0.394 | 0.333 | 1.333 |
| c1r | 0.034 | 0.394 | 0.333 | 1.333 |
| c1  | 0.005 | 0.25  | 0.667 | 0.333 |

**c1r 与 c0 四任务逐项全等**（含 zustand 128 个 rollback 文件、keep 空）
—— +2.0 graph-verified feature 重打分对计划零净效应。c1 的全部表面
差异来自 resolve 侧 LLM 救活 keep 侧（zustand keep 0→2 文件 →
rollback 128→1；chalk-rollback keep 0→12），即 12B 的 d1 效应。

## 复现性（同模型同温度两轮 gold 标定）

31/47 单元标签两轮一致（GLM-5.3-Flash vLLM 批内推理非确定性 +
reasoning 路径漂移）；label_acc 0.596 → 0.511，intent_acc 0.787 →
0.766。headline 数字有 ~±0.09 的轮间噪声带。

## 结论

1. **标签质量面：方向成立但天花板低** —— C0 目录触发词在跨切单元上
   塌成 mixed（44/47），LLM 标签 0.51-0.60 / intent 0.77-0.79；
   两轮仅 31/47 复现。feature floor-check 可测处 7/7 全过。
2. **回退面：NOT SUPPORTED** —— 标签重打分零净效应；c1 的改善是
   12B resolve 效应的复利，不是标签的功劳。
3. **成本面** —— 47 单元 6 批 ≈ 12.6k tokens/102s；每任务 40-85s
   标签延迟买不来计划收益。
