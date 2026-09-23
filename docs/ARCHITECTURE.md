# dataAgent 整体架构与函数手册

> 更新：2026-09-23（Phase 12A + 13 完结后）。本文覆盖 `src/` 主要模块与
> `experiments/` 实验入口，逐函数给出一句话作用。代码是唯一真源，本文与代码
> 不一致时以代码为准。

---

## 1. 一句话总览

dataAgent 是一个**默认只读的代码仓库维护框架**：输入"X 改坏了，回退它但保留 Y"这类
自然语言请求，输出带完整证据链（evidence → finding → verdict → decision → policy）
的定位 / 影响面 / 回退计划。Phase 13 起分析计划可以在 **git worktree 沙箱**里真实
执行（贴反向 patch → 验证 → 五面终审 → 双策略门），沙箱事实经**人类显式批准 +
三把钥匙**后才允许晋升到真实仓库（默认关闭，落地不 commit 不 push）。沙箱之外，
系统对仓库零写操作。

体系分三轨：

| 轨道 | 位置 | 说明 |
|---|---|---|
| 四模式消融管线（Phase 1-5） | `src/main.py` → `src/tasks/` | lexical / structural / structural_git / semantica 四级上下文逐层叠加，回答"加一层证据值多少分" |
| Agent/Skill/Broker 体系（Phase 9-13） | `src/agents/` `src/skills/` `src/semgraph/` `src/services/` `src/maintenance/` | 六个分析 Agent + MaintenanceExecutorAgent 委托 Skill，经 ContextBroker 一扇门消费确定性服务；执行环只写沙箱 |
| 真实 repo 验证（Phase 12A+） | `experiments/real_repo/` | chalk/zustand/express 三 repo 基准：分析面 + 沙箱执行面 + oracle 对照（gold 计划直喂执行环） |

---

## 2. 顶层架构图（Phase 9-11 主体系）

```
┌────────────────────────────────────────────────────────────────────┐
│ 入口层                                                              │
│  src/main.py（四模式 CLI）                                          │
│  experiments/g_ablation.py（G0-G4 图层消融）                         │
│  experiments/skill_eval.py（逐 skill 评测）                          │
│  experiments/real_repo/（Phase 12A 真实 repo benchmark）             │
└───────────────┬────────────────────────────────────────────────────┘
                │ run(query, keep_hint)
┌───────────────▼────────────────────────────────────────────────────┐
│ Agent 层（6 个负责人，src/agents/）        「做决策、定归属」          │
│  Orchestrator（唯一协调者，自己不带逻辑）                              │
│   ├─ RepositoryNavigator    模糊 query → feature 目标（唯一可碰 LLM） │
│   ├─ ChangeIntelligenceAgent 词表 → ChangeUnit 匹配                  │
│   ├─ ImpactSliceAgent       目标 → 有界波及面（callers/routes）       │
│   ├─ RollbackPlanner        单元仲裁 + 回退计划 + policy gate         │
│   └─ Deterministic/SemanticVerifier  finding 三档裁决                │
│  每个 Agent 在 ScopedContext 下跑（读什么/委托哪些 skill/产出什么全记账）│
└───────────────┬────────────────────────────────────────────────────┘
                │ SkillRuntime.run(name, ctx)（白名单注入 llm）
┌───────────────▼────────────────────────────────────────────────────┐
│ Skill 层（8 个可复用能力，src/skills/）   「可被枚举/测试/评测的单元」  │
│  resolve_target · build_task_view · impact_analysis                 │
│  change_unit_analysis · coupling_analysis · safe_rollback           │
│  evidence_verification · policy_check                               │
│  SkillSpec=数据契约  CapabilityGuard=运行期能力执法（fail fast）       │
└───────────────┬────────────────────────────────────────────────────┘
                │ 只经这一扇门（CAPABILITIES 方法→能力映射）
┌───────────────▼────────────────────────────────────────────────────┐
│ ContextBroker（src/semgraph/context_broker.py）                      │
│  图层开关 layers={code,semantic,change,evidence,decision,taskview}    │
│  （G0-G4 消融旋钮）；evidence 铸造接缝；task view 预算点               │
└──────┬─────────────────────────────┬───────────────────────────────┘
       │                             │
┌──────▼──────────────────┐   ┌──────▼───────────────────────────────┐
│ 确定性服务 ×8             │   │ LLM 语义适配器（唯一 LLM 合法入口）     │
│ src/services/            │   │ src/llm/semantic_adapter.py           │
│ resolution  目标解析      │   │  map_features（唯一接线：resolve_target）│
│ graph_query 路径查询      │   │  label_change_unit / verify_semantic  │
│ task_view   有界视图      │   │  （预留接口，12B/12C 接线）             │
│ change      变更事实      │   │ prompts/：公共不变量 + 三个 skill prompt │
│ evidence    证据注册表    │   │ 「LLM 只能挑候选，不能铸造确定性事实」     │
│ decision    决策记忆      │   └──────────────────────────────────────┘
│ policy      规则门        │
│ semantic    语义门面      │
│ tooling: call_tool 统一包装 ToolResult（ok/degraded/failed 三态）      │
└──────┬──────────────────┘
       │
┌──────▼────────────────────────────────────────────────────────────┐
│ 物理工具层（确定性事实的唯一铸造者）                                   │
│  structural/   tree-sitter TS/TSX/JS/JSX 解析 → 符号/导入/调用/JSX/UI  │
│  git_history/  git 只读白名单（log/show/diff/blame）                  │
│  change_units/ hunk 特征化 + union-find 聚类 → ChangeUnit             │
│  search/       词法搜索（rg 优先）+ 中英词元抽取                        │
│  semantica.kg  v1 图容器（KnowledgeGraph/GraphBuilder/PathFinder）    │
└──────┬─────────────────────────────────────────────────────────────┘
       │ 事实沉淀在：
┌──────▼────────────────────────────────────────────────────────────┐
│ GraphV2 六层图（code/semantic/change/evidence/decision + taskview）  │
│ Evidence / Finding / Conflict / Decision / Verdict / PolicyResult    │
│ ExecutionRecorder 调用树审计（EventLayer×span，不存载荷/CoT）          │
└────────────────────────────────────────────────────────────────────┘
```

