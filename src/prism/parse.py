"""Stage 4 -- symbols, calls and imports, from the AST.

Scope is deliberately narrow. Prism does not want a map of every code element;
it wants enough structure to say *how* someone implemented something and to
point at the lines. So this stage extracts definitions with their spans, the
calls between them, and the imports that reveal technology -- and nothing else.

Resolution is tiered and every tier is labelled, because an honest "I am less
sure about this one" is worth more than a confident wrong edge:

* ``same_file``         -- one definition of that name in this file  (EXTRACTED)
* ``import_resolved``   -- the name was imported from a file we parsed (EXTRACTED)
* ``cross_file_unique`` -- exactly one definition repo-wide            (INFERRED)

Anything else is dropped and counted, never guessed at.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .gitio import show_blob
from .languages import (
    LangSpec,
    RawImport,
    is_builtin_call,
    load_language,
    spec_for_path,
    stdlib_modules,
)

__all__ = [
    "Symbol", "ResolvedCall", "InternalImport", "ExternalImport", "ParseResult",
    "parse_files",
]

#: Guard against a single pathological file eating the run.
_MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class Symbol:
    """One definition, located."""

    path: str
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    language: str
    signature: str = ""
    decorators: tuple[str, ...] = field(default=())
    parent: str | None = None   # qualified name of the enclosing symbol

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.kind, self.qualified_name)

    @property
    def span_lines(self) -> int:
        return max(1, self.end_line - self.start_line + 1)


@dataclass(frozen=True)
class CallSite:
    """An unresolved call, as written."""

    path: str
    caller: str | None       # qualified name of the enclosing symbol
    callee_name: str
    receiver: str | None
    line: int


@dataclass(frozen=True)
class ResolvedCall:
    caller: tuple[str, str, str]
    callee: tuple[str, str, str]
    resolution: str
    confidence: str
    line: int
    path: str


@dataclass(frozen=True)
class InternalImport:
    """An import that resolved to another file in this repository."""

    path: str
    target_path: str
    module: str
    line: int
    confidence: str = "EXTRACTED"


@dataclass(frozen=True)
class ExternalImport:
    """An import of something outside the repository -- a technology signal."""

    path: str
    module: str          # as written: "fastapi.routing"
    root: str            # the package: "fastapi"
    line: int
    language: str


@dataclass
class ParseResult:
    symbols: tuple[Symbol, ...] = ()
    calls: tuple[ResolvedCall, ...] = ()
    internal_imports: tuple[InternalImport, ...] = ()
    external_imports: tuple[ExternalImport, ...] = ()
    #: Repo files reached only by an import from a parsed file.
    context_files: tuple[str, ...] = ()
    unresolved_calls: int = 0
    module_level_calls: int = 0
    skipped: dict[str, str] = field(default_factory=dict)

    def symbols_in(self, path: str) -> tuple[Symbol, ...]:
        return tuple(s for s in self.symbols if s.path == path)


# --------------------------------------------------------------------------
# AST walk
# --------------------------------------------------------------------------

def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def _name_of(node) -> str:
    named = node.child_by_field_name("name")
    if named is not None:
        return _text(named).strip()
    # Go type_spec and similar put the identifier first.
    for child in node.named_children:
        if child.type in {"identifier", "type_identifier", "field_identifier", "property_identifier"}:
            return _text(child).strip()
    return ""


def _signature_of(node, name: str) -> str:
    params = node.child_by_field_name("parameters")
    if params is None:
        return name
    text = _text(params).replace("\n", " ")
    text = " ".join(text.split())
    if len(text) > 180:
        text = text[:177] + "..."
    return f"{name}{text}"


def _callee_of(node, spec: LangSpec) -> tuple[str, str | None]:
    """Return ``(callee_name, receiver_or_None)`` for a call node."""
    if spec.call_name_field:
        direct = node.child_by_field_name(spec.call_name_field)
        if direct is not None:
            obj = node.child_by_field_name("object")
            return _text(direct).strip(), (_text(obj).strip() or None)
        # object_creation_expression names a type instead.
        type_node = node.child_by_field_name("type")
        if type_node is not None:
            return _text(type_node).strip(), None

    fn = node.child_by_field_name(spec.call_function_field)
    if fn is None:
        # new_expression in JS names a constructor.
        ctor = node.child_by_field_name("constructor")
        if ctor is not None:
            return _text(ctor).strip(), None
        return "", None

    if fn.type in spec.accessor_types:
        attr = fn.child_by_field_name(spec.accessor_name_field)
        obj = fn.child_by_field_name("object") or fn.child_by_field_name("operand")
        return _text(attr).strip(), (_text(obj).strip() or None)

    raw = _text(fn).strip()
    if not raw:
        return "", None
    if "." in raw:
        head, _, tail = raw.rpartition(".")
        return tail.strip(), head.strip() or None
    return raw, None


def _is_class_like(symbol: Symbol | None) -> bool:
    return symbol is not None and symbol.kind in {"class", "interface", "struct", "enum"}


class _FileWalker:
    """Collects symbols, call sites and imports from one parsed file."""

    def __init__(self, path: str, spec: LangSpec):
        self.path = path
        self.spec = spec
        self.symbols: list[Symbol] = []
        self.calls: list[CallSite] = []
        self.imports: list[RawImport] = []

    def run(self, root) -> None:
        self._walk(root, enclosing=None, pending_decorators=())

    # -- internals ------------------------------------------------------

    def _emit_symbol(self, node, enclosing: Symbol | None, decorators: tuple[str, ...]) -> Symbol | None:
        name = _name_of(node)
        if not name:
            return None
        kind = self.spec.kind_for(node.type, inside_class=_is_class_like(enclosing))
        qualified = f"{enclosing.qualified_name}.{name}" if enclosing else name
        symbol = Symbol(
            path=self.path,
            kind=kind,
            name=name,
            qualified_name=qualified,
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
            language=self.spec.name,
            signature=_signature_of(node, name),
            decorators=decorators,
            parent=enclosing.qualified_name if enclosing else None,
        )
        self.symbols.append(symbol)
        return symbol

    def _js_named_function(self, node) -> tuple[object, object] | None:
        """``const handler = () => {}`` -- the name is on the declarator."""
        if node.type not in {"variable_declarator", "pair", "public_field_definition"}:
            return None
        value = node.child_by_field_name("value")
        if value is None or value.type not in {"arrow_function", "function_expression", "function"}:
            return None
        name_node = node.child_by_field_name("name") or node.child_by_field_name("key")
        if name_node is None:
            return None
        return name_node, value

    def _walk(self, node, *, enclosing: Symbol | None, pending_decorators: tuple[str, ...]) -> None:
        spec = self.spec
        node_type = node.type

        # A decorated definition: hoist the decorators onto the definition below.
        if spec.decorated_wrapper and node_type == spec.decorated_wrapper:
            decorators = tuple(
                _text(c).strip()
                for c in node.named_children
                if c.type in spec.decorator_types
            )
            for child in node.named_children:
                if child.type in spec.decorator_types:
                    continue
                self._walk(child, enclosing=enclosing, pending_decorators=decorators)
            return

        if node_type in spec.import_types and spec.import_extractor is not None:
            raw = spec.import_extractor(node)
            if raw is not None:
                self.imports.append(raw)
            return

        # JS/TS arrow function bound to a name.
        bound = self._js_named_function(node)
        if bound is not None:
            name_node, value_node = bound
            name = _text(name_node).strip()
            if name:
                qualified = f"{enclosing.qualified_name}.{name}" if enclosing else name
                symbol = Symbol(
                    path=self.path,
                    kind="method" if _is_class_like(enclosing) else "function",
                    name=name,
                    qualified_name=qualified,
                    start_line=node.start_point[0] + 1,
                    end_line=value_node.end_point[0] + 1,
                    language=spec.name,
                    signature=_signature_of(value_node, name),
                    decorators=pending_decorators,
                    parent=enclosing.qualified_name if enclosing else None,
                )
                self.symbols.append(symbol)
                for child in value_node.named_children:
                    self._walk(child, enclosing=symbol, pending_decorators=())
                return

        if node_type in spec.definition_types:
            symbol = self._emit_symbol(node, enclosing, pending_decorators)
            inner = symbol if symbol is not None else enclosing
            for child in node.named_children:
                self._walk(child, enclosing=inner, pending_decorators=())
            return

        if node_type in spec.call_types:
            callee, receiver = _callee_of(node, spec)
            if callee:
                self.calls.append(
                    CallSite(
                        path=self.path,
                        caller=enclosing.qualified_name if enclosing else None,
                        callee_name=callee,
                        receiver=receiver,
                        line=node.start_point[0] + 1,
                    )
                )
            # Arguments can hold further calls.
            for child in node.named_children:
                self._walk(child, enclosing=enclosing, pending_decorators=())
            return

        for child in node.named_children:
            self._walk(child, enclosing=enclosing, pending_decorators=())


# --------------------------------------------------------------------------
# Import resolution
# --------------------------------------------------------------------------

_PY_SOURCE_ROOTS = ("", "src/", "lib/")


def _python_candidates(raw: RawImport, path: str) -> list[str]:
    """Candidate repo-relative paths for a Python import."""
    candidates: list[str] = []
    parts = [p for p in raw.module.split(".") if p]

    if raw.is_relative:
        base = PurePosixPath(path).parent
        # level 1 is the current package; each extra dot climbs one more.
        for _ in range(raw.level - 1):
            base = base.parent
        stem = base.joinpath(*parts) if parts else base
        candidates.append(f"{stem}.py")
        candidates.append(f"{stem}/__init__.py")
        # "from . import queries" -- the name is the module.
        for name in raw.names:
            if name and name != "*":
                candidates.append(f"{base.joinpath(*parts, name)}.py")
                candidates.append(f"{base.joinpath(*parts, name)}/__init__.py")
        return [c.lstrip("/") for c in candidates]

    for root in _PY_SOURCE_ROOTS:
        stem = root + "/".join(parts)
        candidates.append(f"{stem}.py")
        candidates.append(f"{stem}/__init__.py")
        # "from app.db import queries" where queries is a module, not a name.
        for name in raw.names:
            if name and name != "*":
                candidates.append(f"{stem}/{name}.py")
                candidates.append(f"{stem}/{name}/__init__.py")
    return candidates


def _js_candidates(raw: RawImport, path: str, spec: LangSpec) -> list[str]:
    if not raw.module.startswith("."):
        return []
    base = (PurePosixPath(path).parent / raw.module).as_posix()
    # Normalise "a/b/../c" -> "a/c"
    parts: list[str] = []
    for segment in base.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    stem = "/".join(parts)
    candidates = [stem]
    candidates += [f"{stem}{suffix}" for suffix in spec.resolve_suffixes]
    candidates += [f"{stem}/{index}" for index in spec.index_names]
    return candidates


def _external_root(module: str, language: str) -> str | None:
    """The package name to credit, or None if this is a standard library import."""
    if not module:
        return None

    if language == "python":
        root = module.split(".")[0]
        if not root or root in stdlib_modules():
            return None
        return root

    if language in {"javascript", "typescript", "tsx"}:
        if module.startswith("."):
            return None
        if module.startswith("@"):
            parts = module.split("/")
            return "/".join(parts[:2]) if len(parts) >= 2 else module
        if module.startswith("node:"):
            return None
        return module.split("/")[0]

    if language == "go":
        first = module.split("/")[0]
        # A domain in the first segment means a third-party module; anything
        # else ("net/http", "fmt") is the Go standard library.
        return module if "." in first else None

    if language == "java":
        if module.startswith(("java.", "javax.", "jdk.", "sun.")):
            return None
        parts = module.split(".")
        return ".".join(parts[:3]) if len(parts) >= 3 else module

    return module


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_files(
    repo: Path | str,
    paths: list[str],
    *,
    rev: str = "HEAD",
    known_files: set[str] | None = None,
) -> ParseResult:
    """Parse ``paths`` and resolve the relationships between them.

    ``known_files`` is every tracked path at ``rev``. Imports are resolved
    against it so an import into a file the person never touched is still
    recognised as internal; that target becomes a context File node rather than
    being misfiled as an external technology.
    """
    from tree_sitter import Parser

    known = known_files if known_files is not None else set(paths)
    parse_set = set(paths)

    all_symbols: list[Symbol] = []
    all_callsites: list[CallSite] = []
    internal: list[InternalImport] = []
    external: list[ExternalImport] = []
    context: set[str] = set()
    skipped: dict[str, str] = {}
    # path -> {local_name: target_path}, used for import-aware call resolution.
    imported_names: dict[str, dict[str, str]] = {}

    for path in sorted(parse_set):
        spec = spec_for_path(path)
        if spec is None:
            continue
        language = load_language(spec.name)
        if language is None:
            skipped[path] = f"grammar unavailable: {spec.module}"
            continue
        blob = show_blob(repo, path, rev)
        if blob is None:
            skipped[path] = "not present at rev"
            continue
        if len(blob) > _MAX_FILE_BYTES:
            skipped[path] = f"too large ({len(blob)} bytes)"
            continue
        if b"\x00" in blob[:8192]:
            skipped[path] = "binary"
            continue

        try:
            parser = Parser(language)
            tree = parser.parse(blob)
        except Exception as exc:  # pragma: no cover - grammar-level failure
            skipped[path] = f"parse error: {type(exc).__name__}"
            continue

        walker = _FileWalker(path, spec)
        walker.run(tree.root_node)
        all_symbols.extend(walker.symbols)
        all_callsites.extend(walker.calls)

        # Resolve this file's imports.
        local_names: dict[str, str] = {}
        for raw in walker.imports:
            if spec.name == "python":
                candidates = _python_candidates(raw, path)
            elif spec.name in {"javascript", "typescript", "tsx"}:
                candidates = _js_candidates(raw, path, spec)
            else:
                candidates = []

            target = next((c for c in candidates if c in known and c != path), None)
            if target is not None:
                internal.append(
                    InternalImport(path=path, target_path=target, module=raw.module, line=raw.line)
                )
                if target not in parse_set:
                    context.add(target)
                for name in raw.names:
                    if name and name != "*":
                        local_names[name] = target
                continue

            root = _external_root(raw.module, spec.name)
            if root:
                external.append(
                    ExternalImport(
                        path=path,
                        module=raw.module,
                        root=root,
                        line=raw.line,
                        language=spec.name,
                    )
                )
        imported_names[path] = local_names

    resolved, unresolved, module_level = _resolve_calls(
        all_symbols, all_callsites, imported_names
    )

    return ParseResult(
        symbols=tuple(sorted(all_symbols, key=lambda s: (s.path, s.start_line, s.qualified_name))),
        calls=tuple(sorted(resolved, key=lambda c: (c.path, c.line, c.callee))),
        internal_imports=tuple(sorted(internal, key=lambda i: (i.path, i.line, i.target_path))),
        external_imports=tuple(sorted(external, key=lambda i: (i.path, i.line, i.module))),
        context_files=tuple(sorted(context)),
        unresolved_calls=unresolved,
        module_level_calls=module_level,
        skipped=skipped,
    )


def _resolve_calls(
    symbols: list[Symbol],
    callsites: list[CallSite],
    imported_names: dict[str, dict[str, str]],
) -> tuple[list[ResolvedCall], int, int]:
    by_path_name: dict[tuple[str, str], list[Symbol]] = {}
    by_name: dict[str, list[Symbol]] = {}
    by_qualified: dict[tuple[str, str], Symbol] = {}

    for symbol in symbols:
        by_path_name.setdefault((symbol.path, symbol.name), []).append(symbol)
        by_name.setdefault(symbol.name, []).append(symbol)
        by_qualified[(symbol.path, symbol.qualified_name)] = symbol

    seen: dict[tuple[tuple[str, str, str], tuple[str, str, str]], ResolvedCall] = {}
    unresolved = 0
    module_level = 0

    for site in callsites:
        if site.caller is None:
            module_level += 1
            continue
        if is_builtin_call(site.callee_name):
            continue

        caller = by_qualified.get((site.path, site.caller))
        if caller is None:
            unresolved += 1
            continue

        target: Symbol | None = None
        resolution = ""
        confidence = "INFERRED"

        same_file = by_path_name.get((site.path, site.callee_name), [])
        if len(same_file) == 1:
            target, resolution, confidence = same_file[0], "same_file", "EXTRACTED"
        else:
            target_path = imported_names.get(site.path, {}).get(site.callee_name)
            if target_path is not None:
                in_target = by_path_name.get((target_path, site.callee_name), [])
                if len(in_target) == 1:
                    target, resolution, confidence = in_target[0], "import_resolved", "EXTRACTED"
            if target is None:
                repo_wide = by_name.get(site.callee_name, [])
                if len(repo_wide) == 1:
                    target, resolution, confidence = repo_wide[0], "cross_file_unique", "INFERRED"

        if target is None:
            unresolved += 1
            continue

        key = (caller.key, target.key)
        existing = seen.get(key)
        # Keep the first call site; it is the one the citation points at.
        if existing is None or site.line < existing.line:
            seen[key] = ResolvedCall(
                caller=caller.key,
                callee=target.key,
                resolution=resolution,
                confidence=confidence,
                line=site.line,
                path=site.path,
            )

    return list(seen.values()), unresolved, module_level
