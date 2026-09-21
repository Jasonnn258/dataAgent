# dataAgent — 代码结构 + Git 历史 + Context Graph 的定位/影响/回退实验框架

研究问题：**代码结构（AST）+ Git 历史 + Semantica Context Graph，能否提升
自然语言代码定位、变更影响分析与 Git 安全回退能力？**

这是一个**可重复运行的实验框架**（不是产品）。四种模式在同一评测下消融：

| mode | 上下文来源 |
|---|---|
| `lexical` | 仅文本搜索（ripgrep/grep 兼容后端） |
| `structural` | + AST 结构索引（tree-sitter TS/TSX/JS：符号/调用/导入） |
| `structural_git` | + Git 只读分析（log/diff/blame/co-change） |
| `semantica` | + Semantica Context Graph（结构+Git 入图，图查询供上下文） |

## 快速开始

```bash
# 本机已建好 conda 环境 dataagent（python 3.11，依赖全装）
conda activate dataagent

# 从零复现（其他机器）：
pip install -r requirements.txt
# semantica 模式（可选）：
#   pip install --no-deps semantica==0.6.8 numpy scikit-learn
#   # 完整安装会拖 umap 全家桶（600MB+），我们只用 semantica.kg +
#   # semantica.provenance，numpy/sklearn 是 kg/__init__ 的最小硬依赖

# 构造实验用 fixture 仓库（含"标题+登录混合提交"，rollback 实验必需）
python experiments/fixtures/seed_fixture.py

# Task 1 定位：系统标题在哪里改？
python -m src.main --repo experiments/fixtures/fixture_repo --mode lexical \
  --task locate --query "系统标题在哪里修改"

# Task 2 影响分析（semantica 模式输出含 PathFinder 路由链与溯源证据）
python -m src.main --repo experiments/fixtures/fixture_repo --mode semantica \
  --task impact --query "generateWithRetry"

# Task 3 安全回退（只分析，绝不执行 git 操作）
python -m src.main --repo experiments/fixtures/fixture_repo --mode structural_git \
  --task rollback --query "登录逻辑改坏了，回退登录修改但保留标题修改"

# 四模式消融评测（fixture 自带 gold + mindmap 人工待标注任务）
python -m src.eval.runner
# 产物：experiments/results.jsonl（逐条）+ experiments/results.md（汇总表）
```

## 架构

```
query ──▶ TaskRunner (locate | impact | rollback)
             │  按 mode 逐层激活检索器（消融的机制来源）
             ├── lexical      src/search/lexical.py     文本匹配 + 中英查询扩展
             ├── structural   src/structural/           tree-sitter 索引（迭代遍历）
             ├── git          src/git_history/          只读 git CLI（白名单）
             └── graph        src/semgraph/             Semantica Context Graph
             │
             ▼  RetrievalBundle（有界上下文，禁止整库进 prompt）
             ▼  LLM 可选推理（未配置 → 确定性启发式；配置坏 → 报错不降级）
             ▼
          TaskOutput JSON（src/schema.py，四模式同 schema，可自动评测）
```

### Semantica 集成方式（Phase 5，实测 API）

官方 0.6.8 的 `graph_store` 只支持外部图数据库 backend（neo4j/falkordb/
neptune/age），CLI 亦无写入命令；共享开发机上不该起数据库服务。因此本地
路径采用 `semantica.kg` 内存图 + `networkx` 无向投影：

- `KnowledgeGraph(entities, relationships, metadata)` — 图容器（dataclass）
- `GraphBuilder.build({"entities":…, "relationships":…}, extract=False)` — 官方
  支持的 pre-extracted dict 输入
- `PathFinder().find_shortest_path(nx_graph, a, b)` — 多跳关联链（无向投影，
  因为"相关链"需要逆 CALLS 方向穿行）
- `semantica.provenance.ProvenanceManager` — 溯源（`kg.ProvenanceTracker`
  0.6.8 已弃用，用新接口；内存模式不写盘）

节点：Repository/File/Function/Method/Class/Component/APIEndpoint/UIString/
Commit + 内部辅助 Scope/CallName；边：DEFINES/IMPORTS/CALLS/REFERENCES/
CONTAINS/MODIFIES/CO_CHANGED_WITH。关键细节：`CallName → 同名 sym` 必须补
REFERENCES 解析边，否则 scope→callname 是断头路，多跳路径全部失效。