### 一次 rollback 分析的数据流

```
query="登录改坏了，回退但保留同 commit 的系统标题"
  │
  ├─ Navigator.find_target ──skill──> resolve_target
  │     └─ broker.map_semantic_candidates → SemanticService → SemanticMapper
  │          词面/别名匹配（+可选 LLM 只挑已有 feature 名）→ 候选 + 词表 terms
  ├─ (keep_hint 同样再走一遍导航 → keep 侧词表)
  │
  ├─ ChangeIntel.find_units ──skill──> change_unit_analysis
  │     └─ broker.find_change_units → 图里 cu: 节点按 label/symbol/UI/file 打分
  │          → problem_matches / keep_matches（每条带 CHANGE_UNIT evidence + finding）
  │
  ├─ Planner.arbitrate（真源在 safe_rollback.arbitrate）
  │     同 hit 两套词表的单元归高分者；平局归问题侧；keep 命中钉住 commit
  │
  ├─ Impact.slice_impact ──skill──> build_task_view + impact_analysis
  │     └─ broker.get_target_context → callers/routes/files；视图带审计生长
  │
  ├─ Planner.plan ──skill──> safe_rollback
  │     └─ 装配回退/保留清单 → import_couplings → run_policy_gate
  │          → PASS/HUMAN_REVIEW/BLOCK + 逐单元 Decision（绝不执行 git）
  │
  └─ Verifier.verify ──skill──> evidence_verification
        └─ 确定性证据齐 → SUPPORTED（升 verified）；语义证据单独 → PARTIALLY
```

---

## 3. 模块地图

| 目录 | 层 | 职责 |
|---|---|---|
| `src/schema.py` | 契约 | 统一输出模型（四模式可比）+ ToolResult 三态 |
| `src/errors.py` | 契约 | 错误层级（永不静默吞） |
| `src/config.py` | 契约 | 环境配置 + 维护策略 yaml（版本化权重） |
| `src/execution.py` | 审计 | ExecutionRecorder 调用树事件 |
| `src/search/` | 物理工具 | 词法搜索与词元抽取 |
| `src/structural/` | 物理工具 | tree-sitter AST 解析与仓库级索引 |
| `src/git_history/` | 物理工具 | git 只读访问与历史证据 |
| `src/change_units/` | 物理工具 | commit → 语义 ChangeUnit 聚类 |
| `src/semgraph/` | 图 | v1 ContextGraph、GraphV2、broker、语义层、task view、policy 规则 |
| `src/services/` | 服务 | 八个确定性服务（组合逻辑真源） |
| `src/skills/` | 能力 | SkillSpec/SkillRuntime/守卫 + 八个 skill |
| `src/agents/` | 负责 | 六个角色 Agent + Orchestrator + ScopedContext |
| `src/llm/` | 语义 | LLMClient、SemanticReasoningAdapter、prompts |
| `src/tasks/` `src/eval/` | 旧轨 | Phase 1-5 四模式 runner 与消融评测 |
| `experiments/` | 实验 | G 消融、skill 评测、真实 repo benchmark（12A） |
| `tests/` | 测试 | 221 个测试（phase0-11 + 架构守卫 + 策略配置） |

---

## 4. 函数手册

### 4.1 src/schema.py — 统一输出契约

| 类/函数 | 作用 |
|---|---|
| `Evidence` | 一条可验证事实（provenance 单元）：kind/source/detail/snippet |
| `Target` | 任务解析出的主体（符号/文件/commit…），自带 evidence 列表 |
| `SystemMetrics` | 跨模式成本可比：延迟/上下文量/工具调用数/llm_used |
| `ToolRecorder` | 记录每次检索/解析/git 调用，绝不静默 |
| ┈ `tool(name)` | 追加一次工具调用名 |
| ┈ `context(chars, items)` | 累计取回上下文量 |
| ┈ `warn(msg)` | 记警告（进输出 JSON 的 system.warnings） |
| ┈ `metrics(mode, llm_used)` | 汇总成 SystemMetrics |
| `ToolResult` | 物理工具统一返回（ok/degraded/failed 三态，不放大载荷） |
| ┈ `success/degraded_ok/failure` | 三态构造器 |
| ┈ `unwrap()` | 成功取值；失败就地大声报错（绝不静默成 None） |
| `LocatedCandidate` / `LocateResult` | locate 任务的候选与结果模型 |
| `ImpactSide` / `ImpactResult` | impact 任务的受影响实体与结果模型 |
| `HunkRef` / `ChangeUnit` / `RollbackResult` | rollback 任务的 hunk/语义单元/结果模型 |
| `TaskOutput` | 顶层 JSON 信封（schema_version/task/mode/result/system） |
| ┈ `write_json(path)` | 落盘统一 JSON |
| `truncate_candidates(res, top_k)` | 候选截断到上限 |

### 4.2 src/errors.py

| 类 | 作用 |
|---|---|
| `DataAgentError` | 全框架错误基类 |
| `SearchError` / `ParseError` / `GitError` / `GraphError` / `LLMError` | 各后端失败；LLM 未配置不算错（启发式模式），配置了但坏才算 |

