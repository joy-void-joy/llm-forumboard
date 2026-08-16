# lup: ignore[bare-object, cast, frozenset-shape]
# Introspection's domain IS the arbitrary live object, so `object` params are
# the honest type, each display branch casts after its isinstance/inspect
# check, and TypedDict's own key-introspection API hands back frozensets;
# all three rules are opted out file-wide.
"""Helpers for ``py info`` — unified object introspection."""

import dataclasses  # lup: ignore[dataclass] — inspected, not used for modeling
import enum
import inspect
import json
import typing
from collections.abc import Callable
from typing import cast

import typer
from pydantic import BaseModel

from lup.devtools.py.common import find_module_path


def format_signature(obj: object, name: str) -> str:
    try:
        sig = inspect.signature(cast(Callable[..., object], obj))
        return f"{name}{sig}"
    except (ValueError, TypeError):
        return name


def format_type(annotation: object) -> str:
    if annotation is inspect.Parameter.empty or annotation is None:
        return "?"
    if isinstance(annotation, type):
        return annotation.__qualname__
    s = str(annotation)
    for prefix in ("typing.", "typing_extensions."):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    return s


def get_docstring(obj: object) -> str:
    doc = inspect.getdoc(obj)
    if not doc:
        return ""
    return doc.split("\n\n")[0]  # lup: ignore[string-split] — first paragraph


# ---------------------------------------------------------------------------
# py info — unified introspection
# ---------------------------------------------------------------------------


def defined_in(mod: object, attr: str) -> bool:
    """Whether ``attr`` is defined in ``mod`` rather than imported into it.

    Classes and functions carry ``__module__`` pointing at their defining
    module — names imported from elsewhere are excluded. Imported submodules
    (whose own ``__name__`` differs) are excluded too. Plain values without a
    ``__module__`` (module-level constants assigned here) are kept.
    """
    obj = getattr(mod, attr)
    host = getattr(mod, "__name__", None)
    if inspect.ismodule(obj):
        return getattr(obj, "__name__", None) == host
    owner = getattr(obj, "__module__", None)
    if owner is None:
        return True
    return owner == host


def show_module(obj: object, path: str, private: bool) -> None:
    file_path = find_module_path(path)
    if file_path:
        typer.echo(f"File: {file_path}")

    version = getattr(obj, "__version__", None)
    if version:
        typer.echo(f"Version: {version}")

    doc = get_docstring(obj)
    if doc:
        typer.echo(f"\n{doc}")

    module_name = getattr(obj, "__name__", path)

    # Four display buckets, filled by the classify loop below.
    classes: list[str] = []  # lup: ignore[empty-collection]
    functions: list[str] = []  # lup: ignore[empty-collection]
    values: list[str] = []  # lup: ignore[empty-collection]
    reexports: list[str] = []  # lup: ignore[empty-collection]
    for name in sorted(dir(obj)):
        if name.startswith("_") and not private:
            continue
        if not private and not defined_in(obj, name):
            continue
        member = getattr(obj, name)
        if inspect.ismodule(member):
            continue
        is_class = inspect.isclass(member)
        is_function = inspect.isfunction(member) or inspect.isbuiltin(member)
        if is_class or is_function:
            origin = getattr(member, "__module__", None)
            if isinstance(origin, str) and origin != module_name:
                reexports.append(f"{name} (from {origin})")
            elif is_class:
                classes.append(name)
            else:
                functions.append(format_signature(member, name))
        else:
            values.append(f"{name}: {type(member).__name__}")

    if classes:
        typer.echo(f"\nClasses ({len(classes)}):")
        for c in classes:
            typer.echo(f"  {c}")
    if functions:
        typer.echo(f"\nFunctions ({len(functions)}):")
        for f in functions:
            typer.echo(f"  {f}")
    if values:
        typer.echo(f"\nValues ({len(values)}):")
        for v in values:
            typer.echo(f"  {v}")
    if reexports:
        typer.echo(f"\nRe-exports ({len(reexports)}, defined elsewhere):")
        for r in reexports:
            typer.echo(f"  {r}")


# lup: ignore[library-default] — the members pydantic itself puts on a model, so
# the set follows that library rather than any taste of this one's
PYDANTIC_INTERNALS = frozenset(  # lup: ignore[frozenset-shape] — membership
    {
        "model_config",
        "model_fields",
        "model_computed_fields",
        "model_json_schema",
        "model_validate",
        "model_validate_json",
        "model_dump",
        "model_dump_json",
        "model_post_init",
        "model_rebuild",
        "model_copy",
        "model_construct",
        "model_fields_set",
        "model_extra",
        "model_parametrized_name",
    }
)


