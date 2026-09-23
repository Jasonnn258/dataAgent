"""unified diff 解析器回归（Phase 12A 修复）。

旧解析器只按行首字符识别 body，两类真故障：
1. 删除文件段的 `+++ /dev/null` 被吞进上一个 hunk 的 body →
   PatchService 反向 patch 格式损坏（git apply 报 "fragment
   without header"）——真实 repo benchmark 上 zustand/express 两条
   回退任务全灭于此；
2. body 行内容以 `-- ` 开头（被删行内容就是 `-- foo`）被当文件头
   跳过 → hunk body 缺行。

修法：按 @@ 头声明的行数"喂饱"式识别；`\\ No newline` 修饰行不计
行数、原样保留。
"""
from src.git_history.api import _parse_unified_diff
from src.services.patch import PatchService
from src.git_history.api import Hunk


# 一段真实的混合 diff：文件 A 常规修改 + 文件 B 整文件删除（尾随
# +++ /dev/null）+ 文件 C 新增段 —— 旧解析器会在 B 段尾吞头
_MIXED = """\
diff --git a/src/a.js b/src/a.js
index 111..222 100644
--- a/src/a.js
+++ b/src/a.js
@@ -1,4 +1,4 @@
 line1
-old2
+new2
 line3
 line4
diff --git a/src/gone.js b/src/gone.js
deleted file mode 100644
index 333..000 100644
--- a/src/gone.js
+++ /dev/null
@@ -1,3 +0,0 @@
-gone1
-gone2
-gone3
diff --git a/src/c.js b/src/c.js
index 000..444 100644
--- /dev/null
+++ b/src/c.js
@@ -0,0 +1,2 @@
+c1
+c2
"""


def test_dev_null_header_not_swallowed_into_body():
    """删除文件段的 +++ /dev/null 不进任何 hunk 的 body。"""
    out = _parse_unified_diff(_MIXED)
    assert len(out["hunks"]) == 3
    for h in out["hunks"]:
        for tag, text in h.lines:
            assert "dev/null" not in text, (h.file, tag, text)
            assert tag in (" ", "+", "-", "\\")
    # 各 hunk 行数与 @@ 头声明一致（-1,4 +1,4 → 3 上下文+1删+1增=5；
    # 旧版这里会多吞 1 行 '+++ /dev/null' 变 6）
    assert len(out["hunks"][0].lines) == 5
    assert out["deleted_files"] == ["src/gone.js"]


def test_reversed_hunks_are_count_consistent():
    """反转后每个 hunk 的声明行数与实际 body 行数一致（可被 git apply 解析）。"""
    out = _parse_unified_diff(_MIXED)
    for h in out["hunks"]:
        lines = PatchService._reverse_hunk(h)
        header, body = lines[0], lines[1:]
        # @@ -new,newc +old,oldc @@：两侧计数 = 翻转后的 body 实数
        old_side = sum(1 for l in body if l[:1] in (" ", "-"))
        new_side = sum(1 for l in body if l[:1] in (" ", "+"))
        import re
        m = re.match(r"@@ -(\d+),(\d+) \+(\d+),(\d+) @@", header)
        assert m, header
        assert int(m.group(2)) == old_side, (header, old_side)
        assert int(m.group(4)) == new_side, (header, new_side)


def test_body_line_starting_with_dashes_stays_in_hunk():
    """body 行内容以 `-- ` 开头（删除行内容即 `-- foo`）不被误判为文件头。"""
    raw = """\
diff --git a/note.md b/note.md
index 111..222 100644
--- a/note.md
+++ b/note.md
@@ -1,3 +1,2 @@
 keep
--- dual dash line
 tail
"""
    out = _parse_unified_diff(raw)
    h = out["hunks"][0]
    assert h.old_lines == 3 and h.new_lines == 2
    # 完整保留（tag='-'，text 含原内容 '-- dual dash line'），没有被跳过
    assert ("-", "-- dual dash line") in h.lines


def test_no_newline_marker_kept_without_counting():
    """`\\ No newline at end of file` 保留在 body 里且不计入行数。"""
    raw = """\
diff --git a/tail.js b/tail.js
index 111..222 100644
--- a/tail.js
+++ b/tail.js
@@ -1,2 +1,2 @@
 head
-old no nl
\\ No newline at end of file
+new no nl
\\ No newline at end of file
"""
    out = _parse_unified_diff(raw)
    h = out["hunks"][0]
    tags = [t for t, _ in h.lines]
    # 计数行 = 3（1 上下文 + 1 删 + 1 增），修饰行不计
    assert tags.count("\\") == 2
    counted = [t for t in tags if t != "\\"]
    assert counted == [" ", "-", "+"]
    # 反转时修饰行原样跟随各自修饰的行（rev[0] 是 @@ 头，rev[1] 上下文）
    rev = PatchService._reverse_hunk(h)
    assert rev[2] == "+old no nl"
    assert rev[3].startswith("\\ ")
    assert rev[-1].startswith("\\ ")


def test_hunk_closes_on_count_then_next_header_recognized():
    """hunk 吃饱后，紧随的下一个 diff --git / +++ 头回到文件头语境。"""
    out = _parse_unified_diff(_MIXED)
    assert [h.file for h in out["hunks"]] == \
        ["src/a.js", "src/gone.js", "src/c.js"]
    assert [h.idx for h in out["hunks"]] == [0, 0, 0]
    # gone.js 是纯删除：反转后全部为 '+'（重建文件）
    gone = out["hunks"][1]
    rev = PatchService._reverse_hunk(gone)
    assert all(l[:1] == "+" for l in rev[1:])