### 4.3 src/config.py

| 函数/类 | 作用 |
|---|---|
| `LLMConfig` | 环境变量驱动的可选 LLM 配置（available 属性） |
| `Settings` | CLI 运行配置（repo/mode/task/top_k…） |
| ┈ `validate()` | 模式/任务/repo/.git 前置校验 |
| ┈ `llm_enabled()` | 是否真用 LLM（--no-llm 可强关） |
| `_parse_simple_yaml(text)` | PyYAML 缺席时的极简 yaml 解析退路 |
| `_scalar(val)` | 字符串标量转 bool/int/float/str |
| `_deep_merge(base, override)` | 策略配置深度合并 |
| `load_maintenance_policy(path)` | 每次真读磁盘的策略文件（消融用） |
| `maintenance_policy()` | 当前生效策略（进程内缓存） |
| `set_maintenance_policy(cfg)` | A/B 消融钩子（None 恢复文件加载） |
| `policy_version()` | 当前策略版本（记入 Decision 供对账） |

### 4.4 src/execution.py — ExecutionRecorder 调用树

| 类/函数 | 作用 |
|---|---|
| `EventLayer` | 七层事件枚举（tool/service/broker/skill/agent/llm/policy） |
| `ExecutionEvent` | 一条审计事件（trace/parent/layer/actor/status/耗时/evidence 关联） |
| `_classify_tool_name(name)` | 旧 rec.tool 轨迹名自动归类到层 |
| `ExecutionRecorder` | ToolRecorder 的结构化超集（同一调用面 + 一棵执行树） |
| ┈ `tool(name)` | 兼容面：平铺轨迹照旧，同时发一条事件 |
| ┈ `warn(msg)` | 警告挂到当前 span（有界：每事件至多 8 条） |
| ┈ `_emit(...)` | 追加事件并挂 parent 链 |
| ┈ `span(layer, actor, action)` | 带耗时/父子关系的执行区间；异常标 failed 后上抛 |
| ┈ `events_of(layer)` | 按层过滤事件 |
| ┈ `tree()` | 按 parent 链重建嵌套调用树 |
| ┈ `summary()` | 有界摘要（各层计数 + 失败数） |

### 4.5 src/search/ — 词法层

**keywords.py（中英词元抽取，全模式共享）**

| 函数/类 | 作用 |
|---|---|
| `QueryTerms` | 词元容器（CJK 段/子词/英文词/双语扩展） |
| ┈ `all_search_terms()` | 实际检索的 (term, origin) 全集 |
| ┈ `summary()` | 人类可读摘要 |
| `_split_by_function_words(run)` | 按虚词切 CJK 连续段 |
| `extract_terms(query)` | 主入口：query → QueryTerms（确定性） |
| `_camel_parts(word)` | camelCase 拆子词（保留原词） |

**lexical.py（mode=lexical 的唯一上下文源）**

| 函数/类 | 作用 |
|---|---|
| `extension_weight(path)` | 文件类型先验权重（代码满分，文档降权） |
| `Match` | 一次命中（file/line/col/term/origin/行文本） |
| `LexicalSearcher` | rg 优先、纯 Python 退路的可移植搜索器 |
| ┈ `iter_files()` | 遍历仓库文件（忽略 node_modules 等） |
| ┈ `search(terms)` | 全部词元搜索 → Match 列表 |
| ┈ `_search_rg` / `_search_python` | rg / 纯 Python 两条同构实现 |
| `_has_cjk(s)` | 是否含 CJK 字符 |
| `_first_matching_term(line, terms)` | 行内第一个命中的词 |
| `_origin_of(term, terms)` | 词的来源标签（cjk:/expand:/en:） |
| `classify_hit(line_text, col, term)` | 命中分类（字符串/标识符/注释…）+ 权重 |
| `filename_bonus(rel_path, terms)` | 文件名命中加分 |

### 4.6 src/structural/ — AST 结构层

**parser.py（tree-sitter 单文件解析）**

| 函数/类 | 作用 |
|---|---|
| `get_parser(suffix)` | 按后缀取 TS/TSX/JS/JSX parser |
| `Symbol` / `ImportDecl` / `CallEdge` / `JSXRef` / `UIString` / `FileParse` | 符号/导入/调用边/JSX 引用/UI 文案/单文件解析结果的数据形态 |
| ┈ `Symbol.qualified()` | file::name 限定名（图 sym: id 同构） |
| `parse_file(abs_path, rel_path)` | 解析一个文件 → FileParse（容错，ERROR 节点计数） |
| `_walk(root, src, fp, rel)` | AST 遍历主循环：抽符号/导入/调用/JSX/UI |
| `_push_children(node, …)` | 显式栈遍历的子节点压栈（维护 func_stack） |
| `_parse_import(node, src, rel)` | 抽一条 import 声明 |
| `_qualify` / `_module_scope` | 限定名/模块作用域计算 |
| `_first_line` / `_text` / `_descendants` / `_has_jsx` | 节点文本/行号/后代/JSX 判断小工具 |
| `sps(named_imports_node)` | named import 子句解析 |

**index.py（仓库级索引）**

