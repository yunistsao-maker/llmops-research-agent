"""Three-way merge of one JSON document, record by record (``portalctl pull`` uses it for the JSON files under data/).

A file of research data is edited from two sides: the research agent publishes releases, a person edits a checkout.
Merged file by file, one record added on each side made the whole file a conflict - and one side's work got lost.
Here both edited versions are compared with the version they started from (the base), value by value:

* object: per key.  What only one side added, changed or removed is taken from that side; the same change on both
  sides is taken once; two different changes are a conflict at that key's JSON pointer.
* list whose elements are all records (``patch.record_key``, the key the agent and the pages address a record by -
  unique in the list): per record, the same way.  A record removed on one side and changed on the other is a conflict.
  Order: when only one side changed which records the list holds (or their order), that side's order; otherwise the
  base order, then the records the release added, then those the checkout added.  When both sides changed the list
  and one of them also reordered it, there is no order that keeps both: a conflict at the list.
* any other list (strings, elements without a record key): taken from the side that changed it; both changed it, a conflict.
* the top-level ``asOf`` / ``generatedAt`` stamps, moved on both sides: the later point in time.

Nothing here writes, and a conflict is reported, never decided.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from research.portal.checks import loads_strict
from research.portal.patch import record_key

#: top-level fields that say when a file was brought up to date: when both sides moved one, the later value is taken
STAMPS = ("asOf", "generatedAt")


class _Missing:
    """The value at a place one version does not have (never added, or removed)."""

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


class Unmergeable(ValueError):
    """A version of the file this merge cannot read.  ``side``: "base", "release", "checkout" (or "any": one of them);
    ``why``: "not JSON" or "nested too deeply"."""

    def __init__(self, side: str, why: str):
        super().__init__(f"{side}: {why}")
        self.side, self.why = side, why


@dataclass
class Clash:
    """A place both sides changed differently: its JSON pointer and the two values (``MISSING``: that side removed it).
    A record in a list is addressed by its key, as in the agent's operations (``/events/<id>/date``)."""

    pointer: str
    release: Any
    checkout: Any


@dataclass
class Merge:
    value: Any = None                                  # the merged document (only meaningful without clashes)
    clashes: list[Clash] = field(default_factory=list)
    from_release: int = 0                              # changes taken from the release: a record, a field, a whole list
    from_checkout: int = 0                             # ... and from the checkout
    #: "release" / "checkout" when the merged document is that side's version as it is (``merge_bytes``): then that
    #: side's file is the result, byte for byte
    equals: str | None = None


def same(a: Any, b: Any) -> bool:
    """Equal as JSON values.  ``1``, ``1.0`` and ``true`` are three values (Python's ``==`` says one): a change from one
    to another is a change.  The order of an object's keys is not."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b  # (MISSING == MISSING by identity)


def segment(key: str) -> str:
    """One JSON pointer segment (RFC 6901 escaping)."""
    return key.replace("~", "~0").replace("/", "~1")


def instant(value: Any) -> dt.datetime | None:
    """The point in time an ISO date or timestamp names (no zone: UTC); None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def _record_keys(value: list) -> list[tuple[str, str]] | None:
    """The record key of every element, in order - None unless every element is a record and no key occurs twice."""
    keys = [record_key(el) for el in value]
    if any(k is None for k in keys) or len(set(keys)) != len(keys):
        return None
    return keys  # type: ignore[return-value]


def _order(base: list, release: list, checkout: list, keep: list, *, strict: bool) -> list | None:
    """The order of ``keep`` (the keys the merged container holds) - see the module's docstring.  ``strict`` (lists of
    records): None when both sides changed the sequence and one of them reordered what it kept of the base."""
    if release == base:
        order = checkout
    elif checkout == base or checkout == release:
        order = release
    else:
        held, in_base = set(keep), set(base)
        if strict:
            for side in (release, checkout):
                present = set(side)
                if [k for k in side if k in held and k in in_base] != [k for k in base if k in held and k in present]:
                    return None
        order = list(base) + [k for k in release if k not in in_base]
        seen = in_base | set(release)
        order += [k for k in checkout if k not in seen]
    held = set(keep)
    out = [k for k in order if k in held]
    listed = set(out)
    return out + [k for k in keep if k not in listed]  # (nothing, unless the document is already in conflict)


