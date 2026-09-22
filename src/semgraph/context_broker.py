"""Context Broker（Phase 9B / 11D）：agent 与 Semantica 之间的唯一一扇门。

Agent 绝不直接碰 GraphV2 内部结构、semantica.kg 对象、CodeIndex 或
GitAPI —— 一切经由这个 facade，它返回的全是项目自有的 dataclass
（spec 9B）。

为什么要这层间接：它是事实进入系统那一刻铸造 evidence 的接缝（原则
5）、给 task view 做预算的地方（原则：禁止整图检索）、也是 Phase 10
G0-G4 消融开关图层的旋钮。

11D 起，broker 只回答"能请求哪些系统能力"（方法面 + CAPABILITIES 映
射），"如何组合确定性事实"全部下沉到 src/services/ 的八个确定性服务；
对外 API 与行为完全不变（旧私有入口 _findings/_evidence/_conflicts、
REASON_SUMMARY_CAP 以属性别名保留）。
"""
from __future__ import annotations

from pathlib import Path

from src.errors import DataAgentError
from src.schema import ToolRecorder
from src.semgraph.objects import (Conflict, Decision, Evidence, EvidenceType,
                                  Finding, PolicyResult)
from src.semgraph.schema_v2 import EdgeType, GraphV2, Node
from src.semgraph.task_view import TaskGraphView
# 11D：服务层（TargetContext/ChangeContext 自服务迁入，这里再导出）
from src.services import (ChangeService, DecisionService, EvidenceService,
                          GraphQueryService, PatchService, PolicyService,
                          ResolutionService, SemanticService, TaskViewService,
                          ValidationService, VerificationService,
                          WorkspaceService)
from src.services.change import ChangeContext
from src.services.resolution import TargetContext


# ------------------------------------------------------------ 能力映射（11C）
# broker 方法名 → capability 名。SkillSpec.allowed_capabilities 声明的
# 就是右侧的名字；SkillRuntime 用它做运行期执法（fail fast）。
# 未列出的方法（layer_active/stats/rec/graph…）是自由内省，不算能力。
CAPABILITIES: dict[str, str] = {
    # repository.*：代码层解析与节点读取
    "resolve_target":        "repository.resolve_target",
    "get_target_context":    "repository.get_context",
    "node":                  "repository.node",
    # graph.*：有界视图与路径
    "path":                  "graph.find_path",
    "create_task_view":      "graph.create_task_view",
    "expand_task_view":      "graph.expand_task_view",
    "get_task_view":         "graph.get_task_view",
    # change.*：变更层事实
    "get_change_context":    "change.get_context",
    "find_change_units":     "change.find_units",
    "import_couplings":      "change.get_couplings",
    # evidence.*：证据与 finding 的注册/查询/状态迁移
    "add_evidence":          "evidence.add",
    "get_evidence":          "evidence.get",
    "all_evidence":          "evidence.query",
    "evidence_about":        "evidence.query",
    "add_finding":           "evidence.finding.add",
    "all_findings":          "evidence.query",
    "unsupported_findings":  "evidence.query",
    "conflicts":             "evidence.query",
    "conflicts_involving":   "evidence.query",
    "unresolved_conflicts":  "evidence.query",
    "set_finding_status":    "evidence.finding.set_status",
    "resolve_conflict":      "evidence.finding.set_status",
    # decision.*：决策记忆
    "record_decision":       "decision.record",
    "get_precedents":        "decision.query",
    # policy.*：规则门
    "run_policy_gate":       "policy.gate",
    "check_policy":          "policy.evaluate",
    "pre_execution_gate":    "policy.gate",
    "post_execution_gate":   "policy.gate",
    # semantic.*：语义门面（LLM 白名单在 skill spec 侧）
    "map_semantic_candidates": "semantic.map_candidates",
    "build_query_terms":     "semantic.query_terms",
    # execution.*：执行层（Phase 13）—— 快照是只读指纹，
    # prepare 起的一切写动作只落在沙箱 worktree
    "execution_snapshot":    "execution.snapshot",
    "prepare_execution":     "execution.prepare",
    "build_rollback_patch":  "execution.build_patch",
    "apply_execution_patch": "execution.apply_patch",
    "validate_execution":    "execution.validate",
    "verify_execution":      "execution.verify",
}