| 函数/类 | 作用 |
|---|---|
| `APIEndpoint` / `ResolvedImport` / `CodeIndex` | 端点/已解析导入/索引容器 |
| ┈ `build()` | 汇总全仓库 FileParse → 符号表+导入图+调用图+路由 |
| ┈ `_resolve_imports()` / `_resolve_specifier(...)` | 文件级导入解析（含 @/ 别名） |
| ┈ `_extract_routes()` | Next.js App Router 约定（route.ts/page.tsx/layout） |
| ┈ `find_symbols(name)` | 按名找符号 |
| ┈ `symbol_at(file, line)` | 行号反查所属符号 |
| ┈ `callers_of(sym)` / `callees_of(sym)` | 调用图正反向查询 |
| ┈ `jsx_callers_of(sym)` | JSX 里引用该组件的位置 |
| ┈ `importers_of_file(file)` | 谁导入此文件 |
| ┈ `indirect_callers(sym, depth)` | 2 跳间接调用者 |
| ┈ `api_routes_reaching(sym)` | 触达该符号的 API 路由 |
| ┈ `tests_referencing(sym)` | 引用该符号的测试文件 |
| ┈ `search_ui_strings(substr)` | UI 文案子串搜索 |
| `_name_imported_into(...)` | 某名字是否被导入进某文件 |

**enrich.py（结构层加成）**

| 函数 | 作用 |
|---|---|
| `build_index(repo, rec)` | 构建（或复用）仓库 CodeIndex |
| `enrich_locate_candidates(repo, cands, qt, rec)` | 给词法候选补符号身份/定义候选/Next.js 布局约定证据 |

### 4.7 src/git_history/ — git 只读层

**api.py（白名单只读）**

| 函数/类 | 作用 |
|---|---|
| `Commit` / `Hunk` / `CommitDiff` | 提交/hunk/diff 数据形态 |
| `GitAPI` | git CLI 只读封装（白名单外的命令在执行前就抛 GitError） |
| ┈ `_run(args)` | 跑一条白名单 git 命令 |
| ┈ `is_repo()` | 是否 git 仓库 |
| ┈ `log(max_count, file, ...)` | 提交列表 |
| ┈ `head()` / `resolve(ref)` | HEAD / 引理解析 |
| ┈ `show(ref)` / `diff(ref)` | 提交详情 / 父差分（解析成 hunk） |
| ┈ `blame(file, lines)` | 行级 blame |
| ┈ `file_history(file)` / `commits_touching(file)` | 文件历史 |
| ┈ `co_change_pairs(max_count)` | 文件共变对 + 支持计数 |
| `_has_parent(api, sha)` | 是否有父提交（root commit 保护） |
| `_parse_unified_diff(raw)` | unified diff 文本 → hunk 结构 |

**enrich.py（git 历史证据）**

| 函数 | 作用 |
|---|---|
| `_api(repo, rec)` | 懒构造 GitAPI（无 .git 返回 None） |
| `enrich_locate_with_git(...)` | 提交信息投票 + churn/recency 加分 |
| `_is_code(path)` | 是否代码文件 |
| `enrich_impact_with_git(...)` | 共变文件 + 定义行 blame 证据 |

### 4.8 src/change_units/units.py — 语义单元聚类

| 函数/类 | 作用 |
|---|---|
| `HunkInfo` | 单个 hunk 的特征（符号/领域信号/UI 文案） |
| ┈ `domain_strengths()` / `domains()` | 领域信号强度 / 领域集合 |
| `characterize_hunks(diff, idx, rec)` | commit diff → 逐 hunk 特征化 |
| `cluster_into_units(sha, infos, idx, rec)` | union-find 聚类 → ChangeUnit 列表 |
| `_UnionFind` | 并查集（find/union） |
| `_domain_strength(shared, ia, ib)` | 两 hunk 的领域耦合强度 |
| `_callgraph_bind(ia, ib, idx, call_syms)` | 调用图绑定：一 hunk 在另一 hunk 所改符号的 caller 里 |
| `_resolve(idx, qualified)` | 限定名解析回 Symbol |
| `_unit_summary(...)` | 单元人类可读摘要 + label |
| `_cjk_strings(text)` | 抽 diff 里的 CJK 字符串 |

### 4.9 src/semgraph/ — 图层

**graph.py（v1 ContextGraph，semantica 桥）**

| 函数/类 | 作用 |
|---|---|
| `_import_semantica()` | 显式导入 semantica 四件套（失败大声抛） |
| `ContextGraph` | 薄封装：从 CodeIndex+Git 事实建 v1 图 |
| ┈ `build(idx, git_api, commits_limit)` | 建 entities/relationships 并建索引与 nx 投影 |
| ┈ `_build_adj()` / `_build_nx()` | 邻接表 / networkx 无向投影（PathFinder 用） |
| ┈ `neighbors(node_id, ...)` | BFS 邻域查询 |
| ┈ `path(start, end)` | semantica PathFinder 最短关联路径 |
| ┈ `find_symbols(name)` / `callname_defs(name)` | 按名找定义 / CallName 解析 |
| ┈ `route_reaching(symbol_id)` | max_hops 内触达该符号的 API 端点 |
| ┈ `ui_strings_matching(substr)` | UI 文案匹配 |
| ┈ `track(entity_id, source)` / `sources_of(entity_id)` | provenance 记录/查询 |
| ┈ `dump(path)` / `stats()` | 落盘 / 统计 |

**schema_v2.py（GraphV2 六层 schema）**