class _Merger:
    def __init__(self) -> None:
        self.result = Merge()

    def clash(self, pointer: str, release: Any, checkout: Any) -> Any:
        self.result.clashes.append(Clash(pointer, release, checkout))  # ("": the document as a whole)
        return checkout  # a stand-in: a document with a clash is not written

    def value(self, base: Any, release: Any, checkout: Any, pointer: str) -> Any:
        if same(release, checkout):
            return release
        if same(checkout, base):
            self.result.from_release += 1
            return release
        if same(release, base):
            self.result.from_checkout += 1
            return checkout
        # changed on both sides, differently
        if any(v is MISSING for v in (base, release, checkout)):  # added on both sides, or removed on one and changed on the other
            return self.clash(pointer, release, checkout)
        if isinstance(base, dict) and isinstance(release, dict) and isinstance(checkout, dict):
            return self.merge_object(base, release, checkout, pointer)
        if isinstance(base, list) and isinstance(release, list) and isinstance(checkout, list):
            return self.merge_list(base, release, checkout, pointer)
        return self.clash(pointer, release, checkout)

    def merge_object(self, base: dict, release: dict, checkout: dict, pointer: str) -> dict:
        out: dict[str, Any] = {}
        for key in dict.fromkeys([*base, *release, *checkout]):
            was, theirs, mine = base.get(key, MISSING), release.get(key, MISSING), checkout.get(key, MISSING)
            if pointer == "" and key in STAMPS and _both_moved(was, theirs, mine):
                if (instant(mine), mine) > (instant(theirs), theirs):  # (equal instants spelled differently: the larger string)
                    self.result.from_checkout += 1
                    out[key] = mine
                else:
                    self.result.from_release += 1
                    out[key] = theirs
                continue
            merged = self.value(was, theirs, mine, f"{pointer}/{segment(key)}")
            if merged is not MISSING:
                out[key] = merged
        order = _order(list(base), list(release), list(checkout), list(out), strict=False) or []
        return {k: out[k] for k in order}

    def merge_list(self, base: list, release: list, checkout: list, pointer: str) -> Any:
        keys = [_record_keys(v) for v in (base, release, checkout)]
        if keys[0] is None or keys[1] is None or keys[2] is None:
            return self.clash(pointer, release, checkout)  # not a list of records, and changed on both sides
        kb, kr, km = keys
        was, theirs, mine = dict(zip(kb, base)), dict(zip(kr, release)), dict(zip(km, checkout))
        out: dict[tuple[str, str], Any] = {}
        for key in dict.fromkeys([*kb, *kr, *km]):
            merged = self.value(was.get(key, MISSING), theirs.get(key, MISSING), mine.get(key, MISSING), f"{pointer}/{segment(key[1])}")
            if merged is not MISSING:
                out[key] = merged
        order = _order(kb, kr, km, list(out), strict=True)
        if order is None:
            return self.clash(pointer, release, checkout)
        return [out[k] for k in order]


def _both_moved(base: Any, release: Any, checkout: Any) -> bool:
    """A stamp that both sides set, to two different points in time (removing one is not moving it)."""
    if release is MISSING or checkout is MISSING or same(release, checkout) or same(release, base) or same(checkout, base):
        return False
    return instant(release) is not None and instant(checkout) is not None


def merge(base: Any, release: Any, checkout: Any) -> Merge:
    """Merge the release's and the checkout's version of one JSON document with the version both started from."""
    merger = _Merger()
    merger.result.value = merger.value(base, release, checkout, "")
    return merger.result


def merge_bytes(base: bytes, release: bytes, checkout: bytes) -> Merge:
    """``merge`` of three versions of a file.  Raises ``Unmergeable`` when one of them is not JSON for every parser
    (``loads_strict``) or is nested too deeply to be compared."""
    docs = []
    for side, raw in (("base", base), ("release", release), ("checkout", checkout)):
        try:
            docs.append(loads_strict(raw))
        except RecursionError:
            raise Unmergeable(side, "nested too deeply") from None
        except ValueError:  # (a JSONDecodeError and a UnicodeDecodeError are ValueErrors)
            raise Unmergeable(side, "not JSON") from None
    try:
        result = merge(*docs)
        if not result.clashes:
            result.equals = "release" if same(result.value, docs[1]) else "checkout" if same(result.value, docs[2]) else None
    except RecursionError:  # (deeper than this merge can follow, though not than the parser could)
        raise Unmergeable("any", "nested too deeply") from None
    return result


def dump(value: Any) -> bytes:
    """A data file the way the portal repo writes one: two-space indent, UTF-8 as it is, a line feed at the end."""
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
