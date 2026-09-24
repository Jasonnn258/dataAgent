"""SemanticReasoningAdapter —— LLM 语义推理的唯一合法入口（Phase 11H）。

定位（spec）：
- LLM 不是 Physical Tool，绝不混入确定性 Physical Tool Layer；
  它是可选的语义推理适配器，供少数语义 Skill 精化候选
- 只有 SkillSpec.semantic_reasoning = allowed 的 skill 才可能拿到
  llm 引用（SkillRuntime 白名单注入，见 runtime.py），调用最终
  收敛到本适配器
- 不变量在这里双重生效：prompt 层（invariants.py）+ 代码层过滤
  （幻觉 id 丢弃、标签/裁决白名单、证据不足归 unresolved）

输出永远是 candidate 级建议 —— 确定性事实（CALLS/IMPORTS/git 关系）
只能由 Physical Tool 铸造。
"""
from __future__ import annotations

from src.llm.prompts.skills import (change_labeling, semantic_mapping,
                                    semantic_verification)


class SemanticReasoningAdapter:
    """把 duck-typed LLM 客户端收敛成语义推理接口。

    llm 只需满足 `available` 属性 + `chat_json(system, user)`；
    上游（SkillRuntime 注入）负责白名单，本层负责不变量。
    """

    def __init__(self, llm):
        self.llm = llm

    @property
    def available(self) -> bool:
        return bool(getattr(self.llm, "available", False))

    # ---------------------------------------------- 已启用：resolve_target
    def map_features(self, features: list[dict], query: str) -> list[dict]:
        """query → feature 候选（semantic_mapping prompt）。

        只保留输入里真实存在的 feature_id —— 幻觉直接丢弃。
        chat_json 异常原样上抛，由调用方决定确定性退路
        （semantic_mapper 会 warn 后返回 []）。
        """
        if not self.available:
            return []
        out = self.llm.chat_json(
            system=semantic_mapping.SYSTEM_PROMPT,
            user=semantic_mapping.user_payload(features, query))
        known = {f.get("feature_id") for f in features}
        picks: list[dict] = []
        for c in (out or {}).get("candidates", [])[:semantic_mapping.MAX_CANDIDATES]:
            fid = c.get("feature_id")
            if fid in known:  # 不变量：不创造不存在的 feature
                picks.append({"feature_id": fid,
                              "reason": str(c.get("reason", ""))[:200]})
        return picks

    # ---------------------------------------------- 12C 启用：单元标签
    def label_change_units(self, units: list[dict], hint_terms: list[str],
                           known_features: list[str]) -> list[dict]:
        """变更单元语义标签批量版（change_labeling prompt，12C 启用）。

        输出契约（严格 JSON）：每单元 {unit_id, label, intent,
        candidate_features, reason}。白名单外 label/intent 归 other；
        candidate_features 只保留 known_features 里真实存在的名字
        （不变量：LLM 只能挑已有名字）。LLM 不可用/输出不合法 =>
        空列表（调用方走确定性路径），绝不因适配器缺位抛错。
        """
        if not self.available:
            return []
        out = self.llm.chat_json(
            system=change_labeling.SYSTEM_PROMPT,
            user=change_labeling.user_payload(
                units[:change_labeling.MAX_UNITS], hint_terms,
                known_features)) or {}
        known = set(known_features)
        by_id = {u["unit_id"] for u in units}
        results: list[dict] = []
        for item in (out or {}).get("units", []):
            uid = item.get("unit_id")
            if uid not in by_id:   # 幻觉 unit_id 直接丢弃
                continue
            label = item.get("label")
            if label not in change_labeling.LABELS:
                label = "other"
            intent = item.get("intent")
            if intent not in change_labeling.INTENTS:
                intent = "other"
            feats = [f for f in item.get("candidate_features", [])
                     if isinstance(f, str) and f in known]
            results.append({"unit_id": uid, "label": label, "intent": intent,
                            "candidate_features": feats,
                            "reason": str(item.get("reason", ""))[:200],
                            "status": "candidate"})
        return results

    def verify_semantic(self, claim: str, evidence_texts: list[str]) -> dict:
        """finding 语义裁决（semantic_verification prompt，未启用）。

        LLM 不可用/裁决不在白名单 => unresolved —— 证据不足不允许猜。
        """
        unresolved = {"verdict": "unresolved",
                      "reason": "llm unavailable or output rejected",
                      "fallback": True}
        if not self.available:
            return unresolved
        out = self.llm.chat_json(
            system=semantic_verification.SYSTEM_PROMPT,
            user=semantic_verification.user_payload(claim, evidence_texts)) or {}
        verdict = out.get("verdict")
        if verdict not in semantic_verification.VERDICTS:
            verdict = "unresolved"
        return {"verdict": verdict,
                "reason": str(out.get("reason", ""))[:200],
                "fallback": False}
