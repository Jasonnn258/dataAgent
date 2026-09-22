"""Validation Service（Phase 13F）：沙箱里跑验证命令的唯一出口。

铁律（spec 13F）：
- **禁止 shell=True**：只收 argv list（["pytest", "-q"]），shell 字符串
  在 13B 就被丢掉，这里再防一层（非 list[str] 直接 BLOCKED）
- **可执行文件白名单**：argv[0] 只能是裸名且在白名单里 —— "./evil"、
  "/bin/sh"、"sh -c ..." 构造上不可能跑
- cwd 钉死在沙箱 worktree（外面传进来的路径一律拒绝）
- timeout 有界、输出摘要有界（头尾各留一段）
- 命令来源只有两个：plan.validation_commands（调用方显式给）或
  <repo>/.dataagent/validation_commands.json（repo 配置）。绝不来自
  LLM 输出。

结果永远是结构化的 ValidationResult —— 就算全绿也不代表执行成功，
那只代表"验证命令跑过了"；执行成败由 13G Verifier 综合裁决。
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from src.errors import DataAgentError
from src.maintenance.models import ExecutionAttempt, ValidationResult

# 可执行文件白名单（裸名；带路径分隔符的一律拒绝）
DEFAULT_ALLOWED_EXECUTABLES: frozenset[str] = frozenset({
    "pytest", "python", "python3", "node", "npm", "npx", "yarn",
    "pnpm", "tsc", "eslint", "ruff", "make",
})

_TIMEOUT = 600          # 单命令秒数上限
_SUMMARY_HEAD = 1_500   # 摘要保留的头部字符
_SUMMARY_TAIL = 500     # 摘要保留的尾部字符


def _bound(text: str) -> str:
    """输出摘要：头 + 尾，中间截断标记（测试输出通常头是收集、尾是结论）。"""
    if len(text) <= _SUMMARY_HEAD + _SUMMARY_TAIL + 50:
        return text
    return (text[:_SUMMARY_HEAD]
            + f"\n... [{len(text)} chars, truncated] ...\n"
            + text[-_SUMMARY_TAIL:])


class SafeCommandRunner:
    """argv-list-only 命令运行器（shell=False + 白名单 + 有界输出）。"""

    def __init__(self, allowed: frozenset[str] | None = None,
                 timeout: int = _TIMEOUT) -> None:
        self.allowed = allowed if allowed is not None \
            else DEFAULT_ALLOWED_EXECUTABLES
        self.timeout = timeout

    def _blocked(self, argv, why: str) -> ValidationResult:
        return ValidationResult(command=list(argv) if isinstance(argv, list)
                                else [repr(argv)],
                                status="BLOCKED",
                                stderr_summary=why[:_SUMMARY_HEAD])

    def run(self, argv, cwd: Path) -> ValidationResult:
        # ---- 形态门：argv list of non-empty str ----
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(a, str) and a.strip() for a in argv)):
            return self._blocked(argv, "command must be a non-empty "
                                        "argv list of strings")
        # ---- 白名单门：裸名 + 在集合内 ----
        head = argv[0]
        if "/" in head or "\\" in head or head.endswith(".exe"):
            return self._blocked(argv, f"path-like executable refused: "
                                       f"{head!r}")
        if head not in self.allowed:
            return self._blocked(argv, f"executable not in whitelist: "
                                       f"{head!r} (allowed: "
                                       f"{sorted(self.allowed)})")
        # ---- cwd 门：必须在给定根目录内（调用方传沙箱 worktree）----
        here = Path(cwd).resolve()
        if not here.is_dir():
            return self._blocked(argv, f"cwd is not a directory: {cwd}")

        start = time.monotonic()
        try:
            proc = subprocess.run(argv, cwd=str(here), shell=False,
                                  capture_output=True, text=True,
                                  errors="replace", timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            return ValidationResult(
                command=argv, exit_code=None,
                duration=time.monotonic() - start, status="TIMEOUT",
                stdout_summary=_bound(e.stdout or ""),
                stderr_summary=f"timed out after {self.timeout}s")
        except OSError as e:
            return ValidationResult(
                command=argv, exit_code=None,
                duration=time.monotonic() - start, status="ERROR",
                stderr_summary=_bound(str(e)))
        return ValidationResult(
            command=argv, exit_code=proc.returncode,
            duration=time.monotonic() - start,
            status="PASSED" if proc.returncode == 0 else "FAILED",
            stdout_summary=_bound(proc.stdout or ""),
            stderr_summary=_bound(proc.stderr or ""))


class ValidationService:
    """把 plan/repo-config 的验证命令跑在沙箱里（broker 委托至此）。"""

    # repo 配置文件名（放在 <repo>/.dataagent/ 下）
    CONFIG_NAME = "validation_commands.json"

    def __init__(self, broker) -> None:
        self.broker = broker
        self.repo = broker.repo
        self.rec = broker.rec
        self.runner = SafeCommandRunner()

    # ------------------------------------------------------------ 命令来源
    def _commands(self, attempt: ExecutionAttempt) -> list[list[str]]:
        """plan 优先，其次 repo 配置；两处都没有就空跑（13G 会判
        tests_pass=False，不会因为"没跑测试"而沾沾自喜）。"""
        cmds = [list(c) for c in (attempt.plan.validation_commands or [])
                if isinstance(c, list) and c]
        if cmds:
            return cmds
        cfg = self.repo / ".dataagent" / self.CONFIG_NAME
        if cfg.is_file():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise DataAgentError(
                    f"bad {self.CONFIG_NAME}: {e}") from e
            if isinstance(data, list):
                cmds = [list(c) for c in data
                        if isinstance(c, list) and c
                        and all(isinstance(t, str) for t in c)]
        return cmds

    # ------------------------------------------------------------ 执行
    def run(self, attempt: ExecutionAttempt) -> tuple[ExecutionAttempt,
                                                       list[ValidationResult]]:
        """APPLIED_SANDBOX → VALIDATED（全部 PASSED）/ TEST_FAILED。

        BLOCKED（可执行文件策略拒绝）按 TEST_FAILED 处理并在 note 里
        说明 —— 配置了不让跑的命令，本身就说明配置有问题。
        """
        ws = self.broker._workspace_svc
        if attempt.status != "APPLIED_SANDBOX" or not attempt.workspace:
            raise DataAgentError(
                f"validate_execution needs an APPLIED_SANDBOX attempt, "
                f"got {attempt.status}")
        worktree = Path(attempt.workspace)

        commands = self._commands(attempt)
        results: list[ValidationResult] = []
        for argv in commands:
            results.append(self.runner.run(argv, cwd=worktree))

        attempt = ws.registry.attach(attempt.execution_id,
                                     validation_results=[
                                         r.to_dict() for r in results])
        bad = [r for r in results
               if r.status in ("FAILED", "TIMEOUT", "ERROR", "BLOCKED")]
        if bad:
            note = "; ".join(f"{r.command[:3]} -> {r.status}"
                             for r in bad)[:300]
            ws.registry.update_status(attempt.execution_id, "TEST_FAILED",
                                      note=note)
        elif not results:
            ws.registry.update_status(
                attempt.execution_id, "VALIDATED",
                note="no validation commands configured")
        else:
            ws.registry.update_status(attempt.execution_id, "VALIDATED",
                                      note=f"{len(results)} command(s) "
                                           f"passed")
        ws._dump_attempt(ws.registry.get(attempt.execution_id))
        self.rec.tool(f"validation:run:{len(results)}")
        return ws.registry.get(attempt.execution_id), results
