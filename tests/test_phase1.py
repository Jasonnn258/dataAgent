"""Phase 1 tests: lexical baseline (keyword extraction, search, locate e2e)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MINDMAP = ROOT / "mindmap"

from src.search.keywords import extract_terms  # noqa: E402
from src.config import Settings  # noqa: E402
from src.tasks.locate import run_locate  # noqa: E402
from src.schema import TaskOutput  # noqa: E402

pytestmark = pytest.mark.skipif(not MINDMAP.is_dir(), reason="mindmap repo not present")


# ------------------------------------------------------------ keywords
def test_keyword_extraction_cn():
    qt = extract_terms("系统标题在哪里修改")
    assert "系统标题" in qt.cjk_segments
    assert set(qt.cjk_subterms) >= {"系统", "标题"}
    assert "title" in qt.expansions.get("系统标题", []) or \
        any("title" in v for v in qt.expansions.values())


def test_keyword_extraction_mixed():
    qt = extract_list = extract_terms("修改 generateWithRetry 会影响什么")
    assert "generateWithRetry" in extract_list.en_terms
    assert "generate" in extract_list.en_terms  # camel parts kept


def test_keyword_extraction_login():
    qt = extract_terms("登录验证的逻辑写在哪里")
    assert any("login" in v for v in qt.expansions.values())
    assert any("auth" in v for v in qt.expansions.values())


# ------------------------------------------------------------ locate e2e
def _locate(query, mode="lexical", top_k=10, repo=MINDMAP):
    s = Settings(repo=repo, mode=mode, task="locate", top_k=top_k, no_llm=True)
    out = run_locate(s, query)
    assert isinstance(out, TaskOutput)
    return out


def test_locate_login_logic_top3():
    out = _locate("登录验证的逻辑写在哪里")
    files = [c.file for c in out.result.candidates]
    assert any("api/auth/login" in f for f in files[:3])


def test_locate_title_layout_in_top10():
    out = _locate("系统标题在哪里修改")
    files = [c.file for c in out.result.candidates]
    assert "src/app/layout.tsx" in files[:10]


def test_locate_generate_image_route_top3():
    out = _locate("生成图片的功能在哪里")
    files = [c.file for c in out.result.candidates]
    assert any("generate-image" in f for f in files[:3])


def test_locate_evidence_attached():
    out = _locate("登录验证的逻辑写在哪里")
    assert out.result.candidates
    top = out.result.candidates[0]
    assert top.evidence and top.evidence[0].source.count(":") >= 1
    assert top.reason


def test_locate_metrics_recorded():
    out = _locate("系统标题在哪里修改")
    m = out.system
    assert m.mode == "lexical" and m.latency_s >= 0
    assert m.tool_call_count >= 1
    assert m.retrieved_context_chars > 0
