"""Capability 守卫（Phase 11C）：skill 运行期的能力执法。

SkillSpec.allowed_capabilities 是声明式契约；本模块把它变成运行期
硬约束 —— SkillRuntime 在执行 skill 时用 CapabilityGuard 包住 broker，
skill 每碰一个带 capability 的 broker 方法，守卫就地核对声明：
未声明的使用立刻抛 DataAgentError（fail fast），绝不等到事后审计。

自由内省（layer_active / stats / rec）不算能力，直通。
"""
from __future__ import annotations

from src.errors import DataAgentError


def capability_allowed(capability: str, declared: list[str]) -> bool:
    """capability 是否落在声明清单内。支持 "evidence.finding.*" 前缀
    通配（声明一族能力）。"""
    for entry in declared:
        if entry == capability:
            return True
        if entry.endswith(".*") and capability.startswith(entry[:-1]):
            return True
    return False


class CapabilityGuard:
    """包住 broker 的窄门：按 skill 的声明核对每次能力调用。

    used 集合记录本次运行实际碰到的 capability（进 SkillResult，
    供 11F 执行事件 / 11J 评估消费）。
    """

    def __init__(self, broker, skill_name: str, declared: list[str]):
        self._broker = broker
        self._skill = skill_name
        self._declared = list(declared or [])
        self.used: set[str] = set()
        self.call_count = 0             # 能力调用次数（11J 评估指标）

    def _check(self, method: str, capability: str) -> None:
        if not capability_allowed(capability, self._declared):
            raise DataAgentError(
                f"skill {self._skill!r} used undeclared capability "
                f"{capability!r} (broker.{method}) — declare it in "
                f"SkillSpec.allowed_capabilities")
        self.used.add(capability)
        self.call_count += 1

    def __getattr__(self, item: str):
        # 延迟导入避免循环依赖（context_broker 不反向 import skills）
        from src.semgraph.context_broker import CAPABILITIES
        attr = getattr(self._broker, item)
        capability = CAPABILITIES.get(item)
        if capability is None or not callable(attr):
            return attr                      # 非 capability：自由内省直通
        def guarded(*args, **kwargs):
            self._check(item, capability)
            return attr(*args, **kwargs)
        return guarded
