"""Describe SQLTide's public shape surface: each public name in its role modules."""

from __future__ import annotations

import ast
import dataclasses
import enum
import importlib
import inspect
import json
import pkgutil
import typing
from types import MappingProxyType, ModuleType
from typing import Any

from scripts.shape_surface.constants import (
    BOOL_ANNOTATION,
    CAUSE_ANNOTATION,
    INT_ANNOTATION,
    PACKAGE,
    PROBE_ERROR_KEY,
    PROBE_OVERRIDES,
    PROBE_TUPLE_KEY,
    ROLES,
    TUPLE_ANNOTATION,
)


def describe_value(
    *, value: object, alias_source: str = "", probe: dict[str, Any] | None = None
) -> dict[str, object]:
    """The comparable record of one public class, `type` alias source or constant value."""
    if isinstance(value, type):
        return _describe_class(cls=value, probe=probe)
    if isinstance(value, typing.TypeAliasType):
        return {"kind": "alias", "module": value.__module__, "value": alias_source}
    return {"kind": "constant", "type": type(value).__qualname__, "value": _canonical(value)}


def _describe_class(*, cls: type, probe: dict[str, Any] | None) -> dict[str, object]:
    record: dict[str, object] = {
        "module": cls.__module__,
        "qualname": cls.__qualname__,
        "mro": [f"{base.__module__}.{base.__qualname__}" for base in cls.__mro__],
    }
    if issubclass(cls, enum.Enum):
        record["kind"] = "enum"
        record["members"] = [[member.name, _canonical(member.value)] for member in cls]
    elif issubclass(cls, BaseException):
        record["kind"] = "exception"
        record["doc"] = cls.__doc__
        key: str = f"{cls.__module__}:{cls.__qualname__}"
        chosen: dict[str, Any] = probe or PROBE_OVERRIDES.get(key) or _derive_probe(cls)
        record["probe"] = chosen
        record["instance"] = _probe_instance(cls=cls, probe=chosen)
    elif hasattr(cls, "model_json_schema"):
        record["kind"] = "pydantic"
        record["fields"] = sorted(getattr(cls, "model_fields", {}))
    elif dataclasses.is_dataclass(cls):
        record["kind"] = "dataclass"
        record["match_args"] = list(getattr(cls, "__match_args__", ()))
        record["hashable"] = cls.__hash__ is not None
        params: Any = vars(cls)["__dataclass_params__"]
        record["frozen"] = params.frozen
        record["fields"] = [
            [
                field.name,
                field.default is not dataclasses.MISSING
                or field.default_factory is not dataclasses.MISSING,
                field.compare,
                field.repr,
            ]
            for field in dataclasses.fields(cls)
        ]
    elif getattr(cls, "_is_protocol", False):
        record["kind"] = "protocol"
        record["members"] = sorted(getattr(cls, "__protocol_attrs__", ()))
    else:
        record["kind"] = "class"
    return record


def _probe_value(*, name: str, annotation: object) -> object:
    """A JSON-encoded argument for one constructor parameter, chosen from its annotation."""
    text: str = (
        annotation
        if isinstance(annotation, str)
        else getattr(annotation, "__name__", repr(annotation))
    )
    if TUPLE_ANNOTATION in text:
        return {PROBE_TUPLE_KEY: [f"{name}-a", f"{name}-b"]}
    if text == INT_ANNOTATION:
        return 3
    if text == BOOL_ANNOTATION:
        return True
    if CAUSE_ANNOTATION in text:
        return {PROBE_ERROR_KEY: f"{name}-cause"}
    return f"probe-{name}"


def _derive_probe(cls: type) -> dict[str, Any]:
    """Constructor arguments for one exception: required parameters only, by annotation."""
    try:
        signature: inspect.Signature = inspect.signature(cls)
    except (TypeError, ValueError):
        return {"args": ["probe message"], "kwargs": {}}
    arguments: list[object] = []
    keywords: dict[str, object] = {}
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        value: object = _probe_value(name=parameter.name, annotation=parameter.annotation)
        if parameter.kind is parameter.KEYWORD_ONLY:
            keywords[parameter.name] = value
        else:
            arguments.append(value)
    if not arguments and not keywords and signature.parameters:
        arguments = ["probe message"]
    return {"args": arguments, "kwargs": keywords}


def _decode(value: Any) -> object:
    if isinstance(value, dict) and PROBE_TUPLE_KEY in value:
        return tuple(_decode(item) for item in value[PROBE_TUPLE_KEY])
    if isinstance(value, dict) and PROBE_ERROR_KEY in value:
        return ValueError(value[PROBE_ERROR_KEY])
    return value


def _probe_instance(*, cls: type, probe: dict[str, Any]) -> object:
    """The probed instance's `.args`, `str` and `repr`, or the construction failure."""
    try:
        instance: object = cls(
            *[_decode(item) for item in probe["args"]],
            **{key: _decode(item) for key, item in probe["kwargs"].items()},
        )
    except Exception as error:
        return {"raised": type(error).__name__, "message": str(error)}
    return {
        "args": _canonical(getattr(instance, "args", ())),
        "str": str(instance),
        "repr": repr(instance),
    }


def _canonical(value: object) -> object:
    if isinstance(value, enum.Enum):
        return f"{type(value).__qualname__}.{value.name}"
    if isinstance(value, (frozenset, set)):
        return sorted(json.dumps(_canonical(item), sort_keys=True) for item in value)
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (dict, MappingProxyType)):
        return sorted([_canonical(key), _canonical(item)] for key, item in value.items())
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, BaseException):
        return [type(value).__name__, _canonical(value.args)]
    return repr(value)


def module_statements(module: ModuleType) -> tuple[frozenset[str], dict[str, str]]:
    """The module's top-level assigned names, and each `type` alias's value source."""
    names: set[str] = set()
    aliases: dict[str, str] = {}
    for node in ast.parse(inspect.getsource(module)).body:
        if isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            aliases[node.name.id] = ast.unparse(node.value)
            continue
        targets: list[ast.expr] = (
            [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        )
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(names), aliases


def describe_module(module: ModuleType) -> dict[str, dict[str, object]]:
    """Each public class, alias and constant `module` defines, keyed `module:name`."""
    assigned, aliases = module_statements(module)
    surface: dict[str, dict[str, object]] = {}
    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        defined_here: bool = (
            isinstance(value, (type, typing.TypeAliasType)) and value.__module__ == module.__name__
        )
        constant: bool = name in assigned and not isinstance(value, (type, ModuleType))
        if defined_here or constant:
            surface[f"{module.__name__}:{name}"] = describe_value(
                value=value, alias_source=aliases.get(name, "")
            )
    return surface


def collect_surface() -> dict[str, dict[str, object]]:
    """Every public name defined in an installed role module, keyed `module:name`."""
    package: ModuleType = importlib.import_module(PACKAGE)
    surface: dict[str, dict[str, object]] = {}
    for info in pkgutil.walk_packages(package.__path__, f"{PACKAGE}."):
        if info.name.rsplit(".", 1)[-1] in ROLES:
            surface |= describe_module(importlib.import_module(info.name))
    return dict(sorted(surface.items()))


def render_baseline(surface: dict[str, dict[str, object]]) -> str:
    """The baseline file text: one sorted name per line, so each removal is a one-line diff."""
    lines: list[str] = [
        f"{json.dumps(key)}: {json.dumps(record, sort_keys=True)}"
        for key, record in sorted(surface.items())
    ]
    return "{\n" + ",\n".join(lines) + "\n}\n"