图层的角色定位（用户约定）：Semantica 只负责 Context Graph / 知识组织 /
provenance / evidence 组织，**不替代 AST parser** —— 所有结构事实仍来自
tree-sitter 与 git CLI。

### Change Unit（rollback 的实验重点）

1 commit ≠ 1 change：`src/change_units/units.py` 把 commit 的 diff hunk 按
①触达符号 ②领域信号（auth/title/ui/deps/docs/api/data，中英触发词）
③union-find 聚类（同符号 / 同文件同域 / 跨文件域强度 / **callgraph 绑定**）
拆成语义单元，再按 query 的"问题子句 / 保留子句"分类回退与保留。
fixture 的 c5 用例专门验证 callgraph 绑定：validate.ts 无任何 auth 词面，
lexical 模式必然漏检，structural 模式靠调用图全召回。

## 评测（Phase 6）

- `experiments/tasks.jsonl`：fixture 任务带 by-construction gold；
  mindmap（真实仓库）任务 gold **刻意留空**（`gold_source: "manual"`），
  等人工标注，**不伪造真实仓库 Ground Truth**
- `src/eval/metrics.py`：locate recall/precision@k；impact file 级 P/R/F1；
  rollback P/R + preservation_rate + **collateral_damage_rate**
  （错误建议撤销的有益改动 / 应保留改动数）；system 时延/上下文量/工具调用数
- `src/eval/runner.py`：任务 × 四模式矩阵，错误显式记录不吞

fixture 消融结果（n 小，仅方向性参考）：impact 的 recall lexical 0.5 →
structural+ 1.0；rollback 的 recall lexical 0.75（漏 callgraph 绑定的
validate.ts）→ structural+ 1.0；semantica ≈ structural_git（同决策 + 图证据）。
完整表见 `experiments/results.md`。

## 当前支持范围

- 语言：TypeScript / TSX / JavaScript（第一阶段）
- 任务：locate / impact / rollback（rollback 仅分析建议，不执行任何 git 写操作）
- Phase 0 骨架 ✅ → 1 lexical ✅ → 2 structural ✅ → 3 git ✅ → 4 rollback ✅
  → 5 semantica ✅ → 6 评测消融 ✅
- Phase 9 ✅（Graph Schema v2 / Context Broker / TaskGraphView / 语义特征层 /
  变更时间图 / Evidence-Finding-Conflict / Decision Memory / Policy Gate /
  五 Agent + Orchestrator / 双 Verifier）→ Phase 10 G0-G4 图消融 ✅

## Agent Layer v2（Phase 9，Semantica 设计哲学的重排）

核心原则：**LLM 负责模糊理解，确定性工具负责事实**。Agent 不共享中间思考，
只共享结构化事实（Evidence / Finding / Decision / Policy，全部一等数据对象）。

```
query ──▶ ContextBroker（src/semgraph/context_broker.py，唯一边界）
             │  Agents never touch GraphV2/semantica.kg/CodeIndex/GitAPI
             ▼
          Orchestrator（src/agents/orchestrator.py，只协调不思考）
             ├── RepositoryNavigator      模糊 NL → Feature（LLM 只能选已有名字）
             ├── ChangeIntelligenceAgent  terms → ChangeUnit（分数+时间排序）
             ├── ImpactSliceAgent         有界 TaskGraphView + 波及面
             ├── RollbackPlanner          单元级回退/保留计划 + Policy Gate
             └── Verifiers                Deterministic + Semantic（三档裁决，
                                          禁伪精确 confidence；语义证据永不能
                                          单独把 finding 升为 verified）
```

- **Graph Schema v2**（`src/semgraph/schema_v2.py`）：typed Node/Edge + 五层
  （code/semantic/change/evidence/decision）+ 时间字段（valid_from_commit/
  valid_to_commit/observed_at）；v1 ContextGraph 仍是查询引擎，`from_v1`/
  `sync_back_to_v1` 双向兼容，Phase 0-6 parser 一行未改
- **Change + Temporal Graph**（`src/semgraph/change_graph.py`）：复用
  git_history/change_units，Commit CONTAINS_CHANGE ChangeUnit、ChangeUnit
  MODIFIES File/ChangedSymbol/Feature，**绝不默认 commit == change unit**
- **Conflict Model**：同主题不同断言 → CONTRADICTS 边 + 注册表，双方都保留；
  冲突未解决不得 verified（严格守卫：链式冲突也算未解决）
