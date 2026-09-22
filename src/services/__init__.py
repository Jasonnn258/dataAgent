"""Service 层（Phase 11D）：确定性事实的组合方式。

分层语义：Broker = agent/skill 能请求哪些系统能力（API 面）；
Service = 这些能力如何组合确定性事实（逻辑体）；Physical Tool =
真正碰源码/git/AST 的底层（src/git_history、src/search、src/structural、
src/change_units、semantica —— 只在图构建期经 enrich 进入）。

八个服务（broker 内部委托至此，对外 API 不变）：
    GraphQueryService    节点读取 + 多跳路径
    ResolutionService    目标解析 + 确定性邻域上下文
    TaskViewService      有界视图创建/生长/读取
    ChangeService        变更层上下文/单元筛选/导入耦合
    EvidenceService      evidence/finding/冲突注册表 + 状态迁移
    DecisionService      决策记忆（记录 + 先例查询）
    PolicyService        版本化规则门 + 落档
    SemanticService      语义播种 + 查询词表门面
    WorkspaceService     仓库状态指纹 + 沙箱工作区（Phase 13B/13C）
    PatchService         确定性反向 patch 构建（Phase 13D）
    ValidationService    沙箱验证命令执行（Phase 13F）
"""
from src.services.change import ChangeService, ChangeContext
from src.services.decision import DecisionService
from src.services.evidence import EvidenceService
from src.services.graph_query import GraphQueryService
from src.services.patch import PatchService
from src.services.policy import PolicyService
from src.services.resolution import ResolutionService, TargetContext
from src.services.semantic import SemanticService
from src.services.task_view import TaskViewService
from src.services.tooling import call_tool
from src.services.validation import SafeCommandRunner, ValidationService
from src.services.workspace import WorkspaceService

__all__ = ["GraphQueryService", "ResolutionService", "TaskViewService",
           "ChangeService", "EvidenceService", "DecisionService",
           "PolicyService", "SemanticService", "WorkspaceService",
           "PatchService", "ValidationService", "SafeCommandRunner",
           "TargetContext", "ChangeContext", "call_tool"]
