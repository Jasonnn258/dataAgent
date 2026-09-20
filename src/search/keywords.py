"""Query term extraction + bilingual expansion.

Deterministic, shared by all modes (mode ablation varies the *context layers*,
not query understanding). Strategy:
1. CJK runs are segmented by function words -> content segments (verbatim terms)
2. known dictionary terms inside segments expand to English code-side patterns
3. English words (plus camelCase parts) are kept minus stopwords
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_DICT_PATH = Path(__file__).parent / "zh_en_terms.json"
with open(_DICT_PATH, encoding="utf-8") as _f:
    _DICT = json.load(_f)

CJK_TERM_MAP: dict[str, list[str]] = _DICT["terms"]
FUNCTION_WORDS: list[str] = sorted(_DICT["function_words"], key=len, reverse=True)

EN_STOPWORDS = {
    "where", "what", "how", "which", "who", "when", "is", "are", "was", "were",
    "do", "does", "did", "the", "a", "an", "of", "in", "on", "for", "to", "at",
    "by", "with", "and", "or", "not", "no", "i", "we", "you", "it", "this",
    "that", "these", "those", "my", "our", "your", "its", "change", "changed",
    "modify", "modified", "edit", "locate", "find", "show", "me", "please",
    "code", "file", "files", "function", "functions", "class", "classes",
}

CJK_RE = re.compile(r"[一-鿿㐀-䶿]+")
WORD_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


@dataclass
class QueryTerms:
    raw: str
    cjk_segments: list[str] = field(default_factory=list)   # content segments, verbatim searchable
    cjk_subterms: list[str] = field(default_factory=list)   # dict keys found inside segments
    en_terms: list[str] = field(default_factory=list)       # english words incl. camel parts
    expansions: dict[str, list[str]] = field(default_factory=dict)  # zh term -> en patterns

    def all_search_terms(self) -> list[tuple[str, str]]:
        """(term, origin) pairs actually searched: verbatim CJK + subterms + expansions + EN."""
        out = []
        for seg in self.cjk_segments:
            out.append((seg, f"cjk:{seg}"))
        for sub in self.cjk_subterms:
            out.append((sub, f"cjk_sub:{sub}"))
        for seg, pats in self.expansions.items():
            for p in pats:
                out.append((p, f"expand:{seg}"))
        for t in self.en_terms:
            out.append((t, f"en:{t}"))
        return out

    def summary(self) -> str:
        parts = []
        if self.cjk_segments:
            parts.append("cjk=" + ",".join(self.cjk_segments))
        if self.cjk_subterms:
            parts.append("sub=" + ",".join(self.cjk_subterms))
        if self.expansions:
            parts.append("expand=" + ",".join(f"{k}->{','.join(v)}" for k, v in self.expansions.items()))
        if self.en_terms:
            parts.append("en=" + ",".join(self.en_terms))
        return "; ".join(parts) or "(none)"


def _split_by_function_words(run: str) -> list[str]:
    """Cut a CJK run at function words; keep non-empty residues >=2 chars."""
    pattern = "|".join(re.escape(w) for w in FUNCTION_WORDS)
    residues = [r for r in re.split(pattern, run) if len(r) >= 2]
    return residues or ([run] if len(run) >= 2 else [])


def extract_terms(query: str) -> QueryTerms:
    qt = QueryTerms(raw=query)

    for run in CJK_RE.findall(query):
        for seg in _split_by_function_words(run):
            if seg not in qt.cjk_segments:
                qt.cjk_segments.append(seg)

    # dictionary expansion: known zh terms appearing anywhere in a segment.
    # matched keys also become verbatim subterms (系统标题 -> 系统,标题 verbatim),
    # which lets CJK UI strings ("SUN ...教学设计系统") hit directly.
    for seg in qt.cjk_segments:
        for zh, en_pats in CJK_TERM_MAP.items():
            if zh in seg or seg == zh:
                qt.expansions.setdefault(seg, [])
                for p in en_pats:
                    if p.lower() not in [x.lower() for x in qt.expansions[seg]]:
                        qt.expansions[seg].append(p)
                if zh not in qt.cjk_subterms:
                    qt.cjk_subterms.append(zh)

    for w in WORD_RE.findall(query):
        parts = _camel_parts(w)
        for p in parts:
            if p.lower() not in EN_STOPWORDS and len(p) >= 2 and p not in qt.en_terms:
                qt.en_terms.append(p)
    return qt


def _camel_parts(word: str) -> list[str]:
    parts = [p for p in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+", word) if p]
    # keep the original token too when it was camelCase (generateWithRetry)
    if len(parts) > 1:
        parts.append(word)
    return parts