| 函数/类 | 作用 |
|---|---|
| `NodeType` / `EdgeType` | 节点/边类型枚举（code/semantic/change/evidence/decision 五域） |
| `LAYER_OF_NODE` / `LAYER_OF_EDGE` | 类型→层归属表（G 消融统计用） |
| `stable_id(*parts)` | 内容寻址的确定性 id（crc32） |
| `Node` / `Edge` | 类型化节点/边；`layer()` 查归属；`set_validity()` 写时间字段 |
| `GraphV2` | 内存 typed graph（broker 对外的规范 schema） |
| ┈ `add_node(node)` | 加节点；同 id 异 type 直接抛（冲突绝不静默覆盖） |
| ┈ `add_edge(edge)` | 加边；重复边累计 count（信息不丢） |
| ┈ `node` / `nodes_of_type` / `edges_of_type` | 点/边查询 |
| ┈ `neighbors(nid, ...)` | BFS 邻域（方向/类型/深度可限） |
| ┈ `edges_from` / `edges_to` / `edge_between` | 定向边查询 |
| ┈ `layer_stats()` / `stats()` | 按层统计 |
| ┈ `prune_to_layers(active)` | 按激活层裁剪副本（G0-G4 消融核心） |
| ┈ `all_nodes()` / `all_edges()` | 全量读取 |
| ┈ `to_v1_dicts()` | 渲染回 v1 dict（semantica 管线零改动复用） |
| ┈ `from_v1(cg)` | 导入 v1 图（悬空引用物化为隐式 CallName，边不丢） |
| ┈ `sync_back_to_v1(cg)` | v2 独有层推回 v1（PathFinder 看到完整视图） |
| `_coerce_type(raw)` | 未知 v1 类型降级 CONCEPT 而不是崩 |
| `now_iso()` | 当前时间戳 |

**objects.py（一等数据对象）**

| 类/方法 | 作用 |
|---|---|
| `EvidenceType` | 九种证据类型枚举 |
| `Evidence` / `Evidence.make(...)` | 证据（provenance 单元），make 用内容寻址 id |
| `Finding` / `Finding.make(...)` | 单条可验证断言；`supported` 属性=有无证据 |
| `Decision` / `Decision.make(...)` | 决策记忆（reason_summary 是审计摘要，非 CoT） |
| `PolicyAction` / `PolicyRule` / `PolicyResult` | PASS/HUMAN_REVIEW/BLOCK 三档 + 规则 + 结果 |
| `Conflict` | 两条互斥 finding（双方都保留，verifier 裁决） |
| `VerdictStatus` / `Verdict` | 三档定性裁决（刻意不带数值 confidence） |

**change_graph.py（变更层填充）**

| 函数 | 作用 |
|---|---|
| `build_change_graph(broker, repo, commits_limit)` | 幂等填充变更层：Commit/ChangeUnit/Hunk/ChangedSymbol 节点 + CONTAINS_CHANGE/INTRODUCED_BY/MODIFIES/CHANGED_BY/CO_CHANGED_WITH 边（均带时间字段），每单元铸 CHANGE_UNIT evidence |

**semantic_mapper.py（语义层）**

| 函数/类 | 作用 |
|---|---|
| `SemanticTargetCandidate` | feature 候选数据形态（method/status/score） |
| `_camel(*parts)` | 词段 → CamelCase 名 |
| `SemanticMapper` | 构建并查询语义层 |
| ┈ `seed_deterministic()` | 规则种子：route/component/根 layout → Feature（是事实，带 provenance） |
| ┈ `_seed_feature(fid, name, ...)` | 单个 feature 落图 + IMPLEMENTS 边 + 证据 |
| ┈ `map_query(query, llm)` | query → 候选：词面 → 中文别名 → 可选 LLM（只挑已有名） |
| ┈ `_llm_map(query, llm)` | 经 SemanticReasoningAdapter 挑 feature，包装成 candidate 级证据 |
| ┈ `_related_symbols(feature_id)` | feature 连到的符号 |
| `ZH_FEATURE_ALIASES` / `_zh_alias(query)` | 人工整理中文别名表（数据不是逻辑） |
| `query_vocabulary(query, feature_name)` | 变更分析词表（query 词元 + feature 别名） |

**task_view.py（有界任务视图）**

| 类/方法 | 作用 |
|---|---|
| `Expansion` | 一次扩展的审计记录（seeds/relations/新增点边/trigger） |
| `TaskGraphView` | 任务实际看到的有界图切片 |
| ┈ `select(graph, task_id, targets, ...)` | 初始投影：目标 + 邻域 |
| ┈ `_admit(node, exp)` | 节点入视图并记账 |
| ┈ `expand(graph, seeds, ...)` | 沿指定关系定向生长（全程审计） |
| ┈ `refresh(graph)` | 图变更后重投影已选节点间的边（不生长） |
| ┈ `nodes_of_type(...)` | 视图内按类型取节点 |
| ┈ `dump(budget_chars)` | 有界文本投影（唯一允许进 prompt 的形态） |
| ┈ `stats()` | nodes/edges/expansion_count |

**policy.py（规则门）**

| 函数/类 | 作用 |
|---|---|
| `SimpleRule` / `evaluate(context)` | 触发条件为纯函数的规则；输出 PolicyResult |
| `_shared_symbol` 等 6 个 `_xxx` | 六条内置规则的检查函数（共享符号/公共 API/迁移文件/无证据 finding/无效路径/未解决测试失败） |
| `_mk(name, action, trigger, fn)` | 规则构造小工具 |
| `POLICY_RULES` | 规则注册表（3×HUMAN_REVIEW + 3×BLOCK） |
| `evaluate_all(context)` | 所有被触发的规则 |
| `gate(context)` | 全规则集的最重动作（gate 只裁决不执行） |

**enrich.py（v1 模式的图邻域加成，Phase 5 轨）**

| 函数 | 作用 |
|---|---|
| `get_context_graph(repo, rec)` | 构建/复用本进程的 ContextGraph |
| `enrich_locate_with_graph(...)` | locate：导入/共变邻域加成 + 图符号查询 + UI 字符串命中 |
| `enrich_impact_with_graph(...)` | impact：PathFinder 路由链 + provenance |
| `enrich_rollback_with_graph(...)` | rollback：回退/保留侧文件导入耦合证据 |