- **Policy Gate**（`src/semgraph/policy.py`）：结构化规则带版本，
  BLOCK > HUMAN_REVIEW > PASS；rollback/keep 共享符号、公共 API、DB 迁移 →
  HUMAN_REVIEW；无证据 finding / 无效图路径 / 未解决测试失败 → BLOCK
- **Decision Memory**：只存审计向 reason_summary（500 字截断 + 告警），
  禁存隐藏 CoT；`get_precedents` 按 category/target/查询词找先例

### 验收演示（六步，全部确定性，无 LLM 也能跑）

「昨天登录逻辑改坏了，帮我找出问题修改，准备回退，但保留同 commit 中已经
改好的系统标题」：

```bash
/vla_test/yjx/miniconda3/envs/dataagent/bin/python - <<'EOF'
import sys; sys.path.insert(0, '.')
from pathlib import Path
from src.schema import ToolRecorder
from src.semgraph.context_broker import ContextBroker
from src.semgraph.change_graph import build_change_graph
from src.agents import Orchestrator
b = ContextBroker(Path("experiments/fixtures/fixture_repo"), ToolRecorder())
build_change_graph(b)
print(Orchestrator(b).run(
    "登录逻辑改坏了，帮我找出问题修改，准备回退",
    keep_hint="保留同 commit 中已经改好的系统标题").dump())
EOF
```

Navigator 识别 Feature AuthLogin/SystemBranding → ChangeIntelligence 定位
混合提交 bbdc659f 的 U2[auth]（问题）/U1[title]（保留）→ ImpactSlice 验证
U2 波及 `/api/auth/login`（有界 23 节点视图）→ RollbackPlanner 出单元级计划
（共享符号/导入耦合均无）→ Verifiers 裁决（单元匹配全部 SUPPORTED+verified，
导航类 finding 诚实标 UNSUPPORTED/PARTIALLY_SUPPORTED）→ Policy Gate 因触
公共 API 路由给 HUMAN_REVIEW。**全程零 git 写操作，回退绝不执行。**

## Phase 10：G0-G4 图消融

`experiments/g_ablation.py`，同一验收场景，逐层开图：

```
level | nav  | problem | keep | policy       | verdicts | verified | view_nodes
G0    | fail | 0       | 0    | -            | 0        | 0        | 0
G1    | ok   | 0       | 0    | -            | 0        | 0        | 0
G2    | ok   | 1       | 1    | ungated      | 0        | 0        | 0
G3    | ok   | 1       | 1    | ungated      | 0        | 0        | 23
G4    | ok   | 1       | 1    | HUMAN_REVIEW | 9        | 5        | 23
```

- Q1 语义层是模糊自然语言入口的必要条件（G0 连目标都解析不出）
- Q2 变更层是单元级回退的必要条件（G1 找到 Feature 但没有单元）
- Q3 TaskView 把上下文钉在有界切片上（23/165 节点，扩展全程留痕）
- Q4 Evidence/Decision/Policy 层让结论可验证可审计（9 裁决 5 verified +
  门控 HUMAN_REVIEW；G3 及以下无裁决无门控）

## 已知限制

- rollback 的 collateral_damage_risk 是启发式打分（共享文件/调用耦合/无匹配），
  非概率语义；operations_hint 需人工执行
- locate 的中文查询扩展依赖 `src/search/zh_en_terms.json` 词典，覆盖有限
- mindmap 无 `.git`，git 相关模式在其上显式拒绝（评测中记录为 error 行）
- fixture gold 是 by-construction（我们自己写的代码），只能验证机制方向，
  不能替代真实仓库人工标注
- semantica 图为进程内缓存，未持久化到图数据库；>10 万边规模的扩展性未测
- LLM rerank 默认关闭（--no-llm / 未配置环境变量），当前结果全部为确定性启发式

## 下一阶段计划

- 人工补充 `experiments/tasks.jsonl` 的 gold（mindmap 真实任务）
- Change Unit 拆分对"同文件多域混合 hunk"的拆分粒度评估
- Cluster Mode（复杂任务的子任务分解，Target Mode 已验证）
- LLM rerank / semantic mapping 接入后的 G4 对比（LLM 是否放大结构层优势）
- 冲突主题模型仍偏粗（共享标识符才算冲突）——积累真实误报/漏报再调

## 实验素材

`mindmap/` 是真实目标仓库（SUN思维太阳教学设计系统，Next.js 16 + TS）。
它本身无 `.git`；git 相关实验使用 `experiments/fixtures/` 下的可重建 fixture。
