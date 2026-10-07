"""A small, strict interpreter for the subset of Amazon States Language that infra/pipeline.yaml uses.

It exists so the *real* state machine definition can be executed in tests against the real Lambda handlers.  It is
deliberately strict: a field or feature it does not model raises ``Unsupported`` instead of being ignored, so the
definition cannot quietly depend on behaviour these tests never exercise.

Modelled (JSONPath mode): Pass, Choice, Wait, Task (lambda:invoke and sns:publish, Parameters / ResultSelector /
ResultPath / OutputPath, Retry, Catch, TimeoutSeconds), Succeed, Fail; the context object's Execution.Id; the 256 KB
payload limit; ``States.Runtime`` for a path that does not resolve (which, as in the service, no Catch can handle).
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_PAYLOAD = 262_144
LAMBDA_INVOKE = "arn:aws:states:::lambda:invoke"
SNS_PUBLISH = "arn:aws:states:::sns:publish"
_COMMON = {"Type", "Comment", "Next", "End", "InputPath", "OutputPath"}
_FIELDS = {
    "Pass": _COMMON | {"Parameters", "Result", "ResultPath"},
    "Choice": {"Type", "Comment", "Choices", "Default", "InputPath", "OutputPath"},
    "Wait": _COMMON | {"Seconds"},
    "Task": _COMMON | {"Resource", "Parameters", "ResultSelector", "ResultPath", "Retry", "Catch", "TimeoutSeconds"},
    "Succeed": {"Type", "Comment"},
    "Fail": {"Type", "Comment", "Error", "Cause"},
}
_RULE_OPS = {"StringEquals", "IsPresent", "BooleanEquals", "NumericEquals"}


class Unsupported(AssertionError):
    """The definition uses something this interpreter does not model: extend the interpreter (and its tests) first."""


class StatesRuntime(Exception):
    """States.Runtime: the execution fails on the spot."""


class TaskError(Exception):
    """An error a Task raised, in Step Functions terms."""

    def __init__(self, error: str, cause: str):
        super().__init__(f"{error}: {cause}")
        self.error, self.cause = error, cause


class ServiceError(Exception):
    """Raise from a fake Lambda to simulate an error of the Lambda *service* (e.g. Lambda.TooManyRequestsException)."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


@dataclass
class Execution:
    status: str  # SUCCEEDED | FAILED
    output: Any = None
    error: str | None = None
    cause: str | None = None
    path: list[str] = field(default_factory=list)  # the states entered, in order
    waited: int = 0  # seconds spent in Wait states
    invocations: list[tuple[str, str]] = field(default_factory=list)  # (state, resource/function) per attempt


# ---------------------------------------------------------------------------------------------- paths
_SEGMENT = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")


def _segments(path: str, root: str) -> list[str]:
    if path == root:
        return []
    rest = path[len(root):]
    segments, pos = [], 0
    while pos < len(rest):
        m = _SEGMENT.match(rest, pos)
        if not m:
            raise Unsupported(f"path syntax not modelled: {path!r}")
        segments.append(m.group(1))
        pos = m.end()
    return segments


def get_path(path: str, data: Any, context: dict) -> Any:
    root, base = ("$$", context) if path.startswith("$$") else ("$", data)
    if not path.startswith("$"):
        raise Unsupported(f"not a path: {path!r}")
    node = base
    for seg in _segments(path, root):
        if not isinstance(node, dict) or seg not in node:
            raise StatesRuntime(f"path {path} does not resolve against {json.dumps(base, ensure_ascii=False)[:200]}")
        node = node[seg]
    return node


def has_path(path: str, data: Any, context: dict) -> bool:
    try:
        get_path(path, data, context)
    except StatesRuntime:
        return False
    return True


def set_path(data: Any, path: str, value: Any) -> Any:
    segments = _segments(path, "$")
    if not segments:
        return value
    out = copy.deepcopy(data) if isinstance(data, dict) else {}
    node = out
    for seg in segments[:-1]:
        if not isinstance(node.get(seg), dict):
            node[seg] = {}
        node = node[seg]
    node[segments[-1]] = value
    return out