**context_broker.py（唯一门面）**

| 类/方法 | 作用 |
|---|---|
| `CAPABILITIES` | broker 方法名 → 能力名映射（守卫执法依据） |
| `ContextBroker.__init__(repo, rec, layers)` | 建 v1+v2 图、裁剪激活层、装配八个服务 |
| `layer_active(name)` | 某图层是否激活 |
| `_evidence/_findings/_conflicts/_views` | 旧私有状态兼容别名（指向服务里的活对象） |
| `resolve_target(query)` / `get_target_context(id)` | 目标解析 / 确定性邻域上下文（转发 resolution 服务） |
| `create_task_view` / `expand_task_view` / `get_task_view` | 视图三件套（转发 task_view 服务） |
| `get_change_context` / `find_change_units` / `import_couplings` | 变更层事实（转发 change 服务） |
| `add_evidence` / `get_evidence` / `all_evidence` | 证据注册/查询 |
| `add_finding` / `all_findings` / `conflicts` | finding 注册/查询 |
| `evidence_about` / `unresolved_conflicts` / `conflicts_involving` / `unsupported_findings` | 证据审计查询 |
| `set_finding_status` / `resolve_conflict` | 带守卫的状态迁移 / 冲突裁决 |
| `record_decision` / `get_precedents` | 决策记忆写入/先例查询 |
| `check_policy` / `run_policy_gate` | 单规则 / 全规则门（结果记成 decision） |
| `map_semantic_candidates(query, llm)` / `build_query_terms(...)` | 语义门面（LLM 白名单在 skill spec 侧） |
| `node` / `path` / `stats` | 自由内省（不算能力） |

### 4.10 src/services/ — 八个确定性服务

**resolution.py**

| 函数/类 | 作用 |
|---|---|
| `TargetContext` | 目标的邻域事实（callers/callees/importers/routes/tests） |
| `ResolutionService.resolve_target(query)` | query 精确符号名 → 节点；退路文件名；都不中大声抛 |
| `ResolutionService.get_target_context(id)` | 组装邻域 + 沿调用链向上找路由（深度封顶）+ 铸 AST evidence |

**graph_query.py**

| 函数/类 | 作用 |
|---|---|
| `GraphQueryService.node(id)` | 节点读取（repository.node 能力真源） |
| `_v1_isolated()` | broker 专属 v1 薄克隆（防跨 broker 污染共享缓存） |
| `path_result(a, b)` / `path(a, b)` | 经 PathFinder 的多跳路径，ToolResult 形态；无路径=答案不是失败 |

**task_view.py**

| 函数/类 | 作用 |
|---|---|
| `TaskViewService.create(...)` | 建有界视图（taskview 层未激活大声报错） |
| `TaskViewService.expand(...)` | 带审计生长（trigger 必须写明发起者） |
| `TaskViewService.get(task_id)` | 取视图 |

**change.py**

| 函数/类 | 作用 |
|---|---|
| `ChangeContext` | 时间性事实（最近触达目标的单元与提交） |
| `ChangeService.get_change_context(id)` | 单元/commit 时近排序 + 铸 CHANGE_UNIT evidence |
| `_commit_date_of(node)` / `_file_of(node_id)` | 排序键 / 目标→文件 id |
| `find_change_units(label, file, commit)` | ChangeUnit 筛选 |
| `import_couplings(files_a, files_b)` | 两文件集之间的双向 IMPORTS 边（附带损伤信号） |

**evidence.py**

| 函数/类 | 作用 |
|---|---|
| `EvidenceService.add_evidence(ev)` | 注册证据 + 落图节点 + SUPPORTED_BY 边；同 id 异事实抛 |
| `get_evidence` / `all_evidence` | 证据查询 |
| `add_finding(finding)` | 注册 finding（绝不覆盖）+ 冲突检测 |
| `_register_conflicts(finding)` | 同主题符号相交但不相同 → 登记 Conflict + CONTRADICTS 边 |
| `all_findings` / `open_conflicts` | 查询 |
| `evidence_about` / `unresolved_conflicts` / `conflicts_involving` / `unsupported_findings` | 审计查询（unsupported 喂 policy gate） |
| `set_finding_status(fid, status)` | 守卫迁移：verified 需证据齐 + 无未决冲突 |
| `resolve_conflict(conflict, ...)` | verifier 裁决：输家标 contradicted，裁决记成 Decision |
| `_topic_symbols(statement)` | finding 断言的主题符号集（停用词/sha 排除） |
| `_is_specific(token)` | identifier 形态判断（纯小写英文词不算主体） |

**decision.py**

| 函数/类 | 作用 |
|---|---|
| `DecisionService.record_decision(d)` | 记决策（reason 超 500 字硬截断——不存 CoT）+ 落图 + 连证据/finding |
| `get_precedents(category, target, query)` | 先例查询（按类/目标/自由文本符号） |

**policy.py**

| 函数/类 | 作用 |
|---|---|
| `PolicyService.check_policy(rule_name, ctx)` | 单规则求值 |
| `run_policy_gate(ctx, task_id)` | 全规则门（policy span 内）+ 结果记 decision（action→risk 外置） |

**semantic.py**

| 函数/类 | 作用 |
|---|---|
| `SemanticService.mapper()` | SemanticMapper 懒构造 + 确定性播种（语义层关→None） |
| `map_candidates_result(query, llm)` | ToolResult 形态的候选查询（LLM 缺席=degraded 留痕） |
| `map_candidates(query, llm)` | 上面的人类友好形态 |
| `query_terms(query, feature_name)` | 变更分析词表 |

**tooling.py**

