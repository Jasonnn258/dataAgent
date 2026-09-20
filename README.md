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
pip install -r requirements.txt

# 构造实验用 fixture 仓库（含"标题+登录混合提交"，rollback 实验必需）
python experiments/fixtures/seed_fixture.py

# Task 1 定位：系统标题在哪里改？
python -m src.main --repo experiments/fixtures/fixture_repo --mode lexical \
  --task locate --query "系统标题在哪里修改"

# Task 2 影响分析
python -m src.main --repo experiments/fixtures/fixture_repo --mode structural \
  --task impact --query "generateWithRetry"

# Task 3 安全回退（只分析，绝不执行 git 操作）
python -m src.main --repo experiments/fixtures/fixture_repo --mode structural_git \
  --task rollback --query "登录逻辑改坏了，回退登录修改但保留标题修改"

# 四模式消融评测
python -m src.eval.runner --repo experiments/fixtures/fixture_repo \
  --tasks experiments/tasks.jsonl --out outputs/eval.json
```

## 架构

```
query ──▶ TaskRunner (locate | impact | rollback)
             │  按 mode 逐层激活检索器（消融的机制来源）
             ├── lexical      src/search/lexical.py     文本匹配
             ├── structural   src/structural/           tree-sitter 索引
             ├── git          src/git_history/          只读 git CLI
             └── graph        src/semgraph/             Semantica Context Graph
             │
             ▼  RetrievalBundle（有界上下文，禁止整库进 prompt）
             ▼  LLM 可选推理（未配置 → 确定性启发式；配置坏 → 报错不降级）
             ▼
          TaskOutput JSON（src/schema.py，四模式同 schema，可自动评测）
```

- 统一数据结构：`TaskContext / Target / Evidence / AnalysisResult`（`src/schema.py`），
  为后续 Agent Cluster（Orchestrator / CodeLocatorAgent / GitHistoryAgent /
  ImpactAgent / RollbackAgent）预留接口。
- 所有结论必须带 `Evidence`（file:line / commit / graph 查询来源）。
- LLM 走 OpenAI-compatible endpoint（`LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`），
  不配置时结构分析与 Git 分析照常运行。

## 当前支持范围

- 语言：TypeScript / TSX / JavaScript（第一阶段）
- 任务：locate / impact / rollback（rollback 仅分析建议，不执行任何 git 写操作）
- 开发顺序：Phase 0 骨架 ✅ → 1 lexical → 2 structural → 3 git → 4 rollback → 5 semantica → 6 评测消融

## 已知限制

（随实现更新）

## 下一阶段计划

- 人工补充 `experiments/tasks.jsonl` 的 gold（当前只有模板与 fixture 自带 gold）
- Multi-Agent（Target Mode 验证成立后再做）

## 实验素材

`mindmap/` 是真实目标仓库（SUN思维太阳教学设计系统，Next.js 16 + TS）。
它本身无 `.git`；git 相关实验使用 `experiments/fixtures/` 下的可重建 fixture。
