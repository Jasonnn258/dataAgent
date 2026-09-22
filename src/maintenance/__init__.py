"""执行层（Phase 13）：Safe Execution Loop 的模型与沙箱工具。

纪律：Plan 决定应该发生什么；Executor 只执行 Plan；Verifier 判断实际
发生了什么；Policy 决定结果能否进入真实代码库。本包不碰源仓库的
任何写路径（promote 除外，且默认关闭）。
"""