| 函数 | 作用 |
|---|---|
| `call_tool(tool, fn, *args)` | 物理工具统一包装：异常转 failure、耗时入 meta |

### 4.11 src/skills/ — Skill 层

**spec.py / base.py / registry.py / runtime.py / capability.py**

| 类/函数 | 作用 |
|---|---|
| `SkillSpec` | 一份 skill 的数据契约（输入/输出/能力白名单/前置成败条件/是否允许 LLM） |
| `SkillResult` | 统一返回（status 三态 + data + evidence + capabilities_used + broker_calls） |
| `BaseSkill.run(context, broker)` | 模板方法：can_run → _execute → 异常转 failed |
| `BaseSkill.can_run(context)` | required_inputs 齐全检查 |
| `register(skill)` / `get_skill(name)` / `default_registry()` | 注册表三件套（重名即抛） |
| `SkillRuntime.run(name, ctx)` | 统一入口：解析 skill、白名单注入 llm、CapabilityGuard 执法、skill span |
| `capability_allowed(capability, declared)` | 能力是否在声明清单内（支持 `evidence.finding.*` 通配） |
| `CapabilityGuard` | 包住 broker 的窄门：未声明的 capability 调用就地抛（fail fast），计数进 SkillResult |

**八个 skill**

| Skill | 作用 |
|---|---|
| `ResolveTargetSkill.resolve_target` | 模糊 query → feature/符号目标 + 词表 + finding（LLM 唯一接线点；无命中走确定性退路） |
| `BuildTaskViewSkill.build_task_view` | 围绕目标建初始有界视图（taskview 层关时 partial） |
| `ImpactAnalysisSkill.impact_analysis` | 逐目标聚合 callers/routes/files/symbols + 视图带审计生长 + impact finding |
| `ChangeUnitAnalysisSkill.change_unit_analysis` | 词表对 ChangeUnit 四路确定性打分（label/UI/symbol/file，权重来自策略 yaml）+ 时近排序 |
| `CouplingAnalysisSkill.coupling_analysis` | 两组文件集合的直接 IMPORTS 耦合（双向） |
| `SafeRollbackSkill.safe_rollback` | 仲裁 + 计划装配 + policy gate + 逐单元 decision（绝不执行 git） |
| `safe_rollback.arbitrate(...)` | 纯函数：双词表命中仲裁、平局策略外置、keep 命中钉住 commit（agent/skill 共用真源） |
| `EvidenceVerificationSkill.evidence_verification` | finding 三档裁决：确定性路径可升 verified；语义证据只能佐证 |
| `PolicyCheckSkill.policy_check` | 组装 gate 上下文跑规则门（decision 层关时显式 UNGATED） |

### 4.12 src/agents/ — Agent 层

| 类/方法 | 作用 |
|---|---|
| `Orchestrator.run(query, keep_hint)` | 五步管线：导航→变更匹配→仲裁→波及面→计划→校验；每 agent 一个 ScopedContext |
| `FinalReport.dump()` | 有界人类可读报告（含"nothing was executed"声明） |
| `RepositoryNavigator.find_target(query)` | 委托 resolve_target skill；失败大声抛 |
| `ChangeIntelligenceAgent.find_units(terms, commits)` | 委托 change_unit_analysis；commits 限定搜索范围 |
| `to_unit_match(m)` | skill 的 match dict → UnitMatch |
| `ImpactSliceAgent.slice_impact(targets, task_id)` | 委托 build_task_view + impact_analysis |
| `RollbackPlanner.arbitrate(...)` | 委托 skill 模块纯函数仲裁 |
| `RollbackPlanner.plan(unit_ids, ...)` | 委托 safe_rollback 装配 RollbackPlan |
| `RollbackPlan.dump()` | 计划文本渲染 |
| `DeterministicVerifier.verify(finding)` | 委托 evidence_verification(mode=deterministic) |
| `SemanticVerifier.verify(finding)` | 委托 evidence_verification(mode=semantic)；无语义证据返回 None |
| `ScopedContext.produced(...)` | agent 产出记账（evidence/finding/decision） |

### 4.13 src/llm/ — LLM 层

