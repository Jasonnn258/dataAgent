"""tree-sitter based TS/TSX/JS structural parser.

One pass per file collects:
- Symbol: functions / arrow-consts / classes / methods (kind incl. component
  heuristic for JSX-returning functions) with line ranges and export info
- ImportDecl: named/default/namespace imports with source specifier
- CallEdge: (innermost enclosing symbol) -[calls]-> textual callee name
- JSXRef: PascalCase component references in JSX
- UIString: CJK string literals *and* jsx_text (UI copy — key for locate/rollback)

Import resolution is repo-level and lives in index.py. tree-sitter is
error-tolerant: partial results are kept, ERROR nodes are counted and surfaced.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Parser

import tree_sitter_typescript as tst
import tree_sitter_javascript as tsjs

_LANGS: dict[str, Language] = {
    ".tsx": Language(tst.language_tsx()),
    ".ts": Language(tst.language_typescript()),
    ".jsx": Language(tsjs.language()),
    ".js": Language(tsjs.language()),
    ".mjs": Language(tsjs.language()),
    ".cjs": Language(tsjs.language()),
}
_PARSERS: dict[str, Parser] = {}

FUNCTION_DEF_NODES = {"function_declaration", "generator_function_declaration"}
METHOD_NODES = {"method_definition"}
CLASS_NODES = {"class_declaration", "class"}
JSX_REF_NODES = {"jsx_opening_element", "jsx_self_closing_element"}
# React idiom: const f = useCallback(() => {...}, []) and friends — the arrow
# inside the wrapper call is the function body.
WRAPPER_CALLEES = {"useCallback", "useMemo", "forwardRef", "memo", "lazy"}
HAS_CJK = re.compile(r"[一-鿿]")


def get_parser(suffix: str) -> Parser | None:
    if suffix not in _PARSERS:
        lang = _LANGS.get(suffix)
        if lang is None:
            return None
        _PARSERS[suffix] = Parser(lang)
    return _PARSERS[suffix]


@dataclass
class Symbol:
    name: str
    kind: str          # function | arrow_const | class | method | component | page_component
    file: str          # repo-relative posix path
    line_start: int    # 1-based inclusive
    line_end: int
    exported: bool = False
    params: str = ""   # raw parameter text, bounded
    snippet: str = ""  # signature / first line, bounded

    @property
    def qualified(self) -> str:
        return f"{self.file}::{self.name}"


@dataclass
class ImportDecl:
    file: str
    source: str                      # module specifier as written
    names: list[str] = field(default_factory=list)
    line: int = 0
    namespace: str | None = None


@dataclass
class CallEdge:
    caller: str        # qualified innermost enclosing symbol ("<module>" scope at top level)
    caller_file: str
    callee: str        # textual callee, e.g. 'login' | 'auth.verify'
    callee_short: str
    file: str
    line: int


@dataclass
class JSXRef:
    file: str
    line: int
    component: str


@dataclass
class UIString:
    file: str
    line: int
    text: str
    in_jsx_text: bool = False


@dataclass
class FileParse:
    file: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportDecl] = field(default_factory=list)
    calls: list[CallEdge] = field(default_factory=list)
    jsx_refs: list[JSXRef] = field(default_factory=list)
    ui_strings: list[UIString] = field(default_factory=list)
    error_nodes: int = 0
    export_names: list[str] = field(default_factory=list)  # `export {a, b}` lists


def parse_file(abs_path: Path, rel_path: str) -> FileParse | None:
    parser = get_parser(abs_path.suffix.lower())
    if parser is None:
        return None
    try:
        src = abs_path.read_bytes()
    except OSError:
        return None
    tree = parser.parse(src)
    fp = FileParse(file=rel_path)
    _walk(tree.root_node, src, fp, rel_path)
    fp.error_nodes = _count_errors(tree.root_node)
    return fp


# ------------------------------------------------------------------ helpers

def _text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _count_errors(node) -> int:
    n, stack = 0, [node]
    while stack:
        cur = stack.pop()
        if cur.type == "ERROR" or cur.is_missing:
            n += 1
        stack.extend(cur.children)
    return n


def _qualify(rel: str, func_stack: list[str], name: str) -> str:
    if func_stack:
        return f"{func_stack[-1]}.{name}"
    return f"{rel}::{name}"


def _module_scope(rel: str, func_stack: list[str]) -> str:
    return func_stack[-1] if func_stack else f"{rel}::<module>"


def _first_line(src: bytes, node) -> str:
    lines = src.split(b"\n")
    row = node.start_point.row
    return lines[row].decode("utf-8", "replace").strip() if row < len(lines) else ""


def _descendants(node, type_name: str):
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == type_name:
            yield cur
        stack.extend(cur.named_children)


def _has_jsx(node) -> bool:
    stack, seen = [node], 0
    while stack and seen < 4000:
        cur = stack.pop()
        seen += 1
        if cur.type in ("jsx_element", "jsx_self_closing_element", "jsx_fragment"):
            return True
        stack.extend(cur.named_children)
    return False


# ------------------------------------------------------------------ walker
#
# Iterative traversal (explicit worklist). The recursive version segfaulted
# on deep JSX (8676-line page.tsx) — Python 3.11 + tree-sitter C bindings
# overflow the C stack before RecursionError fires, so we never recurse.

def _walk(root, src: bytes, fp: FileParse, rel: str) -> None:
    # worklist items: (node, func_stack, exported, class_name)
    stack: list[tuple[object, tuple[str, ...], bool, str | None]] = [(root, (), False, None)]
    while stack:
        node, func_stack, exported, class_name = stack.pop()
        t = node.type

        if t == "export_statement":
            for c in node.named_children:
                if c.type == "export_clause":               # export { a as b }
                    for sp in _descendants(c, "import_specifier"):
                        idents = [x for x in sp.named_children if x.type == "identifier"]
                        if idents:
                            nm = _text(src, idents[-1])      # exported alias if present
                            if nm:
                                fp.export_names.append(nm)
                else:                                       # export <declaration>
                    stack.append((c, func_stack, True, class_name))
            continue

        if t == "import_statement":
            fp.imports.append(_parse_import(node, src, rel))
            continue

        if t == "call_expression":
            fn = node.named_children[0] if node.named_children else None
            if fn is not None:
                callee_txt = _text(src, fn)
                if len(callee_txt) <= 80:
                    fs = list(func_stack)
                    fp.calls.append(CallEdge(
                        caller=_module_scope(rel, fs), caller_file=rel,
                        callee=callee_txt, callee_short=callee_txt.split(".")[-1],
                        file=rel, line=node.start_point.row + 1))
            _push_children(node, stack, func_stack, exported, class_name)
            continue

        if t in JSX_REF_NODES:
            first = node.named_children[0] if node.named_children else None
            if first is not None and first.type == "identifier":
                nm = _text(src, first)
                if nm[:1].isupper():
                    fp.jsx_refs.append(JSXRef(file=rel, line=node.start_point.row + 1,
                                              component=nm))
            for c in node.named_children[1:]:
                stack.append((c, func_stack, exported, class_name))
            continue

        if t in FUNCTION_DEF_NODES:
            name_node = next((c for c in node.named_children if c.type == "identifier"), None)
            if name_node is not None:
                nm = _text(src, name_node)
                params = next((_text(src, c) for c in node.named_children
                               if c.type == "formal_parameters"), "")
                kind = "function"
                if _has_jsx(node):
                    kind = "component"
                sym = Symbol(name=nm, kind=kind, file=rel,
                             line_start=node.start_point.row + 1,
                             line_end=node.end_point.row + 1,
                             exported=exported, params=params[:150],
                             snippet=_first_line(src, node)[:200])
                fp.symbols.append(sym)
                _push_children(node, stack, func_stack + (sym.qualified,), False, class_name)
            else:  # anonymous (export default function () {})
                _push_children(node, stack, func_stack, exported, class_name)
            continue

        if t == "variable_declarator":
            name_node = node.named_children[0] if node.named_children else None
            value = next((c for c in node.named_children
                          if c.type not in ("identifier", "type_annotation")), None)
            inner_fn = None
            if value is not None and value.type in ("arrow_function", "function_expression"):
                inner_fn = value
            elif value is not None and value.type == "call_expression":
                callee = value.named_children[0] if value.named_children else None
                callee_txt = _text(src, callee).split(".")[-1] if callee is not None else ""
                if callee_txt in WRAPPER_CALLEES:
                    arrows = list(_descendants(value, "arrow_function"))
                    fns = list(_descendants(value, "function_expression"))
                    inner_fn = arrows[0] if arrows else (fns[0] if fns else None)
            if name_node is not None and name_node.type == "identifier" and inner_fn is not None:
                nm = _text(src, name_node)
                kind = "arrow_const" if not _has_jsx(value) else "component"
                q = _qualify(rel, list(func_stack), nm)
                sym = Symbol(name=nm, kind=kind, file=rel,
                             line_start=node.start_point.row + 1,
                             line_end=node.end_point.row + 1,
                             exported=exported, snippet=_first_line(src, node)[:200])
                fp.symbols.append(sym)
                _push_children(node, stack, func_stack + (q,), False, class_name)
                continue
            _push_children(node, stack, func_stack, exported, class_name)
            continue

        if t in CLASS_NODES:
            name_node = next((c for c in node.named_children if c.type == "identifier"), None)
            if name_node is not None:
                nm = _text(src, name_node)
                sym = Symbol(name=nm, kind="class", file=rel,
                             line_start=node.start_point.row + 1,
                             line_end=node.end_point.row + 1,
                             exported=exported, snippet=_first_line(src, node)[:200])
                fp.symbols.append(sym)
                _push_children(node, stack, func_stack, False, nm)
            else:
                _push_children(node, stack, func_stack, exported, class_name)
            continue

        if t in METHOD_NODES:
            name_node = next((c for c in node.named_children
                              if c.type == "property_identifier"), None)
            if name_node is not None:
                nm = _text(src, name_node)
                fs = list(func_stack)
                q = _qualify(rel, fs, nm) if fs else (
                    f"{rel}::{class_name}.{nm}" if class_name else f"{rel}::{nm}")
                sym = Symbol(name=nm, kind="method", file=rel,
                             line_start=node.start_point.row + 1,
                             line_end=node.end_point.row + 1,
                             exported=exported, snippet=_first_line(src, node)[:200])
                fp.symbols.append(sym)
                _push_children(node, stack, func_stack + (q,), False, class_name)
            else:
                _push_children(node, stack, func_stack, exported, class_name)
            continue

        if t in ("string", "template_string"):
            txt = _text(src, node)
            if HAS_CJK.search(txt) and len(txt) <= 200:
                fp.ui_strings.append(UIString(file=rel, line=node.start_point.row + 1,
                                              text=txt.strip("`'\"")[:150]))
            _push_children(node, stack, func_stack, exported, class_name)
            continue

        if t == "jsx_text":
            txt = _text(src, node).strip()
            if HAS_CJK.search(txt) and 2 <= len(txt) <= 150:
                fp.ui_strings.append(UIString(file=rel, line=node.start_point.row + 1,
                                              text=txt, in_jsx_text=True))
            continue

        _push_children(node, stack, func_stack, exported, class_name)


def _push_children(node, stack, func_stack, exported, class_name):
    """Push named children in source order (LIFO stack -> push reversed)."""
    for c in reversed(node.named_children):
        stack.append((c, func_stack, exported, class_name))


def _parse_import(node, src, rel) -> ImportDecl:
    imp = ImportDecl(file=rel, source="", line=node.start_point.row + 1)
    source_node = next((c for c in node.named_children if c.type == "string"), None)
    if source_node is not None:
        imp.source = _text(src, source_node).strip("'\"`")
    clause = next((c for c in node.named_children if c.type == "import_clause"), None)
    if clause is not None:
        for part in clause.named_children:
            if part.type == "identifier":                    # default import
                imp.names.append(_text(src, part))
            elif part.type == "namespace_import":
                ident = next((x for x in part.named_children if x.type == "identifier"), None)
                if ident is not None:
                    imp.namespace = _text(src, ident)
            elif part.type == "named_imports":
                for sp in sps(part):
                    idents = [x for x in sp.named_children if x.type == "identifier"]
                    if idents:
                        nm = _text(src, idents[-1])          # local name (alias if `a as b`)
                        if nm and nm not in imp.names:
                            imp.names.append(nm)
    return imp


def sps(named_imports_node):
    for c in named_imports_node.named_children:
        if c.type == "import_specifier":
            yield c
