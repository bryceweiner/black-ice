"""Tool registry. Schemas are derived from type hints -- never hand-written,
so a service function and its tool cannot drift apart."""

from __future__ import annotations

import inspect
import logging
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("blackice.tools")

JSON_TYPES: dict[Any, str] = {
    str: "string", int: "integer", float: "number", bool: "boolean",
    list: "array", dict: "object",
}


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """`str | None` -> (str, True)."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return (args[0] if args else str), len(args) < len(typing.get_args(annotation))
    return annotation, False


def _json_type(annotation: Any) -> dict[str, Any]:
    base, _ = _unwrap_optional(annotation)
    origin = typing.get_origin(base) or base
    if origin in (list, typing.List):  # noqa: UP006
        return {"type": "array", "items": {}}
    if origin in (dict, typing.Dict):  # noqa: UP006
        return {"type": "object"}
    if isinstance(base, type) and issubclass(base, bool):
        return {"type": "boolean"}
    return {"type": JSON_TYPES.get(origin, "string")}


def schema_from_signature(fn: Callable) -> dict[str, Any]:
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    props: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls") or param.kind in (
            inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD
        ):
            continue
        props[name] = _json_type(hints.get(name, str))
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., Awaitable[Any]]

    def spec(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def register(
        self,
        fn: Callable[..., Awaitable[Any]],
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> Tool:
        tool = Tool(
            name=name or fn.__name__,
            description=description or (inspect.getdoc(fn) or "").split("\n\n")[0],
            parameters=parameters or schema_from_signature(fn),
            fn=fn,
        )
        self.tools[tool.name] = tool
        return tool

    def tool(self, name: str | None = None, description: str | None = None):
        def deco(fn):
            self.register(fn, name=name, description=description)
            return fn

        return deco

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self.tools.values()]

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return await tool.fn(**(arguments or {}))
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:
            log.exception("tool %s failed", name)
            return {"error": f"{type(exc).__name__}: {exc}"}


registry = ToolRegistry()
tool = registry.tool


def project_plugin_tools(plugin_registry: Any, into: ToolRegistry) -> None:
    """Expose each plugin's declared tools as `<plugin>.<tool>`."""
    for name, sup in plugin_registry.supervisors.items():
        for descriptor in sup.describe():
            for spec in descriptor.tools:
                qualified = f"{name}.{spec.name}"

                def make(plugin: str, cmd: str):
                    async def call(**kwargs):
                        return await plugin_registry.command(plugin, cmd, **kwargs)

                    return call

                into.tools[qualified] = Tool(
                    name=qualified,
                    description=spec.description,
                    parameters=spec.parameters or {"type": "object", "properties": {}},
                    fn=make(name, spec.name),
                )