_FORMAT = re.compile(r"States\.Format\('((?:[^'\\{}]|\\.|\{\})*)'((?:\s*,\s*\$\$?(?:\.[A-Za-z_][A-Za-z0-9_]*)*)*)\s*\)")


def _format(call: str, data: Any, context: dict) -> str:
    """States.Format('text with {} in it', <path>, ...): the one intrinsic function the definition uses.

    As the service reads it: the template stands in single quotes; a quote, a brace or a backslash that is meant as
    itself is written with a backslash in front; every {} takes the next argument (a path; text and numbers as they
    are, anything else as JSON).  What does not fit that is not modelled - and is reported, not guessed at.
    """
    m = _FORMAT.fullmatch(call)
    if not m:
        raise Unsupported(f"intrinsic function not modelled (or not well-formed): {call!r}")
    args = [get_path(a.strip(), data, context) for a in m.group(2).split(",")[1:]]
    pieces = re.split(r"(\\.|\{\})", m.group(1))
    if pieces.count("{}") != len(args):
        raise StatesRuntime(f"States.Format: {pieces.count('{}')} placeholders, {len(args)} arguments")
    out, rest = [], iter(args)
    for piece in pieces:
        if piece == "{}":
            value = next(rest)
            out.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        else:
            out.append(piece[1] if len(piece) == 2 and piece[0] == "\\" else piece)
    return "".join(out)


def render(template: Any, data: Any, context: dict) -> Any:
    """Parameters / ResultSelector: keys ending in ".$" hold a path - or States.Format(...), the one intrinsic function modelled."""
    if isinstance(template, dict):
        out = {}
        for key, value in template.items():
            if key.endswith(".$"):
                if isinstance(value, str) and value.startswith("States."):
                    out[key[:-2]] = _format(value, data, context)
                    continue
                if not isinstance(value, str) or not value.startswith("$"):
                    raise Unsupported(f"{key}: only paths are modelled, got {value!r}")
                out[key[:-2]] = copy.deepcopy(get_path(value, data, context))
            else:
                out[key] = render(value, data, context)
        return out
    if isinstance(template, list):
        return [render(v, data, context) for v in template]
    return template


# ---------------------------------------------------------------------------------------------- static checks
def targets(state: dict) -> list[str]:
    out = [state[k] for k in ("Next", "Default") if k in state]
    out += [c["Next"] for c in state.get("Choices", [])]
    out += [c["Next"] for c in state.get("Catch", [])]
    return out


def lint(definition: dict) -> list[str]:
    """Structural problems cfn-lint does not report (dangling or unreachable states, states that lead nowhere)."""
    states = definition.get("States", {})
    problems = []
    if definition.get("StartAt") not in states:
        problems.append(f"StartAt {definition.get('StartAt')!r} is not a state")
    for name, state in states.items():
        kind = state.get("Type")
        unknown = set(state) - _FIELDS.get(kind, set())
        if kind not in _FIELDS:
            problems.append(f"{name}: state type {kind!r} is not modelled")
        elif unknown:
            problems.append(f"{name}: fields not modelled: {sorted(unknown)}")
        for target in targets(state):
            if target not in states:
                problems.append(f"{name}: transition to unknown state {target!r}")
        if kind in ("Pass", "Wait", "Task") and "Next" not in state and not state.get("End"):
            problems.append(f"{name}: neither Next nor End")
        if kind == "Choice" and "Default" not in state:
            problems.append(f"{name}: a Choice without Default fails the execution when no rule matches")
    seen, todo = set(), [definition.get("StartAt")]
    while todo:
        name = todo.pop()
        if name in seen or name not in states:
            continue
        seen.add(name)
        todo += targets(states[name])
    problems += [f"{name}: unreachable" for name in states if name not in seen]
    if not any(s.get("Type") in ("Succeed", "Fail") or s.get("End") for s in states.values()):
        problems.append("no terminal state")
    return problems


