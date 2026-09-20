"""Repo-level structural code index (TS/TSX/JS first stage).

Combines per-file parses into:
- symbol table (by name, by file, by line)
- resolved import graph (file -> file, respecting '@/alias' -> src/)
- call graph (resolved by callee name -> unique definition when possible)
- Next.js App Router conventions: app/**/route.ts = API endpoint,
  app/**/page.tsx = page component, app/layout.tsx = root layout
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from src.errors import ParseError
from src.schema import ToolRecorder
from src.search.lexical import IGNORE_DIRS, SKIP_FILENAMES, MAX_FILE_BYTES
from src.structural.parser import (CallEdge, FileParse, Symbol, UIString,
                                   parse_file)

CODE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
RESOLVE_SUFFIXES = ["", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx"]
HTTP_HANDLER_NAMES = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


@dataclass
class APIEndpoint:
    file: str
    route_path: str      # inferred URL path from app-dir location
    methods: list[str] = field(default_factory=list)
    handlers: list[str] = field(default_factory=list)  # qualified handler symbols


@dataclass
class ResolvedImport:
    importer: str        # file
    source_file: str     # resolved file ('' if external/unresolved)
    specifier: str
    names: list[str] = field(default_factory=list)


class CodeIndex:
    def __init__(self, repo: Path, rec: ToolRecorder | None = None):
        self.repo = repo
        self.rec = rec
        self.files: list[str] = []
        self.parses: dict[str, FileParse] = {}
        self.symbols: list[Symbol] = []
        self.symbols_by_name: dict[str, list[Symbol]] = defaultdict(list)
        self.symbols_by_file: dict[str, list[Symbol]] = defaultdict(list)
        self.ui_strings: list[UIString] = []
        self.import_edges: list[ResolvedImport] = []
        self.importers_of: dict[str, list[ResolvedImport]] = defaultdict(list)
        self.calls: list[CallEdge] = []
        self.api_endpoints: list[APIEndpoint] = []
        self.pages: list[Symbol] = []
        self.root_layouts: list[Symbol] = []

    # ------------------------------------------------------------ build
    def build(self) -> "CodeIndex":
        if self.rec:
            self.rec.tool("structural:index_build")
        code_files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.repo):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                if fn in SKIP_FILENAMES or p.suffix.lower() not in CODE_SUFFIXES:
                    continue
                try:
                    if p.stat().st_size > MAX_FILE_BYTES * 4:
                        continue
                except OSError:
                    continue
                code_files.append(p)

        for p in sorted(code_files):
            rel = p.relative_to(self.repo).as_posix()
            fp = parse_file(p, rel)
            if fp is None:
                continue
            self.files.append(rel)
            self.parses[rel] = fp
            self.symbols.extend(fp.symbols)
            self.ui_strings.extend(fp.ui_strings)
            self.calls.extend(fp.calls)

        for s in self.symbols:
            self.symbols_by_name[s.name].append(s)
            self.symbols_by_file[s.file].append(s)

        self._resolve_imports()
        self._extract_routes()
        if self.rec and any(fp.error_nodes for fp in self.parses.values()):
            n = sum(fp.error_nodes for fp in self.parses.values())
            self.rec.warn(f"structural parse: {n} ERROR nodes tolerated across repo")
        return self

    # ------------------------------------------------------------ imports
    def _resolve_imports(self) -> None:
        for rel, fp in self.parses.items():
            for imp in fp.imports:
                src_file = self._resolve_specifier(rel, imp.source)
                ri = ResolvedImport(importer=rel, source_file=src_file,
                                    specifier=imp.source, names=list(imp.names))
                self.import_edges.append(ri)
                if src_file:
                    self.importers_of[src_file].append(ri)

    def _resolve_specifier(self, from_file: str, spec: str) -> str:
        if not spec or spec.startswith(("http:", "https:", "node:")):
            return ""
        candidates: list[Path] = []
        if spec.startswith("."):
            base = (self.repo / from_file).parent / spec
            candidates.append(base)
        elif spec.startswith("@/"):
            for root in ("src", "."):
                candidates.append(self.repo / root / spec[2:])
        else:
            return ""  # bare package import -> external
        seen = set()
        for cand in candidates:
            for suf in RESOLVE_SUFFIXES:
                p = Path(str(cand) + suf)
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                if p.is_file():
                    try:
                        return p.resolve().relative_to(self.repo.resolve()).as_posix()
                    except ValueError:
                        return ""
        return ""

    # ------------------------------------------------------------ routes
    def _extract_routes(self) -> None:
        """Next.js App Router conventions: <app>/.../route.ts = endpoint,
        .../page.tsx = page, .../layout.tsx = layout."""
        for rel, fp in self.parses.items():
            parts = Path(rel).parts
            name = parts[-1] if parts else ""
            app_idx = next((i for i, p in enumerate(parts[:-1]) if p == "app"), None)
            if app_idx is None:
                continue
            segs = parts[app_idx + 1:-1]  # URL segments between app/ and the file
            if name == "route.ts":
                handlers = [s for s in fp.symbols if s.name in HTTP_HANDLER_NAMES]
                self.api_endpoints.append(APIEndpoint(
                    file=rel,
                    route_path="/" + "/".join(segs).rstrip("/") or "/",
                    methods=[h.name for h in handlers],
                    handlers=[h.qualified for h in handlers]))
            elif name == "page.tsx":
                for s in fp.symbols:
                    if s.kind == "component":
                        self.pages.append(s)
                        break
            elif name == "layout.tsx":
                self.root_layouts.extend(
                    s for s in fp.symbols if s.kind in ("function", "component"))

    # ------------------------------------------------------------ queries
    def find_symbols(self, name: str) -> list[Symbol]:
        return self.symbols_by_name.get(name, [])

    def symbol_at(self, file: str, line: int) -> Symbol | None:
        """Innermost symbol whose range contains (file, line)."""
        best = None
        for s in self.symbols_by_file.get(file, []):
            if s.line_start <= line <= s.line_end:
                if best is None or (s.line_end - s.line_start) < (best.line_end - best.line_start):
                    best = s
        return best

    def callers_of(self, sym: Symbol) -> list[CallEdge]:
        """Call edges whose callee resolves to this symbol.

        Resolution tiers: same-file name match (strong) > name imported into
        caller file (strong) > name globally unique in repo (medium — covers
        hook-destructured names like `const {login} = useAuth()`).
        """
        out: list[CallEdge] = []
        globally_unique = len(self.symbols_by_name.get(sym.name, [])) == 1
        for e in self.calls:
            if e.callee_short != sym.name:
                continue
            if e.caller_file == sym.file:
                out.append(e)
            elif _name_imported_into(self.import_edges, e.caller_file, sym.name):
                out.append(e)
            elif globally_unique:
                out.append(e)
        return out

    def jsx_callers_of(self, sym: Symbol) -> list[tuple[str, int]]:
        """Files+lines rendering this component (JSX references)."""
        if sym.kind != "component":
            return []
        out = []
        for fp in self.parses.values():
            for r in fp.jsx_refs:
                if r.component != sym.name or r.file == sym.file:
                    continue
                if _name_imported_into(self.import_edges, r.file, sym.name) or \
                        len(self.symbols_by_name.get(sym.name, [])) == 1:
                    out.append((r.file, r.line))
        return out

    def callees_of(self, sym: Symbol) -> list[CallEdge]:
        prefix = sym.qualified + "."
        out = []
        for e in self.calls:
            if e.caller == sym.qualified or e.caller.startswith(prefix):
                out.append(e)
        return out

    def importers_of_file(self, file: str) -> list[ResolvedImport]:
        return self.importers_of.get(file, [])

    def indirect_callers(self, sym: Symbol, depth: int = 2) -> dict[str, list[str]]:
        """BFS up the call graph: qualified caller -> chain of call sites."""
        seen: dict[str, list[str]] = {}
        frontier = [(sym.qualified, [])]
        for _ in range(depth):
            nxt = []
            for q, chain in frontier:
                for s in self.symbols_by_name.get(q.split("::")[-1].split(".")[-1], []):
                    for e in self.callers_of(s):
                        if e.caller in seen or e.caller == sym.qualified:
                            continue
                        seen[e.caller] = chain + [f"{e.file}:{e.line}"]
                        nxt.append((e.caller, seen[e.caller]))
            frontier = nxt
        return seen

    def api_routes_reaching(self, sym: Symbol) -> list[APIEndpoint]:
        """API endpoints whose handler closure contains the symbol (2-hop)."""
        reach = {sym.qualified} | set(self.indirect_callers(sym, 2).keys())
        out = []
        for ep in self.api_endpoints:
            if any(h in reach for h in ep.handlers):
                out.append(ep)
        return out

    def tests_referencing(self, sym: Symbol) -> list[ResolvedImport]:
        return [ri for ri in self.importers_of_file(sym.file)
                if any(k in ri.importer for k in ("test", "spec", "__tests__"))]

    def search_ui_strings(self, substr: str) -> list[UIString]:
        return [u for u in self.ui_strings if substr in u.text]


def _name_imported_into(edges: list[ResolvedImport], into_file: str, name: str) -> bool:
    for ri in edges:
        if ri.importer == into_file and name in ri.names:
            return True
    return False
