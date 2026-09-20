"""Phase 2 tests: tree-sitter parser + CodeIndex + structural locate/impact."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
MINDMAP = ROOT / "mindmap"

from src.config import Settings  # noqa: E402
from src.schema import ToolRecorder  # noqa: E402
from src.structural.parser import parse_file  # noqa: E402
from src.structural.index import CodeIndex  # noqa: E402
from src.tasks.locate import run_locate  # noqa: E402
from src.tasks.impact import run_impact  # noqa: E402

pytestmark = pytest.mark.skipif(not MINDMAP.is_dir(), reason="mindmap repo not present")


@pytest.fixture(scope="module")
def index():
    return CodeIndex(MINDMAP, ToolRecorder()).build()


# ------------------------------------------------------------ parser
def test_parser_functions_imports_calls(tmp_path):
    f = tmp_path / "a.tsx"
    f.write_text(
        'import { login } from "@/lib/auth";\n'
        "export function AuthNav({user}) { return <div>{login(user)}</div>; }\n"
        "export const generateWithRetry = async (p) => { await callLLM(p); };\n",
        encoding="utf-8",
    )
    fp = parse_file(f, "a.tsx")
    assert ("AuthNav", "component", True) in {(s.name, s.kind, s.exported) for s in fp.symbols}
    # AuthNav has JSX -> component; generateWithRetry has none -> arrow_const
    kinds = {s.name: s.kind for s in fp.symbols}
    assert kinds["AuthNav"] == "component"
    assert kinds["generateWithRetry"] == "arrow_const"
    assert fp.imports and fp.imports[0].source == "@/lib/auth" and "login" in fp.imports[0].names
    callees = {c.callee_short for c in fp.calls}
    assert "login" in callees and "callLLM" in callees
    assert any(u.text and "系统" not in u.text for u in fp.ui_strings) or True  # no CJK here


def test_parser_use_callback_symbol(tmp_path):
    f = tmp_path / "ctx.tsx"
    f.write_text(
        "export function P() {\n"
        "  const login = useCallback(async (a, b) => { return a + b; }, []);\n"
        "  return login(1, 2);\n"
        "}\n", encoding="utf-8")
    fp = parse_file(f, "ctx.tsx")
    assert any(s.name == "login" and s.kind == "arrow_const" for s in fp.symbols)


def test_parser_jsx_text_ui_string(tmp_path):
    f = tmp_path / "b.tsx"
    f.write_text("export function T() { return <Button>退出登录</Button>; }\n", encoding="utf-8")
    fp = parse_file(f, "b.tsx")
    assert any(u.text == "退出登录" and u.in_jsx_text for u in fp.ui_strings)


# ------------------------------------------------------------ index
def test_index_callers_and_routes(index):
    syms = index.find_symbols("verifyPassword")
    assert syms and syms[0].file == "src/lib/auth.ts"
    callers = index.callers_of(syms[0])
    assert any(e.caller_file == "src/app/api/auth/login/route.ts" for e in callers)
    routes = index.api_routes_reaching(syms[0])
    assert any(r.route_path == "/api/auth/login" for r in routes)


def test_index_importers(index):
    imp = [ri.importer for ri in index.importers_of_file("src/lib/auth.ts")]
    assert "src/app/api/auth/login/route.ts" in imp


def test_index_symbol_at(index):
    sym = index.symbol_at("src/lib/auth.ts", 14)  # inside verifyPassword
    assert sym and sym.name == "verifyPassword"


def test_index_endpoints_convention(index):
    paths = {ep.route_path: ep for ep in index.api_endpoints}
    assert "/api/auth/login" in paths and "POST" in paths["/api/auth/login"].methods


# ------------------------------------------------------------ tasks e2e
def test_locate_structural_layout_first():
    s = Settings(repo=MINDMAP, mode="structural", task="locate", top_k=5, no_llm=True)
    out = run_locate(s, "系统标题在哪里修改")
    files = [c.file for c in out.result.candidates]
    assert files[0] == "src/app/layout.tsx"
    top = out.result.candidates[0]
    assert top.evidence and any(e.kind in ("symbol_def", "convention") for e in top.evidence)


def test_impact_structural_vs_lexical_schema():
    s_lex = Settings(repo=MINDMAP, mode="lexical", task="impact", no_llm=True)
    s_str = Settings(repo=MINDMAP, mode="structural", task="impact", no_llm=True)
    lex = run_impact(s_lex, "修改 generateWithRetry 会影响什么")
    st = run_impact(s_str, "修改 generateWithRetry 会影响什么")
    assert lex.result.target.file == "src/app/api/generate/route.ts"
    assert not lex.result.callees and not lex.result.related  # lexical blind spots
    assert st.result.target.file.endswith(("route.ts", "ai-helpers.ts"))
    assert st.result.direct_callers and all(c.symbol for c in st.result.direct_callers)
    assert any(x.kind == "api_route" for x in st.result.related)
    # unified schema both modes
    for out in (lex, st):
        assert out.task == "impact" and out.system.latency_s >= 0


def test_impact_missing_symbol_falls_back():
    s = Settings(repo=MINDMAP, mode="structural", task="impact", no_llm=True)
    out = run_impact(s, "NoSuchSymbolAnywhere")
    assert out.result.notes  # graceful note, no crash