def show_class(cls: type, schema: bool, private: bool) -> None:
    try:
        source_file = inspect.getfile(cls)
        typer.echo(f"File: {source_file}")
    except TypeError:
        pass

    bases = [b.__qualname__ for b in cls.__mro__[1:] if b is not object]
    if bases:
        typer.echo(f"MRO: {' → '.join(bases)}")

    doc = get_docstring(cls)
    if doc:
        typer.echo(f"\n{doc}")

    is_pydantic = issubclass(cls, BaseModel)
    if is_pydantic:
        show_pydantic_fields(cast(type[BaseModel], cls), schema)

    if not is_pydantic:
        is_typed_dict = hasattr(cls, "__required_keys__") and hasattr(
            cls, "__optional_keys__"
        )
        if is_typed_dict:
            show_typed_dict_fields(cls)
        elif issubclass(cls, enum.Enum):
            show_enum_members(cast(type[enum.Enum], cls))
        elif dataclasses.is_dataclass(cls):
            show_dataclass_fields(cls)
        elif hasattr(cls, "__annotations__") and cls.__annotations__:
            typer.echo("\nAnnotations:")
            try:
                hints = typing.get_type_hints(cls)
            except (NameError, AttributeError, TypeError, RecursionError):
                hints = dict(cls.__annotations__)
            for name, ann in hints.items():
                if name.startswith("_") and not private:
                    continue
                default = getattr(cls, name, inspect.Parameter.empty)
                if default is not inspect.Parameter.empty:
                    typer.echo(f"  {name}: {format_type(ann)} = {default!r}")
                else:
                    typer.echo(f"  {name}: {format_type(ann)}")

    show_methods_section(cls, private, exclude_pydantic=is_pydantic)


def show_pydantic_fields(cls: type, schema: bool) -> None:
    model_cls = cast(type[BaseModel], cls)

    if schema:
        typer.echo("\nJSON Schema:")
        try:
            typer.echo(json.dumps(model_cls.model_json_schema(), indent=2))
        except (AttributeError, TypeError):
            typer.echo("  (cannot generate schema for this model)")
        return

    fields = model_cls.model_fields
    if fields:
        typer.echo(f"\nFields ({len(fields)}):")
        for name, field in fields.items():
            ann = format_type(field.annotation)
            parts = [f"  {name}: {ann}"]
            if not field.is_required():
                parts.append(f" = {field.default!r}")
            if field.description:
                parts.append(f"  — {field.description}")
            typer.echo("".join(parts))

    computed = model_cls.model_computed_fields
    if computed:
        typer.echo(f"\nComputed ({len(computed)}):")
        for name, field in computed.items():
            typer.echo(f"  {name}: {format_type(field.return_type)}")


def show_typed_dict_fields(cls: type) -> None:
    empty: frozenset[str] = frozenset()
    required: frozenset[str] = getattr(cls, "__required_keys__", empty)
    optional: frozenset[str] = getattr(cls, "__optional_keys__", empty)
    try:
        hints = typing.get_type_hints(cls)
    except (NameError, AttributeError, TypeError, RecursionError):
        hints = dict(getattr(cls, "__annotations__", {}))

    typer.echo(f"\nFields ({len(hints)}):")
    for name, ann in hints.items():
        tag = "required" if name in required else "optional" if name in optional else ""
        suffix = f"  ({tag})" if tag else ""
        typer.echo(f"  {name}: {format_type(ann)}{suffix}")


def show_enum_members(cls: type[enum.Enum]) -> None:
    members = list(cls)
    typer.echo(f"\nMembers ({len(members)}):")
    for member in members:
        typer.echo(f"  {member.name} = {member.value!r}")


def show_dataclass_fields(cls: type) -> None:
    dc_fields = dataclasses.fields(cls)
    typer.echo(f"\nFields ({len(dc_fields)}):")
    for f in dc_fields:
        parts = [f"  {f.name}: {format_type(f.type)}"]
        if f.default is not dataclasses.MISSING:
            parts.append(f" = {f.default!r}")
        elif f.default_factory is not dataclasses.MISSING:
            factory_name = getattr(
                f.default_factory, "__name__", repr(f.default_factory)
            )
            parts.append(f" = {factory_name}()")
        typer.echo("".join(parts))


def show_methods_section(
    cls: type, private: bool, *, exclude_pydantic: bool = False
) -> None:
    methods: list[str] = []  # lup: ignore[empty-collection] — display bucket
    properties: list[str] = []  # lup: ignore[empty-collection] — display bucket
    for name in sorted(cls.__dict__):
        if name.startswith("_") and not private:
            continue
        if exclude_pydantic and name in PYDANTIC_INTERNALS:
            continue
        member = cls.__dict__[name]
        if isinstance(member, property):
            properties.append(name)
        elif callable(member):
            methods.append(format_signature(member, name))

    if properties:
        typer.echo(f"\nProperties ({len(properties)}):")
        for p in properties:
            typer.echo(f"  {p}")
    if methods:
        typer.echo(f"\nMethods ({len(methods)}):")
        for m in methods:
            typer.echo(f"  {m}")


def show_callable_info(obj: object, name: str) -> None:
    sig = format_signature(obj, name)
    typer.echo(f"\n{sig}")

    doc = get_docstring(obj)
    if doc:
        typer.echo(f"\n{doc}")

    callable_obj = cast(Callable[..., object], obj)
    try:
        source_file = inspect.getfile(callable_obj)
        _, start_lineno = inspect.getsourcelines(callable_obj)
        typer.echo(f"\nDefined at: {source_file}:{start_lineno}")
    except (TypeError, OSError):
        pass


# A repr can be arbitrarily large (a loaded dataframe, a deep dict); this caps
# the inline preview so `py info` doesn't flood the terminal, and reports the
# full length so the truncation is never silent. Use `py source`/`py eval` for
# the whole value.
REPR_PREVIEW_CHARS = 2000


def show_value_info(obj: object, preview_chars: int = REPR_PREVIEW_CHARS) -> None:
    typer.echo(f"Type: {type(obj).__qualname__}")
    r = repr(obj)
    if len(r) > preview_chars:
        r = r[:preview_chars] + f"… ({len(r)} chars total)"
    typer.echo(f"Value: {r}")

    doc = get_docstring(obj)
    if doc:
        typer.echo(f"\n{doc}")