# ---------------------------------------------------------------------------------------------- execution
class Machine:
    def __init__(self, definition: dict, *, lambdas: dict[str, Callable[[Any], Any]], sns: Callable[..., Any] | None = None,
                 on_wait: Callable[[int], None] | None = None, max_transitions: int = 1000):
        self.definition, self.lambdas, self.sns, self.on_wait, self.max_transitions = definition, lambdas, sns, on_wait, max_transitions
        problems = lint(definition)
        if problems:
            raise Unsupported("; ".join(problems))

    def run(self, execution_input: Any, execution_id: str = "arn:aws:states:us-east-1:123456789012:execution:machine:test") -> Execution:
        context = {"Execution": {"Id": execution_id, "Input": execution_input}}
        ex = Execution("RUNNING")
        data, name = copy.deepcopy(execution_input), self.definition["StartAt"]
        try:
            for _ in range(self.max_transitions):
                state = self.definition["States"][name]
                ex.path.append(name)
                self._check_size(data, name)
                kind = state["Type"]
                if kind == "Succeed":
                    ex.status, ex.output = "SUCCEEDED", data
                    return ex
                if kind == "Fail":
                    ex.status, ex.error, ex.cause = "FAILED", state.get("Error"), state.get("Cause")
                    return ex
                effective = get_path(state.get("InputPath", "$"), data, context)
                if kind == "Choice":
                    name = self._choose(state, effective, context)
                    continue
                if kind == "Wait":
                    ex.waited += int(state["Seconds"])
                    if self.on_wait:
                        self.on_wait(int(state["Seconds"]))
                    result, next_name = effective, None
                elif kind == "Pass":
                    value = render(state["Parameters"], effective, context) if "Parameters" in state else state.get("Result", effective)
                    result, next_name = self._result(state, effective, value), None
                else:
                    result, next_name = self._task(name, state, effective, context, ex)
                if next_name is None:
                    data = get_path(state.get("OutputPath", "$"), result, context)
                    if state.get("End"):
                        ex.status, ex.output = "SUCCEEDED", data
                        return ex
                    name = state["Next"]
                else:  # a Catch: its ResultPath was applied to the state's input, OutputPath does not apply
                    data, name = result, next_name
            raise Unsupported(f"more than {self.max_transitions} transitions: the machine loops")
        except StatesRuntime as e:
            ex.status, ex.error, ex.cause = "FAILED", "States.Runtime", str(e)
            return ex
        except TaskError as e:  # no Retry left and no Catch matched
            ex.status, ex.error, ex.cause = "FAILED", e.error, e.cause
            return ex

    # -- pieces
    @staticmethod
    def _check_size(data: Any, name: str) -> None:
        size = len(json.dumps(data, ensure_ascii=False).encode("utf-8", "surrogatepass"))  # (half a surrogate pair is counted, not choked on)
        if size > MAX_PAYLOAD:
            raise StatesRuntime(f"States.DataLimitExceeded: the state entering {name} is {size} bytes (limit {MAX_PAYLOAD})")

    @staticmethod
    def _result(state: dict, effective_input: Any, value: Any) -> Any:
        if "ResultPath" not in state:
            return value
        if state["ResultPath"] is None:
            return effective_input
        return set_path(effective_input, state["ResultPath"], value)

    def _rule(self, rule: dict, data: Any, context: dict) -> bool:
        if "And" in rule:
            return all(self._rule(r, data, context) for r in rule["And"])
        if "Or" in rule:
            return any(self._rule(r, data, context) for r in rule["Or"])
        if "Not" in rule:
            return not self._rule(rule["Not"], data, context)
        ops = set(rule) - {"Variable", "Next", "Comment"}
        if len(ops) != 1 or not ops <= _RULE_OPS:
            raise Unsupported(f"choice rule not modelled: {rule}")
        op = ops.pop()
        if op == "IsPresent":
            return has_path(rule["Variable"], data, context) is rule[op]
        value = get_path(rule["Variable"], data, context)  # a missing variable is States.Runtime, as in the service
        if op == "StringEquals":
            return isinstance(value, str) and value == rule[op]
        if op == "BooleanEquals":
            return isinstance(value, bool) and value is rule[op]
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value == rule[op]

    def _choose(self, state: dict, data: Any, context: dict) -> str:
        for rule in state["Choices"]:
            if self._rule(rule, data, context):
                return rule["Next"]
        return state["Default"]

    def _invoke(self, name: str, state: dict, params: Any, ex: Execution) -> Any:
        resource = state["Resource"]
        if resource == LAMBDA_INVOKE:
            extra = set(params) - {"FunctionName", "Payload"}
            if extra:
                raise Unsupported(f"{name}: lambda:invoke parameters not modelled: {sorted(extra)}")
            fn = self.lambdas.get(params["FunctionName"])
            if fn is None:
                raise TaskError("Lambda.ResourceNotFoundException", f"no function {params['FunctionName']}")
            ex.invocations.append((name, params["FunctionName"]))
            try:
                payload = fn(json.loads(json.dumps(params.get("Payload"))))  # what crosses the wire is JSON
            except ServiceError as e:
                raise TaskError(e.name, "simulated Lambda service error") from e
            except Exception as e:  # a function error: Error = the exception's class name, Cause = Lambda's error document
                raise TaskError(type(e).__name__, json.dumps({"errorMessage": str(e), "errorType": type(e).__name__, "stackTrace": []},
                                                              ensure_ascii=False)) from e
            payload = json.loads(json.dumps(payload))
            return {"ExecutedVersion": "$LATEST", "Payload": payload, "StatusCode": 200}
        if resource == SNS_PUBLISH:
            extra = set(params) - {"TopicArn", "Subject", "Message"}
            if extra:
                raise Unsupported(f"{name}: sns:publish parameters not modelled: {sorted(extra)}")
            ex.invocations.append((name, SNS_PUBLISH))
            subject, message = params.get("Subject"), params.get("Message")
            if not isinstance(message, str) or not message:
                raise TaskError("SNS.InvalidParameterException", "Invalid parameter: Empty message")
            if subject is not None and (not isinstance(subject, str) or not subject.isascii() or len(subject) > 100 or "\n" in subject
                                        or not subject.strip() or subject != subject.lstrip()):
                raise TaskError("SNS.InvalidParameterException", "Invalid parameter: Subject")
            try:
                self.sns and self.sns(TopicArn=params["TopicArn"], Subject=subject, Message=message)
            except ServiceError as e:
                raise TaskError(e.name, "simulated SNS error") from e
            return {"MessageId": "00000000-0000-0000-0000-000000000000"}
        raise Unsupported(f"{name}: resource not modelled: {resource}")

    @staticmethod
    def _matches(names: list[str], error: str) -> bool:
        return "States.ALL" in names or "States.TaskFailed" in names or error in names

    def _task(self, name: str, state: dict, data: Any, context: dict, ex: Execution) -> tuple[Any, str | None]:
        params = render(state["Parameters"], data, context) if "Parameters" in state else data
        attempts: dict[int, int] = {}
        while True:
            try:
                raw = self._invoke(name, state, params, ex)
                if "ResultSelector" in state:
                    raw = render(state["ResultSelector"], raw, context)
                return self._result(state, data, raw), None
            except TaskError as e:
                retried = False
                for i, retrier in enumerate(state.get("Retry", [])):
                    if self._matches(retrier["ErrorEquals"], e.error):
                        if attempts.get(i, 0) < retrier.get("MaxAttempts", 3):
                            attempts[i] = attempts.get(i, 0) + 1
                            retried = True
                        break
                if retried:
                    continue
                for catcher in state.get("Catch", []):
                    if self._matches(catcher["ErrorEquals"], e.error):
                        info = {"Error": e.error, "Cause": str(e.cause)[:32768]}  # the service cuts a Cause at 32,768 characters
                        return set_path(data, catcher.get("ResultPath", "$"), info), catcher["Next"]
                raise