class ContextBroker:
    # "taskview" 是伪层：它门控的是有界 task view，不是图内容
    #（Phase 10 消融里的 G3）
    ALL_LAYERS = {"code", "semantic", "change", "evidence", "decision",
                  "taskview"}

    def __init__(self, repo: Path, rec: ToolRecorder | None = None,
                 layers: set[str] | None = None):
        """layers：本次运行激活哪些层（G0..G4 消融）。None = 当前已建的
        全部。未激活的图层从 v2 投影里剪掉；未激活的能力在 agent 尝试
        使用时大声报错，绝不静默降级。"""
        from src.semgraph.enrich import get_context_graph
        from src.execution import ExecutionRecorder
        self.repo = repo
        # 11F：默认用结构化记录器（ToolRecorder 的超集，兼容零改动）
        self.rec = rec or ExecutionRecorder()
        self._v1 = get_context_graph(self.repo, self.rec)
        self.graph = GraphV2.from_v1(self._v1)
        self.layers = (set(layers) | {"code"}) if layers is not None \
            else set(self.ALL_LAYERS)
        unknown = self.layers - self.ALL_LAYERS
        if unknown:
            raise DataAgentError(f"unknown layers {sorted(unknown)}")
        if layers is not None:
            self.graph = self.graph.prune_to_layers(self.layers)
        # 11D：确定性服务（组合逻辑的真源；broker 只做能力门面）
        self._graph_svc = GraphQueryService(self)
        self._resolution_svc = ResolutionService(self)
        self._views_svc = TaskViewService(self)
        self._change_svc = ChangeService(self)
        self._evidence_svc = EvidenceService(self)
        self._decision_svc = DecisionService(self)
        self._policy_svc = PolicyService(self)
        self._semantic_svc = SemanticService(self)
        # 13B：执行层（快照只读；prepare/沙箱写动作见 WorkspaceService）
        self._workspace_svc = WorkspaceService(self)
        # 13D：确定性反向 patch 构建（惰性建图缓存，不占 import 期）
        self._patch_svc = PatchService(self)
        # 13F：沙箱验证命令（SafeCommandRunner：argv 白名单 + shell=False）
        self._validation_svc = ValidationService(self)
        # 13G：执行结果终审（Verifier 只裁决，不修复）
        self._verification_svc = VerificationService(self)

    def layer_active(self, name: str) -> bool:
        return name in self.layers

    # ------------------------------------------------ 旧私有状态兼容别名
    #（测试/脚本直接读注册表 dict；返回的是服务里的活对象）
    @property
    def _evidence(self) -> dict[str, Evidence]:
        return self._evidence_svc.evidence

    @property
    def _findings(self) -> dict[str, Finding]:
        return self._evidence_svc.findings

    @property
    def _conflicts(self) -> list[Conflict]:
        return self._evidence_svc.conflicts

    @property
    def _views(self) -> dict[str, TaskGraphView]:
        return self._views_svc.views

    # 审计文本硬上限的真源在 DecisionService（兼容旧引用）
    REASON_SUMMARY_CAP = DecisionService.REASON_SUMMARY_CAP

    # ------------------------------------------------------------ 解析
    def resolve_target(self, query: str) -> Node:
        return self._resolution_svc.resolve_target(query)

    def get_target_context(self, target_id: str) -> TargetContext:
        return self._resolution_svc.get_target_context(target_id)

    # ------------------------------------------------------------ 视图
    def create_task_view(self, task_id: str, target_ids: list[str],
                         relations: set[EdgeType] | None = None) -> TaskGraphView:
        return self._views_svc.create(task_id, target_ids, relations)

    def expand_task_view(self, task_id: str, seeds: list[str],
                         relations: set[EdgeType] | None = None,
                         depth: int = 1, trigger: str = "") -> TaskGraphView:
        return self._views_svc.expand(task_id, seeds, relations, depth, trigger)

    def get_task_view(self, task_id: str) -> TaskGraphView:
        return self._views_svc.get(task_id)

    # ------------------------------------------------------------ 变更
    def get_change_context(self, target_id: str) -> ChangeContext:
        return self._change_svc.get_change_context(target_id)

    def find_change_units(self, label: str = "", file: str = "",
                          commit: str = "") -> list[Node]:
        return self._change_svc.find_change_units(label, file, commit)

    def import_couplings(self, files_a: list[str],
                         files_b: list[str]) -> list[str]:
        return self._change_svc.import_couplings(files_a, files_b)

    # ------------------------------------------------------------ evidence
    def add_evidence(self, ev: Evidence) -> Evidence:
        return self._evidence_svc.add_evidence(ev)

    def get_evidence(self, ids: list[str]) -> list[Evidence]:
        return self._evidence_svc.get_evidence(ids)

    def all_evidence(self) -> list[Evidence]:
        return self._evidence_svc.all_evidence()

    # ------------------------------------------------------------ finding
    def add_finding(self, finding: Finding) -> Finding:
        return self._evidence_svc.add_finding(finding)

    def all_findings(self) -> list[Finding]:
        return self._evidence_svc.all_findings()

    @property
    def conflicts(self) -> list[Conflict]:
        return self._evidence_svc.open_conflicts

    # ------------------------------------------------------------ 校验（9F/9G）
    def evidence_about(self, target_id: str,
                       ev_type: EvidenceType | None = None) -> list[Evidence]:
        return self._evidence_svc.evidence_about(target_id, ev_type)

    def unresolved_conflicts(self) -> list[Conflict]:
        return self._evidence_svc.unresolved_conflicts()

    def conflicts_involving(self, finding_id: str) -> list[Conflict]:
        return self._evidence_svc.conflicts_involving(finding_id)

    def unsupported_findings(self) -> list[Finding]:
        return self._evidence_svc.unsupported_findings()

    def set_finding_status(self, finding_id: str, status: str,
                           verifier: str = "") -> Finding:
        return self._evidence_svc.set_finding_status(finding_id, status,
                                                     verifier)

    def resolve_conflict(self, conflict: Conflict, resolution: str,
                         winner: str | None = None,
                         resolver: str = "verifier") -> Conflict:
        return self._evidence_svc.resolve_conflict(conflict, resolution,
                                                   winner, resolver)

    # ------------------------------------------------------------ 决策（9H）
    def record_decision(self, d: Decision) -> Decision:
        return self._decision_svc.record_decision(d)

    def get_precedents(self, category: str = "", target: str = "",
                       query: str = "") -> list[Decision]:
        return self._decision_svc.get_precedents(category, target, query)

    # ------------------------------------------------------------ 策略（9I）
    def check_policy(self, rule_name: str, context: dict) -> PolicyResult:
        return self._policy_svc.check_policy(rule_name, context)

    def run_policy_gate(self, context: dict, task_id: str = "") -> PolicyResult:
        """跑完整规则集并把结果记成可审计 decision。返回触发的最重动作。
        服从 BLOCK 是 orchestrator 的契约 —— gate 只裁决，不执行。"""
        return self._policy_svc.run_policy_gate(context, task_id)

    def pre_execution_gate(self, plan, task_id: str = ""):
        """执行前门（13H）：计划危险面裁决并记 decision。"""
        return self._policy_svc.pre_execution_gate(plan, task_id)

    def post_execution_gate(self, attempt, task_id: str = ""):
        """执行后门（13H）：沙箱事实裁决并记 decision。"""
        return self._policy_svc.post_execution_gate(attempt, task_id)

    # ------------------------------------------------------------ 语义门面（11A）
    def map_semantic_candidates(self, query: str, llm=None) -> list:
        """模糊 query → feature 候选（agent/skill 不再自己构造 mapper）。"""
        return self._semantic_svc.map_candidates(query, llm=llm)

    def build_query_terms(self, query: str, feature_name: str) -> list[str]:
        """变更分析词表（query 词元 + feature 别名）。"""
        return self._semantic_svc.query_terms(query, feature_name)

    # ------------------------------------------------------------ 执行层（13B）
    def execution_snapshot(self, files: list[str] | None = None):
        """源仓库当前状态指纹（只读；stale 检测的基准）。"""
        return self._workspace_svc.snapshot(files)

    def prepare_execution(self, plan):
        """为 ExecutionPlan 建沙箱 worktree（13C）。

        源仓库零修改（.git/worktrees 元数据除外）；计划过期 →
        STALE_PLAN 终态。get_execution/executions 是自由内省。
        """
        return self._workspace_svc.prepare(plan)

    def get_execution(self, execution_id: str):
        return self._workspace_svc.get_execution(execution_id)

    def executions(self):
        return self._workspace_svc.executions()

    def build_rollback_patch(self, attempt):
        """为 PREPARED 尝试构建确定性反向 patch 并沙箱预检（13D）。

        成功 → PATCH_BUILT + PatchArtifact；预检失败 → CONFLICT 终态。
        keep 单元构造性排除在 PatchService.build_inverse_patch 里。
        """
        return self._patch_svc.build(attempt)

    def apply_execution_patch(self, attempt):
        """把 proposed.patch 应用进沙箱并做计划外修改检查（13E）。

        APPLY_CHECKED → APPLIED_SANDBOX；任何计划外修改 →
        VERIFICATION_FAILED 终态。绝不在源仓库上 apply。
        """
        return self._patch_svc.apply(attempt)

    def validate_execution(self, attempt):
        """在沙箱里跑验证命令（13F）。全过 → VALIDATED；任何失败 →
        TEST_FAILED 终态。命令只来自 plan 或 repo 配置，绝不来自 LLM。
        """
        return self._validation_svc.run(attempt)

    def verify_execution(self, attempt):
        """终审（13G）：五面裁决 → VERIFIED / PARTIAL / FAILED。
        PARTIAL 不推进状态（软面没齐不许往 promote 走）。
        """
        return self._verification_svc.verify(attempt)

    # ------------------------------------------------------------ 工具
    def node(self, node_id: str) -> Node | None:
        return self._graph_svc.node(node_id)

    def path(self, a: str, b: str) -> list[str] | None:
        return self._graph_svc.path(a, b)

    def stats(self) -> dict:
        return {"graph": self.graph.stats(),
                "evidence": len(self._evidence_svc.evidence),
                "findings": len(self._evidence_svc.findings),
                "conflicts": len(self._evidence_svc.conflicts),
                "decisions": len(self._decision_svc.decisions),
                "views": {t: v.stats()
                          for t, v in self._views_svc.views.items()}}