| 类/函数 | 作用 |
|---|---|
| `LLMClient.__init__(cfg)` | OpenAI 兼容客户端（未配置=不可用；配置了但初始化失败=LLMError） |
| `LLMClient.chat_json(system, user)` | 单次 JSON 模式调用（截断到上下文上限；解析容错 ```json 围栏） |
| `SemanticReasoningAdapter.map_features(features, query)` | query → feature 候选；幻觉 id 直接丢弃（不变量：不创造不存在的 feature） |
| `SemanticReasoningAdapter.label_change_unit(...)` | 变更单元语义标签（预留，12C 接线）；白名单外归 other |
| `SemanticReasoningAdapter.verify_semantic(...)` | finding 语义裁决（预留）；证据不足归 unresolved |
| `prompts/invariants.with_invariants(prompt)` | 公共不变量拼到 skill prompt 尾部 |
| `prompts/skills/semantic_mapping.py` | feature 映射 prompt + payload 构造（MAX_CANDIDATES=3） |
| `prompts/skills/change_labeling.py` / `semantic_verification.py` | 预留两路 prompt（标签白名单 / 裁决白名单） |

### 4.14 src/tasks/ + src/eval/ — Phase 1-5 四模式轨

| 类/函数 | 作用 |
|---|---|
| `get_runner(task)`（tasks/__init__） | 任务名 → runner 注册表 |
| `ContextItem` / `RetrievalBundle` | 上下文条目与有界捆绑（add/dump） |
| `TaskRunnerBase` | 共用底座：四层开关（lexical/structural/git/graph_enabled） |
| `LocateRunner.run(query)` | 定位：词法种子 → 逐层加结构/git/图证据 → 可选 LLM 重排 |
| `ImpactRunner.run(query)` | 影响面：挑符号 → 结构调用图 / 词法退路 → git/图证据 |
| `RollbackRunner.run(query, ref)` | 回退分析：拆问题/保留子句 → 域匹配单元 → 风险评估 + 操作提示 |
| `_split_query` / `_classify` / `_domains_of` / `_unit_domains` / `_collateral_risk` / `_decision_evidence` / `_operations_hint` | RollbackRunner 的子步骤（子句拆分/单元分类/域提取/附带损伤风险/决策证据/git 操作提示——只是提示） |
| `compute_metrics(task, output, gold)` | 按任务类型分发指标 |
| `locate_metrics` / `impact_metrics` / `rollback_metrics` / `_prf` | 文件级 P/R、影响对 P/R、回退保留率与附带损伤 |
| `eval/runner.load_tasks` / `run_one` / `summarize` / `write_report` / `main` | 消融 runner：tasks.jsonl × 4 模式，无 gold 只执行不评分 |

### 4.15 experiments/ — 实验入口

| 函数 | 作用 |
|---|---|
| `g_ablation.run_level(name, layers)` | G0-G4 单级消融（同场景逐级开图层） |
| `g_ablation.main()` | 跑全级并输出对照表 |
| `skill_eval.run_skill_eval(repo, query)` | 逐 skill 评测（status/latency/broker_calls/evidence/正确性指标） |
| `skill_eval.prf` / `_run_one` / `_view_nodes` | 指标与执行小工具 |
| `real_repo/schema.validate_task(d)` / `load_tasks(path)` | 真实 repo 任务 schema 校验（manual/pending 两源；重复 id 检测） |
| `real_repo/loader.BrokerPool` | 每 repo 一个 broker（真实 repo 默认 commits_limit=900 大窗口） |
| `real_repo/benchmark.RealRepoBenchmark.run_task(task)` | 按 task_type 走真实 skill 链（locate/impact/history/rollback/compound 五类） |
| `real_repo/evaluator.score_task(task, raw)` | manual gold 才评分（文件 P/R、符号命中、commit 前缀匹配、保留率/附带损伤） |
| `real_repo/execution_bench.ExecutionBench.run_task(task, oracle)` | 克隆副本上跑完整执行链（沙箱事实测量；oracle=True 跳过分析直喂 gold 单元） |
| `real_repo/evaluator.score_execution(task, raw)` | 执行环评分：worktree 实际 diff 文件面 vs gold（贴出来才算，计划说了不算） |
| `real_repo/report.summarize` / `write_markdown` / `write_jsonl` | JSONL 原始 + Markdown 汇总（失败案例单列 + 执行环两段：analysis/oracle） |

### 4.16 src/maintenance/ — Phase 13 执行环（沙箱 + 晋升）

| 类/函数 | 作用 |
|---|---|
| `models.py`（ExecutionPlan/ExecutionAttempt/PatchArtifact/ExecutionStatus…） | 执行环数据模型；状态机 PLANNED→…→PROMOTED，失败即终态 |
| `sandbox_git.SandboxGit.run(args, cwd, env)` | 沙箱 git 运行器：argv 旗标白名单 + cwd 钉死（sandbox/repo 分区）+ 写 index 命令强制隔离 GIT_INDEX_FILE |
| `sandbox_git.SandboxGit.run_source(args)` | 晋升专用：唯一允许在源仓库跑的 git（argv 钉死 apply --check/apply/diff 三形态） |
| `services/workspace.WorkspaceService` | prepare（建沙箱 worktree + STALE_PLAN 复核）/ 执行注册表 / attempt 落盘（含 trace.jsonl） |
| `services/patch.PatchService.build_inverse_patch(plan)` | 确定性反向 patch：keep 单元构造性排除，同文件 hunk 按 new_start 拼接 |
| `services/patch.content_drift(...)` | 内容级 out-of-plan 核对：临时 index 物化 base+patch 期望 blob，与 worktree hash-object 逐文件比对（diff 文本比对在等价重排下会假阳性） |
| `services/validation.ValidationService` | 沙箱验证命令（SafeCommandRunner：argv 白名单 + shell=False，命令绝不来自 LLM） |
| `services/verification.VerificationService.verify(attempt)` | 五面终审（expected/preservation/scope/tests/traceability）→ VERIFIED/PARTIAL/FAILED |
| `services/execution_policy.pre/post_execution_gate` | 13H 双门：计划危险面与沙箱事实裁决（BLOCK 就地停车 / 永不晋升） |
| `services/promotion.PromotionService.approve/promote` | 三把钥匙（READY_TO_PROMOTE + explicit_approval is True + promote_enabled）；verified.patch 字节冻结；reverse.patch 留档 |
| `agents/executor.MaintenanceExecutorAgent.execute(plan)` | 13I 执行链唯一驾驶员：build→pre gate→prepare→patch→apply→validate→verify→post gate，停车即停车 |

---

## 5. 三条铁律在代码里的落点

| 铁律 | 落点 |
|---|---|
| 只读 git | `git_history/api.py` 白名单（`ALLOWED` 集合外执行前即抛）；全链路无 checkout/revert/reset |
| LLM 不铸造事实 | `SkillRuntime` 白名单注入 + `semantic_adapter` 幻觉过滤 + prompts 不变量 + EvidenceVerification 的 PARTIALLY 档 |
| 证据先行 | EvidenceService 守卫迁移（无证据不能 verified）；policy `unsupported_finding` 规则 BLOCK |
