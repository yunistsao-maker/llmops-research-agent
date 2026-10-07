"""Read the CloudFormation templates in infra/ the way the tests need them (short-form tags become plain dicts)."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

INFRA = Path(__file__).resolve().parents[2] / "infra"


class _Loader(yaml.SafeLoader):
    pass


def _tag(loader: yaml.Loader, suffix: str, node: yaml.Node) -> dict:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {("Ref" if suffix == "Ref" else "Condition" if suffix == "Condition" else "Fn::" + suffix): value}


_Loader.add_multi_constructor("!", _tag)


def load_template(name: str) -> dict:
    return yaml.load((INFRA / name).read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 - a SafeLoader subclass


def _substitute(node: Any, values: dict[str, str]) -> Any:
    if isinstance(node, str):
        for key, value in values.items():
            node = node.replace("${" + key + "}", value)
        return node
    if isinstance(node, dict):
        return {k: _substitute(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, values) for v in node]
    return node


def state_machine(values: dict[str, str] | None = None) -> tuple[dict, dict]:
    """(the definition with its DefinitionSubstitutions filled in from ``values``, the resource's properties)."""
    props = load_template("pipeline.yaml")["Resources"]["UpdateStateMachine"]["Properties"]
    names = list(props["DefinitionSubstitutions"])
    values = values or {n: f"arn:test:{n}" for n in names}
    assert sorted(values) == sorted(names), f"substitutions differ: template has {sorted(names)}"
    definition = _substitute(copy.deepcopy(props["Definition"]), values)
    return definition, props
