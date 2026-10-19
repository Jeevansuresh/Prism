"""Language configuration for the tree-sitter parser.

One :class:`LangSpec` per language, plus a per-language import extractor. The
walker in :mod:`prism.parse` is generic; everything language-specific lives
here, so adding a language is a spec plus one function rather than a new code
path.

Grammars are optional dependencies. A missing grammar makes its language
unavailable -- files in it are recorded as File nodes with no Symbols, never a
crash.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

__all__ = [
    "LangSpec", "SPECS", "spec_for_path", "language_for_path", "load_language",
    "available_languages", "RawImport", "is_builtin_call", "stdlib_modules",
]


@dataclass(frozen=True)
class RawImport:
    """One import statement, before resolution to a file or a technology."""

    module: str              # as written: "fastapi", "./tokens", "app.db.queries"
    names: tuple[str, ...]   # imported symbol names, where the grammar gives them
    line: int
    is_relative: bool = False
    level: int = 0           # number of leading dots, Python relative imports


@dataclass(frozen=True)
class LangSpec:
    """How to find symbols, calls and imports in one language."""

    name: str
    module: str                                  # tree-sitter package module
    extensions: tuple[str, ...]
    language_attr: str = "language"              # callable on the module

    class_types: frozenset[str] = field(default_factory=frozenset)
    function_types: frozenset[str] = field(default_factory=frozenset)
    interface_types: frozenset[str] = field(default_factory=frozenset)
    enum_types: frozenset[str] = field(default_factory=frozenset)
    struct_types: frozenset[str] = field(default_factory=frozenset)

    call_types: frozenset[str] = field(default_factory=frozenset)
    call_function_field: str = "function"
    #: Member/attribute access nodes, for ``obj.method()`` callee names.
    accessor_types: frozenset[str] = field(default_factory=frozenset)
    accessor_name_field: str = "attribute"
    #: Grammars where the call node names the callee directly (Java).
    call_name_field: str = ""

    import_types: frozenset[str] = field(default_factory=frozenset)
    import_extractor: Callable[[Any], RawImport | None] | None = None

    decorator_types: frozenset[str] = field(default_factory=frozenset)
    #: Node wrapping a decorated definition, if the grammar has one.
    decorated_wrapper: str = ""

    #: Relative-import prefixes, used to decide internal vs external.
    relative_prefixes: tuple[str, ...] = ()
    #: Candidate file suffixes when resolving an internal import to a path.
    resolve_suffixes: tuple[str, ...] = ()
    #: Candidate index filenames inside a directory import.
    index_names: tuple[str, ...] = ()

    def kind_for(self, node_type: str, inside_class: bool) -> str:
        if node_type in self.class_types:
            return "class"
        if node_type in self.interface_types:
            return "interface"
        if node_type in self.enum_types:
            return "enum"
        if node_type in self.struct_types:
            return "struct"
        if node_type in self.function_types:
            return "method" if inside_class else "function"
        return "function"

    @property
    def definition_types(self) -> frozenset[str]:
        return (
            self.class_types | self.function_types | self.interface_types
            | self.enum_types | self.struct_types
        )


def _text(node: Any) -> str:
    if node is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _line(node: Any) -> int:
    return node.start_point[0] + 1


# --------------------------------------------------------------------------
# Import extractors
# --------------------------------------------------------------------------

def _python_import(node: Any) -> RawImport | None:
    """``import a.b`` / ``from .x import y`` / ``from a.b import c, d``."""
    if node.type == "import_statement":
        modules: list[str] = []
        for child in node.named_children:
            if child.type == "dotted_name":
                modules.append(_text(child))
            elif child.type == "aliased_import":
                name = child.child_by_field_name("name")
                if name is not None:
                    modules.append(_text(name))
        if not modules:
            return None
        return RawImport(module=modules[0], names=tuple(modules), line=_line(node))

    if node.type == "import_from_statement":
        module_node = node.child_by_field_name("module_name")
        raw = _text(module_node)
        level = 0
        module = raw
        if module_node is not None and module_node.type == "relative_import":
            # "..pkg.mod" -> level 2, module "pkg.mod"
            stripped = raw.lstrip(".")
            level = len(raw) - len(stripped)
            module = stripped
        names: list[str] = []
        for child in node.named_children:
            if child is module_node:
                continue
            if child.type == "dotted_name":
                names.append(_text(child))
            elif child.type == "aliased_import":
                inner = child.child_by_field_name("name")
                if inner is not None:
                    names.append(_text(inner))
            elif child.type == "wildcard_import":
                names.append("*")
        return RawImport(
            module=module,
            names=tuple(names),
            line=_line(node),
            is_relative=level > 0,
            level=level,
        )
    return None


def _js_import(node: Any) -> RawImport | None:
    """ES module ``import ... from "x"`` and bare ``import "x"``."""
    source = node.child_by_field_name("source")
    if source is None:
        for child in node.named_children:
            if child.type == "string":
                source = child
                break
    if source is None:
        return None
    module = _text(source).strip("\"'`")
    if not module:
        return None
    names = tuple(
        _text(n.child_by_field_name("name") or n)
        for n in node.named_children
        if n.type == "import_specifier"
    )
    return RawImport(
        module=module,
        names=names,
        line=_line(node),
        is_relative=module.startswith("."),
    )


def _go_import(node: Any) -> RawImport | None:
    """``import "net/http"`` and grouped import blocks.

    The grouped form yields one RawImport per spec; the walker calls this for
    each ``import_spec`` node, so only the single-spec case is handled here.
    """
    path_node = node.child_by_field_name("path")
    if path_node is None:
        for child in node.named_children:
            if child.type in {"interpreted_string_literal", "raw_string_literal"}:
                path_node = child
                break
    if path_node is None:
        return None
    module = _text(path_node).strip("\"'`")
    if not module:
        return None
    return RawImport(module=module, names=(), line=_line(node))


def _java_import(node: Any) -> RawImport | None:
    """``import com.example.Thing;`` -- the package path is the module."""
    raw = _text(node).strip()
    raw = raw.removeprefix("import").strip().rstrip(";").strip()
    raw = raw.removeprefix("static").strip()
    if not raw:
        return None
    parts = raw.split(".")
    # The trailing element is a type or a wildcard, not part of the package.
    module = ".".join(parts[:-1]) if len(parts) > 1 else raw
    return RawImport(module=module, names=(parts[-1],), line=_line(node))


# --------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------

_JS_FUNCTIONS = frozenset(
    {
        "function_declaration", "generator_function_declaration",
        "method_definition", "function_expression", "arrow_function",
    }
)
_JS_CALLS = frozenset({"call_expression", "new_expression"})
_JS_ACCESSORS = frozenset({"member_expression"})

PYTHON = LangSpec(
    name="python",
    module="tree_sitter_python",
    extensions=(".py", ".pyi"),
    class_types=frozenset({"class_definition"}),
    function_types=frozenset({"function_definition"}),
    call_types=frozenset({"call"}),
    call_function_field="function",
    accessor_types=frozenset({"attribute"}),
    accessor_name_field="attribute",
    import_types=frozenset({"import_statement", "import_from_statement"}),
    import_extractor=_python_import,
    decorator_types=frozenset({"decorator"}),
    decorated_wrapper="decorated_definition",
    relative_prefixes=(".",),
    resolve_suffixes=(".py", ".pyi"),
    index_names=("__init__.py",),
)

JAVASCRIPT = LangSpec(
    name="javascript",
    module="tree_sitter_javascript",
    extensions=(".js", ".jsx", ".mjs", ".cjs"),
    class_types=frozenset({"class_declaration", "class"}),
    function_types=_JS_FUNCTIONS,
    call_types=_JS_CALLS,
    call_function_field="function",
    accessor_types=_JS_ACCESSORS,
    accessor_name_field="property",
    import_types=frozenset({"import_statement"}),
    import_extractor=_js_import,
    decorator_types=frozenset({"decorator"}),
    relative_prefixes=(".", "/"),
    resolve_suffixes=(".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"),
    index_names=("index.js", "index.jsx", "index.ts", "index.tsx"),
)

TYPESCRIPT = LangSpec(
    name="typescript",
    module="tree_sitter_typescript",
    language_attr="language_typescript",
    extensions=(".ts", ".mts", ".cts"),
    class_types=frozenset({"class_declaration", "abstract_class_declaration", "class"}),
    function_types=_JS_FUNCTIONS | frozenset({"function_signature", "method_signature"}),
    interface_types=frozenset({"interface_declaration"}),
    enum_types=frozenset({"enum_declaration"}),
    call_types=_JS_CALLS,
    call_function_field="function",
    accessor_types=_JS_ACCESSORS,
    accessor_name_field="property",
    import_types=frozenset({"import_statement"}),
    import_extractor=_js_import,
    decorator_types=frozenset({"decorator"}),
    relative_prefixes=(".", "/"),
    resolve_suffixes=(".ts", ".tsx", ".mts", ".cts", ".js", ".jsx"),
    index_names=("index.ts", "index.tsx", "index.js"),
)

TSX = LangSpec(
    name="tsx",
    module="tree_sitter_typescript",
    language_attr="language_tsx",
    extensions=(".tsx",),
    class_types=TYPESCRIPT.class_types,
    function_types=TYPESCRIPT.function_types,
    interface_types=TYPESCRIPT.interface_types,
    enum_types=TYPESCRIPT.enum_types,
    call_types=_JS_CALLS,
    accessor_types=_JS_ACCESSORS,
    accessor_name_field="property",
    import_types=frozenset({"import_statement"}),
    import_extractor=_js_import,
    decorator_types=frozenset({"decorator"}),
    relative_prefixes=(".", "/"),
    resolve_suffixes=TYPESCRIPT.resolve_suffixes,
    index_names=TYPESCRIPT.index_names,
)

GO = LangSpec(
    name="go",
    module="tree_sitter_go",
    extensions=(".go",),
    function_types=frozenset({"function_declaration", "method_declaration"}),
    struct_types=frozenset({"type_spec"}),
    call_types=frozenset({"call_expression"}),
    call_function_field="function",
    accessor_types=frozenset({"selector_expression"}),
    accessor_name_field="field",
    import_types=frozenset({"import_spec"}),
    import_extractor=_go_import,
    resolve_suffixes=(".go",),
)

JAVA = LangSpec(
    name="java",
    module="tree_sitter_java",
    extensions=(".java",),
    class_types=frozenset({"class_declaration", "record_declaration"}),
    function_types=frozenset({"method_declaration", "constructor_declaration"}),
    interface_types=frozenset({"interface_declaration"}),
    enum_types=frozenset({"enum_declaration"}),
    call_types=frozenset({"method_invocation", "object_creation_expression"}),
    call_name_field="name",
    accessor_types=frozenset(),
    import_types=frozenset({"import_declaration"}),
    import_extractor=_java_import,
    decorator_types=frozenset({"annotation", "marker_annotation"}),
    resolve_suffixes=(".java",),
)

SPECS: tuple[LangSpec, ...] = (PYTHON, TYPESCRIPT, TSX, JAVASCRIPT, GO, JAVA)

_BY_EXTENSION: dict[str, LangSpec] = {}
for _spec in SPECS:
    for _ext in _spec.extensions:
        _BY_EXTENSION.setdefault(_ext, _spec)


def spec_for_path(path: str) -> LangSpec | None:
    from pathlib import PurePosixPath

    return _BY_EXTENSION.get(PurePosixPath(path).suffix.lower())


def language_for_path(path: str) -> str:
    spec = spec_for_path(path)
    return spec.name if spec else "unknown"


@lru_cache(maxsize=None)
def load_language(spec_name: str):
    """Load and cache a tree-sitter Language. Returns None if unavailable.

    The grammars are optional extras; a missing one must degrade the parse for
    that language, never fail the build.
    """
    import importlib

    spec = next((s for s in SPECS if s.name == spec_name), None)
    if spec is None:
        return None
    try:
        module = importlib.import_module(spec.module)
        factory = getattr(module, spec.language_attr)
        from tree_sitter import Language

        return Language(factory())
    except Exception:
        return None


def available_languages() -> tuple[str, ...]:
    return tuple(s.name for s in SPECS if load_language(s.name) is not None)


# --------------------------------------------------------------------------
# Call-target noise filter
# --------------------------------------------------------------------------

#: Built-in callables that would otherwise become god-nodes accumulating a
#: CALLS edge from every call site in the repository. Adapted from graphify's
#: equivalent list, which exists for exactly this reason.
_BUILTIN_CALLS: frozenset[str] = frozenset(
    {
        # Python
        "str", "int", "float", "bool", "list", "dict", "set", "tuple", "bytes",
        "len", "range", "enumerate", "zip", "map", "filter", "sum", "min", "max",
        "print", "open", "isinstance", "type", "super", "sorted", "reversed",
        "any", "all", "abs", "round", "next", "iter", "hash", "id", "repr",
        "callable", "getattr", "setattr", "hasattr", "delattr", "vars", "dir",
        "format", "input", "exit", "staticmethod", "classmethod", "property",
        # JS / TS
        "String", "Number", "Boolean", "Object", "Array", "Symbol", "BigInt",
        "Date", "RegExp", "Error", "TypeError", "RangeError", "SyntaxError",
        "Promise", "Map", "Set", "WeakMap", "WeakSet", "JSON", "Math",
        "parseInt", "parseFloat", "isNaN", "isFinite", "require", "console",
        "setTimeout", "setInterval", "clearTimeout", "clearInterval", "fetch",
        # Go
        "make", "new", "append", "copy", "delete", "cap", "panic", "recover",
        "close", "complex", "real", "imag",
        # Java
        "equals", "hashCode", "toString", "valueOf", "getClass",
        # Universal noise
        "get", "set", "add", "push", "pop", "join", "split", "replace", "trim",
        "keys", "values", "items", "append_", "log", "debug", "info", "warning",
        "error", "exception", "critical", "assert_", "self", "this",
    }
)


def is_builtin_call(name: str) -> bool:
    return name in _BUILTIN_CALLS


@lru_cache(maxsize=1)
def stdlib_modules() -> frozenset[str]:
    """Python standard library top-level module names.

    Used to keep ``import json`` from inventing a "json" technology. Falls back
    to a small hard-coded set on interpreters without ``stdlib_module_names``.
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    return frozenset(
        {
            "abc", "argparse", "asyncio", "base64", "collections", "contextlib",
            "csv", "dataclasses", "datetime", "enum", "functools", "hashlib",
            "io", "itertools", "json", "logging", "math", "os", "pathlib", "re",
            "shutil", "subprocess", "sys", "tempfile", "textwrap", "time",
            "typing", "unittest", "uuid", "warnings",
        }
    )
