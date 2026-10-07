"""From an accepted research proposal (or a portal checkout) to a live release.

Nothing here trusts the proposal: it is replayed on the data that is live *now* with the same code the agent used to
validate it, the result must pass the portal's schema and cross-file checks, and only research data files may change.
A proposal either becomes a new release, waits for a person (``pending``), or is refused with the reasons.

Whether a proposal may go live on its own is decided from what it would really change - the difference between the
live data and the replayed result - and never from how its operations are spelled: an operation called "replace" that
drops records is a deletion, and one operation that rewrites forty fields is forty changes.

    publish_proposal   agent proposal -> release   (automatic, or after approval)
    approve / discard  decide a pending proposal
    publish_tree       portal checkout -> release  (first release, test results, hand edits)
    rollback           point the UI back at an earlier release (and pause automatic publishing, in the same write)
    pause / resume     stop / restart automatic publishing: while paused every proposal waits for approval
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from research.apply import MAX_PROPOSAL_VALUES, already_applied, apply_saved_proposal, changed_files, dump_json, envelope_problems, too_many_values
from research.portal.checks import (Problem, advisory, blocking, https_url_problem, new_problems, non_finite, shorten, validate_definition,
                                    validate_docs)
from research.portal.data import SCHEMA_PATH, PortalData, is_data_path
from research.portal import labels as L
from research.portal.diff import ADDED, MODIFIED, REMOVED, REORDERED, Change, diff_docs
from research.validate.injection import scan, visible
from research.validate.urls import known_set, links_of, normalize_url, unknown_links

from . import releases as R
from .storage import Conflict, NotFound, Store

PROPOSAL_SCHEMA = "llmops-research-proposal/v1"
PENDING_SCHEMA = "portal-pending/v1"
PENDING = "pending/"
DECIDED = "decided/"
REJECTED = "rejected/"
SOURCES = "data/sources.json"
CATALOG = "data/catalog.json"
_PROPOSAL_ID = re.compile(r"prop-[A-Za-z0-9][A-Za-z0-9._-]{0,80}")  # used with fullmatch
_SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_OPS_EVER = 500
#: a proposal is a few kilobytes of operations and quotes (the agent refuses to write one above 600 KB)
MAX_PROPOSAL_BYTES = 2_000_000
_TIERS = ("official", "independent", "community")
#: validation.flags kinds this code can explain; any other kind is held all the same (see _suspicion_holds)
_KNOWN_FLAGS = ("instructionLikeText", "unseenUrls", "digestNotReread", "uncertainSubject", "uncheckedValue", "digestUnsourced")
ATTEMPTS = 4
#: how much of a change is kept in release.json / a pending proposal (the audit trail) and in the state machine's state
AUDIT_CHANGES, AUDIT_VALUE = 2000, 4000
STATE_CHANGES, STATE_VALUE = 30, 200
CLIPPED = "__truncated__"
#: an approval that was started this long ago and never finished belonged to a run that died: somebody else may decide
STALE_CLAIM = dt.timedelta(minutes=15)
#: Files that people and tools publish (the agent never reads or writes them) and the UI draws pages from; the same
#: file -> type table as the portal's own tools/validate_data.py.
TOOL_FILE_TYPES = (
    ("data/internal/platform-inventory*.json", "PlatformInventory"),
    ("runs/perf/*.json", "PerfRun"),
    ("runs/compat/*.json", "CompatRun"),
)
#: maintained by the platform team (requirements, release gates, metric definitions): research may suggest, a person decides
_TEAM_FILES = ("data/requirements.json",)
#: what a model in the catalog *is*: platformStatus was decided for this name and vendor id, so they do not change unseen
_IDENTITY = re.compile(r"/models/[^/]+/(name|vendor|family|vendorModelId)(/.*)?")
_ACCESSED = re.compile(r"/sources/([^/]+)/accessed")


@dataclass(frozen=True)
class Policy:
    """When may a proposal go live without a person looking at it?"""

    mode: str = "auto"  # "auto": publish when every gate passes · "review": every proposal waits for approval
    max_ops: int = 60  # in auto mode, a proposal that changes more values (or has more operations) than this waits
    max_op_changes: int = 12  # ... and so does one in which a single operation rewrites more existing values than this
    allow_remove: bool = False  # in auto mode, a proposal that removes anything waits for approval
    hold_on_suspicious: bool = True  # ... and so does one built on, or containing, instruction-like text
    allow_sample: bool = False  # proposals made against the agent's bundled SAMPLE data are refused

    def __post_init__(self) -> None:
        if self.mode not in ("auto", "review"):
            raise ValueError(f"publish mode must be 'auto' or 'review', got {self.mode!r}")


# ---------------------------------------------------------------------------------------------- small helpers
def _flat(value: Any, limit: int) -> str:
    """Anything as one line of at most ``limit`` characters (it may come from a web page or from a model)."""
    try:
        # what a reader cannot see is taken out and every control character becomes a blank (an ESC sequence in a
        # reason somebody typed must not reach a terminal or a mail as one)
        text = " ".join(visible(str(value if value is not None else "")).split())
    except Exception:  # e.g. a value nested so deeply that it cannot even be printed
        text = f"<{type(value).__name__}>"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _clip(value: Any, width: int | None) -> Any:
    if width is None:
        return value
    text = json.dumps(value, ensure_ascii=False)
    return value if len(text) <= width else {CLIPPED: text[:width]}


def _instant(text: Any) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def proposal_sha256(proposal: Any) -> str:
    """The identity of a proposal's content: what an approval refers to and what a release was made from."""
    return hashlib.sha256(json.dumps(proposal, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------- what would change
@dataclass
class Review:
    """What publishing a proposal would really change, computed from the live data and the replayed result."""

    base: str  # the release the comparison was made against
    changes: list[Change] = field(default_factory=list)  # what a reader of the portal would see differently
    stamps: list[Change] = field(default_factory=list)  # bookkeeping the replay writes itself: generatedAt, asOf, accessed
    as_of: dict[str, str] = field(default_factory=dict)  # file -> the date it is now said to be verified as of
    new_sources: list[dict] = field(default_factory=list)
    digest: dict | None = None

    def count(self, *kinds: str) -> int:
        return sum(1 for c in self.changes if c.kind in kinds)

    def to_doc(self, limit: int | None = None, width: int | None = None) -> dict:
        """As JSON.  With ``limit`` / ``width`` the size of the result is bounded whatever the proposal holds
        (a path is made of keys the proposal chose, so it is cut as well)."""
        shown = self.changes if limit is None else self.changes[:limit]
        text = 400 if width is None else min(width, 400)
        entries = []
        for c in shown:
            entry: dict[str, Any] = {"file": _flat(c.file, 160), "path": c.path if width is None else _flat(c.path, 300), "kind": c.kind}
            if c.kind != ADDED:
                entry["before"] = _clip(c.before, width)
            if c.kind != REMOVED:
                entry["after"] = _clip(c.after, width)
            entries.append(entry)
        counts = {MODIFIED: self.count(MODIFIED), REMOVED: self.count(REMOVED), ADDED: self.count(ADDED), "stamps": len(self.stamps)}
        if self.count(REORDERED):
            counts[REORDERED] = self.count(REORDERED)
        doc: dict[str, Any] = {"base": self.base, "counts": counts, "changes": entries}
        if len(shown) < len(self.changes):
            doc["more"] = len(self.changes) - len(shown)
        if self.as_of:
            doc["asOf"] = {_flat(k, 160): v for k, v in sorted(self.as_of.items())[:40]}
        if self.new_sources:
            listed = self.new_sources if limit is None else self.new_sources[: max(limit // 3, 1)]
            doc["newSources"] = [{"id": _flat(s.get("id"), 80), "publisher": _flat(s.get("publisher"), 80), "type": _flat(s.get("type"), 40),
                                  "title": _flat(s.get("title"), text), "url": _flat(s.get("url"), text)} for s in listed]
            if len(listed) < len(self.new_sources):
                doc["moreSources"] = len(self.new_sources) - len(listed)
        if self.digest is not None:
            sections = {_flat(k, 60): len(v) for k, v in self.digest.items() if isinstance(v, list)}
            doc["digest"] = {"id": _flat(self.digest.get("id"), 60), "headline": _flat(self.digest.get("headline"), 500),
                             "period": _flat(f"{self.digest.get('periodStart', '')} – {self.digest.get('periodEnd', '')}", 60),
                             "sections": dict(sorted(sections.items())[:30])}
        return doc


def build_review(portal: PortalData, staged: PortalData, proposal: dict, base: str) -> Review:
    created = proposal.get("createdAt")
    today = proposal.get("today") or str(created)[:10]
    as_of_files = set(_list(proposal.get("asOfFiles")))
    touched = set(_list(proposal.get("touchSources")))
    review = Review(base)
    for c in diff_docs(portal.docs, staged.docs):
        accessed = _ACCESSED.fullmatch(c.path) if c.file == SOURCES else None
        if c.kind == MODIFIED and c.path == "/generatedAt" and c.after == created:
            review.stamps.append(c)
        elif c.kind == MODIFIED and c.path == "/asOf" and c.after == today and c.file in as_of_files:
            review.stamps.append(c)
            review.as_of[c.file] = today
        elif c.kind == MODIFIED and accessed and c.after == today and accessed[1].replace("~1", "/").replace("~0", "~") in touched:
            review.stamps.append(c)
        else:
            review.changes.append(c)
            if c.kind == ADDED and c.file == SOURCES and c.path.startswith("/sources/") and isinstance(c.after, dict):
                review.new_sources.append(c.after)
            if c.kind == ADDED and c.path == "" and c.file.startswith("data/digests/") and isinstance(c.after, dict):
                review.digest = c.after
    return review


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def coarse_operations(proposal: dict, effects: list[list[Change]], limit: int) -> list[str]:
    """Operations that each rewrite more than ``limit`` values that existed (one piece of evidence cannot carry that much).

    ``effects``: what each operation really did when the proposal was replayed (``research.apply.stage_proposal``) - the
    same account the agent's validator judges an operation by.
    """
    ops = _list(proposal.get("ops"))
    out = []
    for i, effect in enumerate(effects):
        touched = sum(1 for c in effect if c.kind in (MODIFIED, REMOVED))
        if touched > limit:
            op = _dict(ops[i]) if i < len(ops) else {}
            out.append(f"第 {i + 1} 個 operation（{_flat(op.get('file'), 80)}{_flat(op.get('path'), 160)}）一次改寫了 {touched} 個原有的值")
    return out


# ---------------------------------------------------------------------------------------------- outcome
@dataclass
class Outcome:
    status: str  # published | pending | rejected | unchanged | already_published | rolled_back | discarded | paused | resumed | preview
    kind: str = "agent"  # agent | manual | bootstrap | rollback | control
    release_id: str | None = None
    previous_release_id: str | None = None
    reasons: list[str] = field(default_factory=list)
    changes: dict[str, list[str]] = field(default_factory=dict)
    proposal: dict = field(default_factory=dict)
    sequence: int | None = None
    review: Review | None = None
    warnings: list[str] = field(default_factory=list)  # things that went wrong after the outcome was already decided
    operations: list[dict] = field(default_factory=list)  # the proposal's rationale and quotes (for the message; not kept in the state)
    #: the difference as release.json recorded it, for an outcome that reports a release made earlier (a retried request)
    review_doc: dict | None = None

    @property
    def ok(self) -> bool:
        return self.status != "rejected"

    def to_state(self) -> dict:
        """JSON-safe and small (it travels through Step Functions, whose payloads are limited to 256 KB)."""
        state: dict[str, Any] = {
            "status": self.status, "kind": self.kind, "releaseId": self.release_id, "previousReleaseId": self.previous_release_id,
            "sequence": self.sequence, "reasons": [_flat(r, 400) for r in self.reasons[:12]],
            "changes": {k: [_flat(x, 200) for x in v[:30]] for k, v in self.changes.items() if k in ("added", "modified", "removed")},
            "changed": R.changed_count(self.changes),
            "proposal": self.proposal, "warnings": [_flat(w, 300) for w in self.warnings[:6]],
        }
        if len(self.reasons) > 12:
            state["reasons"].append(f"…另有 {len(self.reasons) - 12} 項")
        if self.review is not None:
            state["review"] = self.review.to_doc(limit=STATE_CHANGES, width=STATE_VALUE)
        elif isinstance(self.review_doc, dict):
            state["review"] = _small_review(self.review_doc)
        return state


def _small_review(doc: dict) -> dict:
    """A difference that was stored in full (release.json), cut to what the state machine's state may carry."""
    out: dict[str, Any] = {"base": _flat(doc.get("base"), 60), "counts": {_flat(k, 20): v for k, v in _dict(doc.get("counts")).items() if isinstance(v, int)}}
    changes = [c for c in _list(doc.get("changes")) if isinstance(c, dict)]
    out["changes"] = [{"file": _flat(c.get("file"), 160), "path": _flat(c.get("path"), 300), "kind": _flat(c.get("kind"), 20),
                       **({"before": _clip(c.get("before"), STATE_VALUE)} if "before" in c else {}),
                       **({"after": _clip(c.get("after"), STATE_VALUE)} if "after" in c else {})} for c in changes[:STATE_CHANGES]]
    more = (doc.get("more") if isinstance(doc.get("more"), int) else 0) + max(len(changes) - STATE_CHANGES, 0)
    if more:
        out["more"] = more
    return out


# ---------------------------------------------------------------------------------------------- inputs
def split_record(doc: Any) -> tuple[dict | None, dict | None]:
    """(proposal, run record).  Accepts what the agent saves: ``runs/<runId>.json`` or ``proposals/<id>.json``."""
    if not isinstance(doc, dict):
        return None, None
    if doc.get("schema") == PROPOSAL_SCHEMA:
        return doc, None
    if "status" in doc or doc.get("type") == "result":
        proposal = doc.get("proposal")
        return (proposal if isinstance(proposal, dict) else None), doc
    return None, None


def brief(proposal: Any, run: Any = None) -> dict:
    """The few facts about a proposal worth carrying into notifications, history and the state machine.

    Every field is forced into a short single line: all of it is data that somebody else wrote.
    """
    if not isinstance(proposal, dict):
        return {}
    digest = _dict(proposal.get("digest"))
    ops = [o for o in _list(proposal.get("ops")) if isinstance(o, dict)]
    by_file: dict[str, int] = {}
    for op in ops:
        name = _flat(op.get("file"), 120)
        by_file[name] = by_file.get(name, 0) + 1
    manifest = _dict(_dict(proposal.get("portal")).get("manifest"))
    out: dict[str, Any] = {
        "id": _flat(proposal.get("id"), 100),
        "createdAt": _flat(proposal.get("createdAt"), 40),
        "summary": _flat(proposal.get("summary"), 1200),
        "ops": len(ops),
        "opsByFile": dict(sorted(by_file.items())[:20]),
        "removes": sum(1 for o in ops if o.get("op") == "remove"),
        "newSources": len(_list(proposal.get("newSources"))),
        "unverified": len(_list(proposal.get("unverified"))),
        "evidence": len(_dict(proposal.get("evidence"))),
        "basedOn": _flat(manifest.get("releaseId") or manifest.get("kind") or "", 100),
        "task": _flat(_dict(proposal.get("task")).get("kind"), 40),
    }
    if digest:
        out["digestId"] = _flat(digest.get("id"), 40)
        out["digestHeadline"] = _flat(digest.get("headline"), 500)
    if isinstance(run, dict) and run.get("runId"):
        out["runId"] = _flat(run["runId"], 100)
    return out


def portal_view(files: dict[str, bytes], release_id: str) -> PortalData:
    """The part of a release the research data model knows about (schema + research files), parsed."""
    parsed: dict[str, Any] = {}
    for rel, raw in files.items():
        if rel == SCHEMA_PATH or is_data_path(rel):
            try:
                parsed[rel] = json.loads(raw)
            except ValueError as e:
                raise R.ReleaseError(f"{rel} is not valid JSON: {e}") from e
    if SCHEMA_PATH not in parsed:
        raise R.ReleaseError(f"the data has no {SCHEMA_PATH}")
    return PortalData.from_mapping(parsed, {"kind": "release", "releaseId": release_id})


# ---------------------------------------------------------------------------------------------- gates
def refusals(proposal: Any, run: Any, policy: Policy, now: dt.datetime | None = None) -> list[str]:
    """Reasons this proposal can never be published as it is (approval does not help: the research must be redone)."""
    if run is not None:
        if not isinstance(run, dict) or run.get("status") != "accepted":
            status = run.get("status") if isinstance(run, dict) else None
            reason = f"這次研究的結果是 {_flat(status, 40)!r}，沒有通過驗證的提案"
            error = run.get("error") if isinstance(run, dict) else None
            return [reason + (f"：{_flat(error, 300)}" if error else "")]
    if not isinstance(proposal, dict) or proposal.get("schema") != PROPOSAL_SCHEMA:
        return [f"這不是 research proposal（schema 應為 {PROPOSAL_SCHEMA}）"]
    # Said before anything is worked through: every later step takes time and memory by the value.
    if too_many_values(proposal):
        return [f"提案裡的值（文字、數字、陣列、物件，全部算在內）超過 {MAX_PROPOSAL_VALUES:,} 個：這比整個 portal 的資料還多，不是一次研究會寫出來的內容"]
    out = [f"提案的格式不正確：{_flat(p, 300)}" for p in envelope_problems(proposal)]
    if not isinstance(proposal.get("id"), str) or not _PROPOSAL_ID.fullmatch(proposal["id"]):
        out.append(f"proposal id 格式不正確：{_flat(proposal.get('id'), 60)!r}")
    for key, kind in (("evidence", dict), ("portal", dict), ("validation", dict), ("stats", dict), ("task", dict), ("unverified", list), ("summary", str)):
        if proposal.get(key) is not None and not isinstance(proposal[key], kind):
            out.append(f"提案的格式不正確：{key} 應為 {'物件' if kind is dict else '陣列' if kind is list else '字串'}")
    if out:
        return out  # the checks below read fields whose shape was just found wanting
    out += _shape_refusals(proposal)
    if out:
        return out

    report = _dict(run).get("report")
    if report is not None and not isinstance(report, dict):
        out.append("agent 的驗證報告格式不正確")
    elif report and (report.get("accepted") is False or report.get("errors")):
        out.append("agent 的驗證報告有錯誤：" + "；".join(_flat(e, 200) for e in _list(report.get("errors"))[:3]))
    if _dict(proposal.get("portal")).get("sample") and not policy.allow_sample:
        out.append("這份提案是依 agent 內建的 SAMPLE 資料產生的，不是目前上線的 portal 資料："
                   "請把 runtime 的 PORTAL_DATA_URI 設成資料位置（s3://<bucket>/portal/）後重新執行")
    ops = _list(proposal.get("ops"))
    if len(ops) > MAX_OPS_EVER:
        out.append(f"提案有 {len(ops)} 個 operation，超過上限 {MAX_OPS_EVER}")
    blind = [f"{_flat(o.get('file'), 80)}{_flat(o.get('path'), 120)}" for o in ops if o.get("op") in ("replace", "remove") and "before" not in o]
    if blind:
        out.append("提案的 replace / remove 沒有記錄原值（before），無法確認它是依目前的資料提出的：" + "、".join(blind[:3]) + ("…" if len(blind) > 3 else ""))
    bad_number = next(non_finite(proposal), None)
    if bad_number is not None:
        out.append(f"提案含有 NaN 或 Infinity（{_flat(bad_number, 160)}）：這不是任何瀏覽器讀得懂的 JSON 數值")
    elif isinstance(run, dict):
        # the whole record is kept when a proposal waits for approval: every part of it has to be storable
        stray = next(non_finite({k: v for k, v in run.items() if k != "proposal"}), None)
        if stray is not None:
            out.append(f"研究紀錄含有 NaN 或 Infinity（{_flat(stray, 160)}）：這不是有效的 JSON 數值")
    clock = now or R.utc_now()
    created = _instant(proposal.get("createdAt"))
    today = str(proposal.get("today") or str(proposal.get("createdAt"))[:10])
    try:
        day: dt.date | None = dt.date.fromisoformat(today)
    except ValueError:
        day = None
    # Every rule about time below (and the "older than the data" hold) reads these two: a value the rules cannot read
    # is not one they may skip.
    if created is None or day is None:
        out.append(f"提案的 createdAt（{_flat(proposal.get('createdAt'), 40)}）或 today（{_flat(today, 20)}）無法解讀為時間")
        return out
    if created > clock + dt.timedelta(minutes=10):
        out.append(f"提案的 createdAt（{_flat(proposal.get('createdAt'), 40)}）在未來")
    if abs((day - created.astimezone(dt.timezone.utc).date()).days) > 1:
        out.append(f"提案的 today（{today}）和 createdAt（{_flat(proposal.get('createdAt'), 40)}）差了一天以上")
    return out


# ("under": the headings the quote stands under, "header": the column headings of its table - both optional)
_EVIDENCE_TEXT = ("url", "title", "tier", "publisher", "quote", "under", "header", "note", "fetchedAt", "pageSha256")
_EVIDENCE_KEYS = frozenset(_EVIDENCE_TEXT) | {"rendered", "cited"}
_MANIFEST_KIND = re.compile(r"[a-z][a-z0-9-]{0,30}")


def _shape_refusals(proposal: dict) -> list[str]:
    """Parts of a proposal that are printed or stored without being replayed: they must at least be what they claim to
    be.  (The agent writes all of them from its own records; a proposal in which they are something else was not
    written by the agent.)"""
    out: list[str] = []
    if len(json.dumps(proposal, ensure_ascii=False)) > MAX_PROPOSAL_BYTES:
        return [f"提案超過 {MAX_PROPOSAL_BYTES} bytes"]
    evidence = _dict(proposal.get("evidence"))
    wrong = []
    for eid, ev in evidence.items():
        ok = (isinstance(eid, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", eid) and isinstance(ev, dict) and set(ev) <= _EVIDENCE_KEYS
              and all(isinstance(ev.get(k, ""), str) for k in _EVIDENCE_TEXT) and all(isinstance(ev.get(k, False), bool) for k in ("rendered", "cited"))
              and isinstance(ev.get("url"), str) and https_url_problem(ev["url"]) is None and isinstance(ev.get("quote"), str) and ev["quote"].strip()
              and ev.get("tier") in _TIERS)
        if not ok:
            wrong.append(_flat(eid, 40))
    if wrong:
        out.append(f"提案的引文紀錄（evidence）格式不正確（{len(wrong)} 則，例如 {'、'.join(wrong[:3])}）：每一則都要有 https 網址、引文與來源層級")
    bare, unknown, extra = [], [], []
    for i, op in enumerate(o for o in _list(proposal.get("ops")) if isinstance(o, dict)):
        ids = op.get("evidence")
        where = f"第 {i + 1} 個（{_flat(op.get('file'), 80)}{_flat(op.get('path'), 120)}）"
        if not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
            bare.append(where)
        elif any(x not in evidence for x in ids):
            unknown.append(where)
        if op.get("op") == "add" and "before" in op:
            extra.append(where)
    if bare:
        out.append("提案的 operation 沒有引用任何引文：" + _listed(bare, 3))
    if unknown:
        out.append("提案的 operation 引用了不存在的引文編號：" + _listed(unknown, 3))
    if extra:
        out.append("提案的 add operation 帶有 before（新增的內容沒有原值）：" + _listed(extra, 3))
    manifest = _dict(proposal.get("portal")).get("manifest")
    if manifest is not None:
        release, kind = _dict(manifest).get("releaseId"), _dict(manifest).get("kind")
        good = isinstance(manifest, dict) and (release is None or (isinstance(release, str) and R.is_release_id(release))) \
            and (kind is None or (isinstance(kind, str) and _MANIFEST_KIND.fullmatch(kind)))
        if not good:
            out.append("提案記錄的資料版本（portal.manifest）格式不正確")
    return out


def holds(proposal: dict, policy: Policy, review: Review | None = None, coarse: Iterable[str] = (),
          live: PortalData | None = None, staged: PortalData | None = None) -> list[str]:
    """Reasons this (valid) proposal should wait for a person instead of going live automatically.

    ``live`` / ``staged``: the data the proposal was replayed on and the result (what a link or a count is checked against).
    """
    out: list[str] = []
    if policy.mode == "review":
        out.append("發布模式是 review：每一份提案都需要核准")
    ops = [o for o in _list(proposal.get("ops")) if isinstance(o, dict)]
    if len(ops) > policy.max_ops:
        out.append(f"提案有 {len(ops)} 個 operation，超過自動發布的上限 {policy.max_ops}")
    if review is not None:
        out += _diff_holds(review, policy)
        out += _stamp_holds(proposal, review, live, staged)
    out += list(coarse)
    if policy.hold_on_suspicious:
        out += _suspicion_holds(proposal, review, live, staged)
    return out


def _listed(items: Iterable[Any], limit: int = 5, width: int = 160) -> str:
    items = list(items)
    return "、".join(_flat(x, width) for x in items[:limit]) + ("…" if len(items) > limit else "")


def _diff_holds(review: Review, policy: Policy) -> list[str]:
    out: list[str] = []
    removed = [c.where for c in review.changes if c.kind == REMOVED]
    if removed and not policy.allow_remove:
        out.append(f"提案會刪除資料（{len(removed)} 處）：" + _listed(removed))
    bare = L.emptied_citations(review.changes)
    if bare and not policy.allow_remove:
        out.append(f"提案會拿掉資料原有的全部引用來源（{len(bare)} 處），那筆資料之後就沒有出處：" + _listed(bare))
    if len(review.changes) > policy.max_ops:
        out.append(f"提案實際會變更 {len(review.changes)} 處（修改 {review.count(MODIFIED)}、新增 {review.count(ADDED)}、刪除 {review.count(REMOVED)}），"
                   f"超過自動發布的上限 {policy.max_ops}")
    identity = [c.where for c in review.changes if c.file == CATALOG and c.kind != ADDED and _IDENTITY.fullmatch(c.path)]
    if identity:
        out.append("提案會修改既有模型的識別欄位（platformStatus 是針對原本的名稱與 vendor id 核定的）：" + _listed(identity))
    for rel in _TEAM_FILES:
        if any(c.file == rel for c in review.changes + review.stamps):
            out.append(f"提案會修改 {rel}（需求、發布關卡與指標定義由平台團隊維護）")
    computed = L.derived(review.changes)
    if computed:
        out.append("提案新增或修改了標示為 derived 的內容（由其他資料推算，沒有引文可以對照），請確認推算成立：" + _listed(computed))
    # What a program cannot hold against a quote.  Automatic publishing is for what was added and for dates, numbers
    # and versions that changed - each of those is literally in a quote the agent's validator read.  The rest of a
    # record that exists is somebody's earlier work, and replacing it is a person's call.
    moved = [c for c in review.changes if c.kind == REORDERED]
    shuffled = [c.where for c in moved if not L.put_in_front(c)]
    if shuffled:
        out.append("提案會改變清單裡紀錄的順序（頁面顯示的是第一筆，例如第一張價格表）：" + _listed(shuffled))
    in_front = [c.where for c in moved if L.put_in_front(c)]
    if in_front:
        out.append("提案把新的項目放在清單裡既有項目的前面（頁面顯示的是第一筆；新增的內容加在清單最後才會自動發布）：" + _listed(in_front))
    emptied = L.blanked(review.changes)
    if emptied and not policy.allow_remove:
        out.append(f"提案會把原有的文字清空（{len(emptied)} 處），等同刪除：" + _listed(emptied))
    relabelled = L.relabelled(review.changes)
    if relabelled:
        out.append("提案會改變資料的可信度標籤（verification）：" + _listed(f"{where}（{_flat(was, 20)} → {_flat(now, 20)}）" for where, was, now in relabelled))
    plain = [c.where for c in L.unquotable(review.changes) if c.where not in identity]
    if plain:
        out.append(f"提案會修改既有資料的狀態、分類或識別欄位（{len(plain)} 處；這類值沒有引文可以逐字對照）：" + _listed(plain))
    reworded = [c.where for c in L.reworded(review.changes)]
    if reworded:
        out.append(f"提案會改寫 portal 上原有的文字（{len(reworded)} 處；新增的文字會直接發布，改寫的需要人看）：" + _listed(reworded))
    dropped = L.dropped_citations(review.changes)
    if dropped:
        out.append("提案會拿掉資料原有的部分引用來源：" + _listed(dropped))
    recited = [where for where in L.recited(review.changes) if where not in dropped]
    if recited:
        out.append("提案會更動資料原有的引用來源（順序或附註；引用來源只能在最後追加，原有的由人修改）：" + _listed(recited))
    return out


def _counted(entry: Any, expected: int, read_here: int) -> tuple[bool, str]:
    """(is this a complete account of sources read?, how to say what is missing).

    ``entry``: {"cited": n, "read": m} as the agent's validator recorded it; ``expected``: how many sources the
    publisher itself counts in the data; ``read_here``: how many of those are among the pages the run record lists as
    fetched.  All three have to agree that everything was read: the two integers alone are a claim, not an account.
    """
    entry = _dict(entry)
    cited, read = entry.get("cited"), entry.get("read")
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in (cited, read)):
        return False, "沒有記錄讀了它引用的哪些來源"
    if cited != expected:
        return False, f"記錄的是 {cited} 個來源，而它實際引用了 {expected} 個"
    if not (cited > 0 and read >= cited):
        return False, f"只讀了它引用的 {cited} 個來源裡的 {read} 個"
    if read_here < expected:
        return False, f"它引用的 {expected} 個來源裡，研究紀錄的頁面清單只有 {read_here} 個"
    return True, ""


def _pages_read(proposal: dict) -> set[str]:
    """The addresses (normalised) of the pages the run record lists as fetched with text on them: where the fetch was
    asked to go, and where it ended up."""
    out: set[str] = set()
    for page in _list(_dict(proposal.get("stats")).get("pages")):
        chars = page.get("chars") if isinstance(page, dict) else None
        if isinstance(chars, int) and not isinstance(chars, bool) and chars > 0:
            out.update(normalize_url(page[key]) for key in ("url", "requestedUrl") if isinstance(page.get(key), str) and page[key])
    return out


def _quoted_urls(proposal: dict) -> set[str]:
    """The addresses (normalised) of the pages that the quotes cited by the proposal's operations come from - and the
    addresses that led there (a source the portal lists under an address that redirects)."""
    evidence = _dict(proposal.get("evidence"))
    cited = {eid for op in _list(proposal.get("ops")) if isinstance(op, dict) for eid in _list(op.get("evidence")) if isinstance(eid, str)}
    out = {normalize_url(evidence[eid]["url"]) for eid in cited
           if isinstance(evidence.get(eid), dict) and isinstance(evidence[eid].get("url"), str) and str(evidence[eid].get("quote") or "").strip()}
    for page in _list(_dict(proposal.get("stats")).get("pages")):
        if isinstance(page, dict) and isinstance(page.get("url"), str) and isinstance(page.get("requestedUrl"), str) and normalize_url(page["url"]) in out:
            out.add(normalize_url(page["requestedUrl"]))
    return out


def _source_urls(*portals: PortalData | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for portal in portals:
        for src in portal.sources if portal is not None else ():
            if isinstance(src, dict) and isinstance(src.get("id"), str) and isinstance(src.get("url"), str):
                out.setdefault(src["id"], src["url"])
    return out


def _stamp_holds(proposal: dict, review: Review, live: PortalData | None = None, staged: PortalData | None = None) -> list[str]:
    """Claims about *when* something was verified.  Nothing here changes what the data says, so nothing is refused; but
    "checked today" is a statement a reader relies on, and it goes live unseen only when it is backed.

    Which stamps a proposal sets is read from the real difference; which sources a file or a record cites is counted
    here, in the data.  Whether those sources were read is the run's own account - the validator's count
    (validation.asOfCoverage / lastVerifiedCoverage) and the list of pages the run fetched (stats.pages) - and it is
    used only to be stricter: unless both say "all of them", the stamp waits for a person.
    """
    out: list[str] = []
    today = str(proposal.get("today") or str(proposal.get("createdAt"))[:10])
    validation = _dict(proposal.get("validation"))
    pages = _pages_read(proposal)
    urls = _source_urls(staged, live)

    def read_here(ids: list[str]) -> int:
        return sum(1 for sid in ids if sid in urls and normalize_url(urls[sid]) in pages)

    coverage = _dict(validation.get("asOfCoverage"))
    for rel in sorted(review.as_of):
        cited = L.source_ids(live.docs.get(rel)) if live is not None else []
        complete, seen = _counted(coverage.get(rel), len(cited), read_here(cited))
        if not complete:
            out.append(f"提案把 {_flat(rel, 120)} 標記為「今天整份重新確認過」（asOf），但這次研究{seen}")
    odd, unbacked = [], []
    coverage = _dict(validation.get("lastVerifiedCoverage"))
    for c in review.changes:
        for where, value, record in L.stamped(c, "lastVerified"):
            if value != today:
                odd.append(f"{c.file}{where}/lastVerified＝{_flat(value, 30)}")
                continue
            cited = L.source_ids(record)
            complete, seen = _counted(coverage.get(f"{c.file}{where}"), len(cited), read_here(cited))
            if not complete:
                unbacked.append(f"{c.file}{where}（{seen}）")
    if odd:
        out.append(f"lastVerified 代表「這一天對照來源確認過」，只能是提案當天（{_flat(today, 20)}）：" + _listed(odd))
    if unbacked:
        out.append("提案把資料標記為「今天對照來源確認過」（lastVerified），但這次研究沒有讀完它引用的來源：" + _listed(unbacked, width=240))
    # The replay never turns a stamp back, so a proposal older than the data applies without making it look older.  It
    # is still research from before somebody else's update: whether it holds is for a person to say.
    created = _instant(proposal.get("createdAt"))
    overtaken = []
    for rel in sorted({c.file for c in review.changes + review.stamps}) if live is not None and created else []:
        doc = live.docs.get(rel)
        stamp = doc.get("generatedAt") if isinstance(doc, dict) else None
        when = _instant(stamp) if isinstance(stamp, str) else None
        if when and when > created:
            overtaken.append(f"{rel}（{_flat(stamp, 40)}）")
    if overtaken:
        out.append(f"提案產生於 {_flat(proposal.get('createdAt'), 40)}，它要修改的資料在那之後已經更新過，請確認這份提案仍然適用：" + _listed(overtaken))
    # "read today" (accessed) is said of a source when a quote that an operation cites comes from its page
    quoted = _quoted_urls(proposal)
    loose = []
    for c in review.stamps:
        hit = _ACCESSED.fullmatch(c.path) if c.file == SOURCES else None
        if hit:
            sid = hit[1].replace("~1", "/").replace("~0", "~")
            if sid not in urls or normalize_url(urls[sid]) not in quoted:
                loose.append(sid)
    if loose:
        out.append(f"提案把 {len(loose)} 個來源標記為「今天讀過」（accessed），但提案引用的引文沒有一則來自這些頁面：" + _listed(loose))
    return out


def _published_text(proposal: dict, review: Review | None) -> list[tuple[str, str, bool]]:
    """(where, text, may it be searched for links?) for everything a proposal would put in front of people: on the
    portal, in the release's history and audit trail, and in the notification about it.

    A quote and the title of the page it comes from are the page's own words: they are shown as a quotation, next to the
    address they were read at, and a link inside them is part of what was quoted.  Everything else is the proposal's.
    """
    out: list[tuple[str, Any, bool]] = []
    if review is not None:
        out += [(c.where, text, True) for c in review.changes if c.kind != REMOVED for text in _strings(c.after)]
    out.append(("summary", proposal.get("summary"), True))
    for i, claim in enumerate(_list(proposal.get("unverified"))[:200]):
        out += [(f"unverified[{i}]", text, True) for text in _strings(claim)]
    for i, op in enumerate(_list(proposal.get("ops"))):
        if isinstance(op, dict):
            out.append((f"ops[{i}].rationale", op.get("rationale"), True))
    for eid, ev in list(_dict(proposal.get("evidence")).items())[:2000]:
        if isinstance(ev, dict):
            out += [(f"evidence[{_flat(eid, 40)}].{key}", ev.get(key), key in ("publisher", "note"))
                    for key in ("quote", "title", "under", "header", "publisher", "note")]
    return [(where, text, linked) for where, text, linked in out if isinstance(text, str) and text]


def _suspicion_holds(proposal: dict, review: Review | None, live: PortalData | None = None, staged: PortalData | None = None) -> list[str]:
    """Instruction-like text and links nobody read: on a page the research read (the agent's own flags), or in what
    would be published (looked for here, in the real difference and in the proposal's own words).

    The agent's validator hands on what it could not settle as ``validation.flags`` - by kind, never by the wording of a
    message: a warning that happens to contain the word "instruction" (a source called aws-bedrock-system-instructions)
    holds nothing.  Every flag holds, also one of a kind this code has no sentence for.
    """
    out: list[str] = []
    pages = [p.get("url") for p in _list(_dict(proposal.get("stats")).get("pages")) if isinstance(p, dict) and p.get("suspiciousText")]
    raw_flags = _dict(proposal.get("validation")).get("flags")
    flags = _dict(raw_flags)
    if raw_flags and not isinstance(raw_flags, dict):
        out.append(f"agent 的驗證標記（validation.flags）不是這一版讀得懂的格式，請人工確認：{_flat(raw_flags, 200)}")
    flagged = _list(flags.get("instructionLikeText"))
    if pages or flagged:
        what = "、".join(_flat(u, 120) for u in pages[:3]) or _flat(flagged[0], 200)
        out.append(f"讀取的頁面含有像是對 AI 下指令的文字（可能是 prompt injection），請人工確認：{what}")
    unread = _list(flags.get("digestNotReread"))
    if unread:
        out.append(f"digest 有 {len(unread)} 個項目引用的頁面這次沒有重新讀取（只是重述 portal 既有的內容），請確認：" + _listed(unread, limit=3))
    unsure = _list(flags.get("uncertainSubject"))
    if unsure:
        out.append(f"有 {len(unsure)} 個值，程式無法確定引文講的就是這筆紀錄（同一行有別的名稱、別的平台，或讀法不只一種），請對照引文確認：" + _listed(unsure, limit=3, width=300))
    unchecked = _list(flags.get("uncheckedValue"))
    if unchecked:
        out.append(f"有 {len(unchecked)} 個值沒有引文可以逐字對照（版本欄位裡不是版本號的寫法、查不到的公告編號、沒有年份的日期），請確認：" + _listed(unchecked, limit=3, width=300))
    unsourced = _list(flags.get("digestUnsourced"))
    if unsourced:
        out.append(f"digest 有 {len(unsourced)} 處內容沒有這次讀過的頁面可以佐證（整份沒有任何項目，或「未證實」線索裡的日期／金額不在讀過的頁面上），請確認："
                   + _listed(unsourced, limit=3, width=300))
    for kind, items in flags.items():
        # a flag of a kind this code has no sentence for - or one whose value is not the list it should be: it holds all the same
        if items and (kind not in _KNOWN_FLAGS or not isinstance(items, list)):
            out.append(f"agent 的驗證標記了需要人工確認的項目（{_flat(kind, 40)}）：" + _listed(items if isinstance(items, list) else [items], limit=3))
    texts = _published_text(proposal, review)
    hits = list(dict.fromkeys(where for where, text, _linked in texts if scan(text, max_hits=1)))
    if hits:
        out.append("要發布的文字裡有像是對 AI 下指令的內容，請人工確認：" + _listed(hits, limit=3))
    if live is not None or staged is not None:
        # Whatever the run says it read, the publisher only knows the portal: a link is expected when it is a source
        # (one the portal has, or one this proposal adds - where a reviewer sees it as a source) or already on the portal.
        known: set[str] = set()
        for data in (live, staged):
            if data is not None:
                known |= known_set(s.get("url") for s in data.sources if isinstance(s, dict))
        links = unknown_links((text for _where, text, linked in texts if linked), known)
        if links and live is not None:
            links = unknown_links(links, known | links_of(live.docs))
    else:
        links = []
    for u in _list(flags.get("unseenUrls")):  # (an entry that is not text is still an entry: the flag holds)
        shown = u if isinstance(u, str) else _flat(u, 200)
        if shown not in links:
            links.append(shown)
    if links:
        out.append("要發布的文字含有不是 portal 來源的連結（portal 上原本也沒有），請確認它指向哪裡：" + _listed(links, limit=3))
    return out


# ---------------------------------------------------------------------------------------------- agent proposals
def operations(proposal: Any) -> list[dict]:
    """The proposal's operations with their quotes, compact enough to keep inside release.json (the audit trail)."""
    proposal = _dict(proposal)
    evidence = _dict(proposal.get("evidence"))
    out = []
    for op in _list(proposal.get("ops")):
        if not isinstance(op, dict):
            continue
        entry: dict[str, Any] = {k: op.get(k) for k in ("file", "op", "path", "rationale") if k in op}
        for key in ("before", "value"):
            if key in op and not (key == "before" and op.get("op") == "add"):  # what is added had no value before
                text = json.dumps(op[key], ensure_ascii=False)
                entry[key] = op[key] if len(text) <= 2000 else text[:2000] + "…（已截斷）"
        entry["evidence"] = [{k: str(evidence[eid].get(k, ""))[:600] for k in ("url", "tier", "publisher", "quote", "under", "header", "fetchedAt")
                              if k not in ("under", "header") or evidence[eid].get(k)}
                             for eid in _list(op.get("evidence")) if isinstance(eid, str) and isinstance(evidence.get(eid), dict)]
        out.append(entry)
    return out


def _published_before(store: Store, release_id: str, info: dict, sequence: int | None = None) -> Outcome:
    """The answer to a request that already had its effect (the same request, delivered again): what was said the first
    time - "published", with the difference and the release it replaced - read from the release's own record."""
    try:
        meta = R.read_meta(store, release_id)
    except R.ReleaseError:
        meta = {}
    changes = {k: [x for x in _list(_dict(meta.get("changes")).get(k)) if isinstance(x, str)] for k in ("added", "modified", "removed")}
    parent = meta.get("parent") if R.is_release_id(meta.get("parent")) else None
    return Outcome("published", release_id=release_id, previous_release_id=parent, changes=changes, proposal=_dict(meta.get("proposal")) or info,
                   sequence=sequence, review_doc=_dict(meta.get("review")) or None, operations=[o for o in _list(meta.get("operations")) if isinstance(o, dict)])


def _made_from(manifest: dict, proposal_id: str, sha: str, request_id: str | None = None) -> str | None:
    """The release (live now, or in the pointer's history) that was made from exactly this proposal - with
    ``request_id``: by exactly this request."""
    entries = [dict(_dict(manifest.get("source")), releaseId=manifest.get("releaseId"))]
    entries += [h for h in _list(manifest.get("history")) if isinstance(h, dict) and h.get("kind") == "agent"]
    for entry in entries:
        if entry.get("proposalId") != proposal_id or entry.get("proposalSha256", sha) != sha:
            continue  # (history written before the hash was kept there is matched by id)
        if request_id is None or entry.get("request") == request_id:
            return entry["releaseId"] if R.is_release_id(entry.get("releaseId")) else None
    return None


def publish_proposal(store: Store, record: Any, *, policy: Policy | None = None, control: Store | None = None,
                     actor: str = "pipeline", approved_by: str | None = None, now: dt.datetime | None = None,
                     request_id: str | None = None) -> Outcome:
    """Replay ``record`` (a run record or a proposal) on the live release and publish the result.

    ``approved_by`` skips the *holds* (a person has looked) but never the validation.  ``control`` is where proposals
    that wait for approval are kept; without it a held proposal is refused.  ``request_id`` names the request this call
    belongs to (one execution of the state machine): when the same request arrives again after it already published,
    the answer is the one it got the first time.
    """
    policy = policy or Policy()
    proposal, run = split_record(record)
    try:
        info = brief(proposal, run)
        refused = refusals(proposal, run, policy, now)
        sha = "" if refused or proposal is None else proposal_sha256(proposal)
    except Exception as e:  # a record no check anticipated (nested a thousand levels deep, say) is refused like any other
        info = {"id": _flat(proposal.get("id"), 100)} if isinstance(proposal, dict) else {}
        refused, sha = [f"提案無法處理（{type(e).__name__}：{_flat(e, 300)}）"], ""
    if refused or proposal is None:
        return _refuse(control, info, refused or ["找不到 proposal"], now)
    info["sha256"] = sha

    for attempt in range(ATTEMPTS):
        current = R.load_current(store)
        sequence = current.manifest.get("sequence")
        if request_id:
            mine = _made_from(current.manifest, proposal["id"], sha, request_id)
            if mine:  # this very request already went through; its first answer was lost on the way
                return _published_before(store, mine, info, sequence if mine == current.release_id else None)
        live = _dict(current.manifest.get("source"))
        if live.get("proposalId") == proposal["id"] and live.get("proposalSha256") == sha:
            # the release that is live was made from exactly this proposal: a repeated request, not a new one
            return Outcome("already_published", release_id=current.release_id, proposal=info, sequence=sequence)
        portal = portal_view(current.files, current.release_id)
        try:
            # decided by what the live data holds, not by the history: after a rollback the proposal is in the history but not live
            if already_applied(portal, proposal):
                return Outcome("already_published", release_id=current.release_id, proposal=info, sequence=sequence)
            effects: list[list[Change]] = []
            staged, problems = apply_saved_proposal(portal, proposal, effects)
            if problems:
                return _refuse(control, info, [f"提案無法套用到目前上線的資料（release {current.release_id}）"] + [_flat(p, 500) for p in problems], now)
            changed = changed_files(portal, staged)
            outside = [rel for rel in changed if not is_data_path(rel)]
            if outside:
                return _refuse(control, info, [f"提案會改到研究資料以外的檔案：{[_flat(r, 120) for r in outside[:5]]}"], now)
            if not changed:
                return Outcome("unchanged", release_id=current.release_id, proposal=info, sequence=sequence)
            review = build_review(portal, staged, proposal, current.release_id)
            held = [] if approved_by else holds(proposal, policy, review, coarse_operations(proposal, effects, policy.max_op_changes),
                                                live=portal, staged=staged)
            stated = operations(proposal)
            files = dict(current.files)
            for rel in changed:
                files[rel] = dump_json(staged.docs[rel]).encode("utf-8")
            heavy = [f"{_flat(rel, 120)}（{len(files[rel]):,} bytes）" for rel in changed if len(files[rel]) > R.MAX_FILE_BYTES]
            if heavy:
                return _refuse(control, info, [f"提案會讓資料檔超過大小上限（{R.MAX_FILE_BYTES:,} bytes）：" + _listed(heavy)], now)
        except Exception as e:  # whatever shape got past the checks: a refusal that says why, never a crashed run
            return _refuse(control, info, [f"提案無法處理（{type(e).__name__}：{_flat(e, 300)}）"], now)

        paused = None if approved_by else R.hold_of(current.manifest)
        if paused:
            held.insert(0, f"自動發布已暫停（{paused['by']} 於 {paused['at']}：{paused['reason'] or '未註明原因'}）")
        if held:
            waiting = Outcome("pending", release_id=current.release_id, reasons=held, proposal=info, review=review, sequence=sequence,
                              operations=stated, changes={"added": [f for f in changed if f not in portal.docs], "modified": [f for f in changed if f in portal.docs], "removed": []})
            if control is None:
                return Outcome("rejected", reasons=["這份提案需要人工核准，但沒有設定存放待核准提案的位置"] + held, proposal=info, review=review)
            try:
                clash = _save_pending(control, proposal["id"], sha, record, held, current.release_id, info, changed, review, now)
            except Exception as e:  # it can neither go live nor wait: say so, instead of leaving a run that just failed
                return _refuse(control, info, [f"提案需要人工核准，但無法存進待核准清單（{type(e).__name__}：{_flat(e, 300)}）"] + held, now)
            return waiting if clash is None else _refuse(control, info, [clash], now)

        stamp = now or R.utc_now()
        release_id = R.new_release_id(stamp, files, salt=f"{current.release_id}:{attempt}")
        changes = R.diff(current.files, files)
        source: dict[str, Any] = {"kind": "agent", "by": actor, "proposalId": proposal["id"], "proposalSha256": sha}
        for key, value in (("runId", info.get("runId")), ("digestId", info.get("digestId")), ("approvedBy", approved_by), ("request", request_id)):
            if value:
                source[key] = value
        meta = R.release_meta(release_id, files, created_at=R.iso(stamp), parent=current.release_id, source=source, changes=changes,
                              details={"proposal": info, "review": review.to_doc(limit=AUDIT_CHANGES, width=AUDIT_VALUE),
                                       "operations": stated, "unverified": _list(proposal.get("unverified"))[:50]})
        try:
            pinned = R.write_release(store, release_id, files, meta)
        except (R.ReleaseError, ValueError) as e:  # a file the release format does not take, a value JSON cannot hold
            return _refuse(control, info, [f"無法寫出新的 release（{_flat(e, 300)}）"], now)
        entry = R.history_entry(release_id, published_at=R.iso(stamp), source=source, summary=info.get("summary", ""), changes=changes)
        manifest = R.build_manifest(release_id, files, published_at=R.iso(stamp), source=source, previous=current.manifest, entry=entry,
                                    release_sha256=pinned)
        try:
            R.switch_pointer(store, manifest, current.etag)
        except Conflict:
            continue  # somebody else published (or paused) in the meantime: start again from what is live now
        outcome = Outcome("published", release_id=release_id, previous_release_id=current.release_id, changes=changes, proposal=info,
                          sequence=manifest["sequence"], review=review, operations=stated)
        if control is not None:  # the release is live: nothing below may turn that into a failure
            note = _clear_pending(control, proposal["id"], sha)
            if note:
                outcome.warnings.append(note)
        return outcome
    return _refuse(control, info, [f"發布失敗：上線中的資料在 {ATTEMPTS} 次嘗試之間一直被其他發布更新；稍後再試一次"], now)


def _clear_pending(control: Store, proposal_id: str, sha: str) -> str | None:
    """Take the proposal that just went live off the waiting list - that proposal, not another one that waits under the
    same id (ids come from run records; what a person was asked to decide stays until they decide it).  Returns a note
    for the notification when something is left behind."""
    key = PENDING + proposal_id + ".json"
    try:
        try:
            waiting = json.loads(control.get(key).data)
        except NotFound:
            return None
        except ValueError:
            waiting = None
        if isinstance(waiting, dict) and _pending_sha(waiting) != sha:
            return (f"待核准清單裡另有一份 id 同為 {proposal_id}、內容不同的提案，它還在等待核准（portalctl show {proposal_id}）；"
                    "剛發布的不是那一份")
        control.delete(key)
    except Exception as e:
        return f"已發布，但清除待核准紀錄 {proposal_id} 時失敗（{type(e).__name__}）；可以用 discard 移除"
    return None


def _refuse(control: Store | None, info: dict, reasons: list[str], now: dt.datetime | None) -> Outcome:
    outcome = Outcome("rejected", reasons=reasons, proposal=info)
    if control is not None and _PROPOSAL_ID.fullmatch(str(info.get("id", ""))):
        try:
            control.put(REJECTED + info["id"] + ".json", R.dump({"proposal": info, "reasons": [_flat(r, 2000) for r in reasons],
                                                                "at": R.iso(now or R.utc_now())}), content_type="application/json")
        except Exception as e:  # the refusal stands whether or not it could be filed
            outcome.warnings.append(f"拒絕的紀錄沒有寫入 {REJECTED}（{type(e).__name__}）")
    return outcome


# ---------------------------------------------------------------------------------------------- pausing automatic publishing
def get_hold(store: Store) -> dict | None:
    """{"by", "at", "reason"} while automatic publishing is paused, else None (also before the first release)."""
    try:
        manifest, _ = R.read_pointer(store)
    except R.NotBootstrapped:
        return None
    return R.hold_of(manifest)


def _set_flag(manifest: dict, flag: dict) -> dict:
    changed = dict(manifest)
    changed[R.AUTO_PUBLISH] = flag
    return changed


def pause(store: Store, *, actor: str, reason: str = "", now: dt.datetime | None = None) -> Outcome:
    """Until ``resume``, no proposal goes live without approval (research keeps running and proposals queue up).

    The flag is part of the pointer, so "is it paused?" and "make this release live" are answered by the same
    conditional write: a proposal cannot slip through between a pause and the switch.
    """
    flag = {"paused": True, "by": _flat(actor, 80), "at": R.iso(now or R.utc_now()), "reason": _flat(reason, 300)}
    for _ in range(ATTEMPTS):
        manifest, etag = R.read_pointer(store)
        try:
            R.switch_pointer(store, _set_flag(manifest, flag), etag)
        except Conflict:
            continue
        return Outcome("paused", kind="control", release_id=manifest["releaseId"], reasons=[flag["reason"]] if flag["reason"] else [],
                       sequence=manifest.get("sequence"))
    raise R.ReleaseError(f"無法暫停自動發布：manifest.json 在 {ATTEMPTS} 次嘗試之間一直被更新，請再試一次")


def resume(store: Store, *, actor: str, now: dt.datetime | None = None, request_id: str | None = None) -> Outcome:
    """``request_id`` identifies one request across its retries: the second delivery of a resume that already happened
    is answered like the first, not with "nothing was paused"."""
    seen: dict | None = None
    for _ in range(ATTEMPTS):
        manifest, etag = R.read_pointer(store)
        was = R.hold_of(manifest)
        common = dict(kind="control", release_id=manifest["releaseId"], sequence=manifest.get("sequence"))
        if was is None:
            if request_id and _dict(manifest.get(R.AUTO_PUBLISH)).get("request") == request_id:
                return Outcome("resumed", **common)
            return Outcome("unchanged", reasons=["自動發布原本就沒有暫停"], **common)
        if seen is not None and was != seen:
            # between reading and writing, somebody paused again (a rollback, most likely): that one is not ours to lift
            return Outcome("paused", reasons=[f"恢復的同時又被暫停了（{was['by']} 於 {was['at']}：{was['reason'] or '未註明原因'}）；確認後再執行一次 resume"], **common)
        seen = was
        flag = {"paused": False, "by": _flat(actor, 80), "at": R.iso(now or R.utc_now()), **({"request": request_id} if request_id else {})}
        try:
            R.switch_pointer(store, _set_flag(manifest, flag), etag)
        except Conflict:
            continue
        return Outcome("resumed", reasons=[f"原本由 {was['by']} 於 {was['at']} 暫停：{was['reason'] or '未註明原因'}"], **common)
    raise R.ReleaseError(f"無法恢復自動發布：manifest.json 在 {ATTEMPTS} 次嘗試之間一直被更新，請再試一次")


# ---------------------------------------------------------------------------------------------- approval
def _save_pending(control: Store, proposal_id: str, sha: str, record: Any, reasons: list[str], base: str, info: dict,
                  changed: list[str], review: Review, now: dt.datetime | None) -> str | None:
    """Keep the proposal for a person to decide.  Returns why it could not be kept (None when it is waiting).

    A waiting proposal is never replaced: what a person was shown is what their approval publishes.
    """
    key = PENDING + proposal_id + ".json"
    doc = {"schema": PENDING_SCHEMA, "proposalId": proposal_id, "proposalSha256": sha, "heldAt": R.iso(now or R.utc_now()),
           "reasons": reasons, "baseReleaseId": base, "proposal": info, "changedFiles": changed,
           "review": review.to_doc(limit=AUDIT_CHANGES, width=AUDIT_VALUE), "record": record}
    try:
        control.put(key, R.dump(doc), content_type="application/json", if_none_match=True)
    except Conflict:
        try:
            existing = json.loads(control.get(key).data)
        except (NotFound, ValueError):
            existing = None
        if not (isinstance(existing, dict) and _pending_sha(existing) == sha):
            return (f"已經有一份 id 同為 {proposal_id}、內容不同的提案在等待核准；先決定那一份（portalctl show / approve / discard），"
                    "再重新發布這一份")
        # the same proposal is already waiting (this request was a repeat)
    # A proposal that somebody discarded and that is submitted again is a new question: the old answer must not be
    # what every later approval is told.  (Only a decision about this very content is cleared.)
    decision, etag = _decision(control, proposal_id)
    if decision and etag and decision.get("decision") == "discard" and decision.get("proposalSha256") == sha:
        try:
            control.delete(DECIDED + proposal_id + ".json")
        except Exception:  # the approval will say "discarded by …": an inconvenience, not a wrong publish
            pass
    return None


def _pending_sha(doc: dict) -> str:
    sha = doc.get("proposalSha256")
    if isinstance(sha, str) and _SHA256.fullmatch(sha):
        return sha
    return proposal_sha256(split_record(doc.get("record"))[0])


def list_pending(control: Store) -> list[dict]:
    out = []
    for key in control.list(PENDING):
        try:
            doc = json.loads(control.get(key).data)
        except (NotFound, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        entry = {k: doc.get(k) for k in ("proposalId", "proposalSha256", "heldAt", "reasons", "baseReleaseId", "proposal", "changedFiles")}
        decision = _decision(control, str(doc.get("proposalId")))[0]
        if decision and decision.get("state") == "refused":
            entry["lastRefusal"] = {k: decision.get(k) for k in ("by", "at", "reasons")}
        out.append(entry)
    return sorted(out, key=lambda d: str(d.get("heldAt")))


def load_pending(control: Store, proposal_id: str) -> dict:
    if not isinstance(proposal_id, str) or not _PROPOSAL_ID.fullmatch(proposal_id):
        raise R.ReleaseError(f"not a proposal id: {_flat(proposal_id, 80)!r}")
    try:
        doc = json.loads(control.get(PENDING + proposal_id + ".json").data)
    except NotFound as e:
        raise R.ReleaseError(f"沒有待核准的提案 {proposal_id}（可能已經核准、捨棄，或從未暫存）") from e
    except ValueError as e:
        raise R.ReleaseError(f"{control.uri(PENDING + proposal_id + '.json')} 不是有效的 JSON") from e
    if not isinstance(doc, dict) or doc.get("schema") != PENDING_SCHEMA or "record" not in doc:
        raise R.ReleaseError(f"{control.uri(PENDING + proposal_id + '.json')} 不是待核准提案")
    return doc


def _decision(control: Store, proposal_id: str) -> tuple[dict | None, str | None]:
    if not _PROPOSAL_ID.fullmatch(proposal_id):
        return None, None
    try:
        blob = control.get(DECIDED + proposal_id + ".json")
        doc = json.loads(blob.data)
    except (NotFound, ValueError):
        return None, None
    return (doc if isinstance(doc, dict) else {}), blob.etag


def _claim(control: Store, proposal_id: str, decision: str, state: str, actor: str, sha: str, now: dt.datetime, not_live: bool = False,
           **extra: Any) -> str | None:
    """Record that ``actor`` is deciding this proposal.  Returns why they cannot (None when the decision is theirs).

    The record is created only if there is none, so of an approval and a discard that arrive together exactly one wins.
    An approval may be repeated (a retry, a second approver): publishing itself happens once.

    ``not_live``: the caller has looked at the data and this content is not in it.  An approval on record is then
    history - the release it made was rolled back and the proposal is waiting again - and does not stand in the way of
    discarding it: "is it published?" is answered by what the live data holds, never by a note that says so.
    """
    key = DECIDED + proposal_id + ".json"
    doc = {"proposalId": proposal_id, "proposalSha256": sha, "decision": decision, "state": state, "by": _flat(actor, 80), "at": R.iso(now), **extra}
    for _ in range(ATTEMPTS):
        try:
            control.put(key, R.dump(doc), content_type="application/json", if_none_match=True)
            return None
        except Conflict:
            pass
        existing, etag = _decision(control, proposal_id)
        if existing is None or etag is None:
            continue  # it vanished again: take it
        was, was_state = existing.get("decision"), existing.get("state")
        who = f"{_flat(existing.get('by'), 80)} 於 {_flat(existing.get('at'), 40)}"
        if existing.get("proposalSha256") != sha:
            pass  # a decision about an earlier proposal that had the same id: it does not bind this one
        elif was == "discard":
            return None if decision == "discard" else f"這份提案已經由 {who} 捨棄"
        elif was == "approve" and was_state == "done":
            if decision == "approve":
                return None
            if not not_live:
                return f"這份提案已經由 {who} 核准並發布"
        elif was == "approve" and was_state == "started" and decision == "discard":
            started = _instant(existing.get("at"))
            if started is None or now - started < STALE_CLAIM:
                return f"這份提案正在核准中（{who}）；等它結束再決定"
        try:
            control.put(key, R.dump(doc), content_type="application/json", if_match=etag)
            return None
        except Conflict:
            continue
    return "同時有其他人在決定這份提案；請稍後再試一次"


def _finish(control: Store, proposal_id: str, decision: str, state: str, actor: str, sha: str, now: dt.datetime, **extra: Any) -> None:
    control.put(DECIDED + proposal_id + ".json", R.dump({"proposalId": proposal_id, "proposalSha256": sha, "decision": decision, "state": state,
                                                         "by": _flat(actor, 80), "at": R.iso(now), **extra}), content_type="application/json")


def approve(store: Store, control: Store, proposal_id: str, *, actor: str, policy: Policy | None = None,
            now: dt.datetime | None = None, expected_sha: str | None = None, request_id: str | None = None) -> Outcome:
    """Publish a pending proposal.  It is validated again against what is live now: approval is not a bypass.

    ``expected_sha`` (from the notification or from ``portalctl show``) ties the approval to the content that was
    reviewed.  A proposal that is refused stays pending, with the refusal on record: discard it, or approve it again
    once the reason is gone.  ``request_id``: see ``publish_proposal`` (and ``answered`` for a repeat that arrives
    after the proposal left the waiting list).
    """
    doc = load_pending(control, proposal_id)
    info = _dict(doc.get("proposal"))
    sha = _pending_sha(doc)
    if expected_sha and expected_sha != sha:
        return Outcome("rejected", proposal=info, reasons=[
            f"待核准的 {proposal_id} 不是這個核准指令所指的內容（sha256 不符）：重新檢視（portalctl show {proposal_id}）後再核准"])
    stamp = now or R.utc_now()
    who = _flat(actor, 80) or "unknown"
    clash = _claim(control, proposal_id, "approve", "started", who, sha, stamp)
    if clash:
        return Outcome("rejected", proposal=info, reasons=[clash])
    outcome = publish_proposal(store, doc["record"], policy=policy, control=None, actor="pipeline", approved_by=who, now=now, request_id=request_id)
    extra = {"request": request_id} if request_id else {}
    try:
        if outcome.status in ("published", "already_published", "unchanged"):
            _finish(control, proposal_id, "approve", "done", who, sha, stamp, releaseId=outcome.release_id, result=outcome.status, **extra)
            control.delete(PENDING + proposal_id + ".json")
        else:
            _finish(control, proposal_id, "approve", "refused", who, sha, stamp, reasons=[_flat(r, 600) for r in outcome.reasons[:20]])
            outcome.reasons.append(f"提案仍在待核准清單裡；不再需要的話請捨棄（portalctl discard {proposal_id}）")
    except Exception as e:  # the publish (or the refusal) already happened: say so instead of failing the run
        outcome.warnings.append(f"核准的結果已定，但更新待核准紀錄時失敗（{type(e).__name__}）；`portalctl pending` 可能還會列出 {proposal_id}")
    return outcome


def discard(control: Store, proposal_id: str, *, actor: str, reason: str = "", now: dt.datetime | None = None,
            store: Store | None = None, request_id: str | None = None) -> Outcome:
    """Drop a pending proposal.  ``store`` (the data location): a proposal that is already published - an approval ran
    as far as the release and died before it could tidy up - is not "discarded": it is live, and the record says so."""
    doc = load_pending(control, proposal_id)
    info = _dict(doc.get("proposal"))
    sha = _pending_sha(doc)
    why = _flat(reason, 300)
    stamp = now or R.utc_now()
    not_live = False  # known, from the data itself, not to be in what is live
    if store is not None:
        release = None
        try:
            current = R.load_current(store)
            manifest = current.manifest
            release = _made_from(manifest, proposal_id, sha)
            proposal = split_record(doc.get("record"))[0]
            applied = bool(proposal and already_applied(portal_view(current.files, current.release_id), proposal))
            # in the history AND still in the data: after a rollback it is neither live nor "published", and may go
            if release and release != current.release_id and not applied:
                release = None
            not_live = not release and not applied
        except Exception:  # the data location cannot be read from here: decide on the waiting list alone
            manifest = {}
        if release:
            approver = next((h.get("by") for h in [_dict(manifest.get("source"))] + _list(manifest.get("history"))
                             if isinstance(h, dict) and h.get("proposalId") == proposal_id), None)
            outcome = Outcome("rejected", release_id=release, proposal=info, reasons=[
                f"這份提案已經發布了（release {release}），不能再捨棄；要撤回它的內容請用 rollback。它已從待核准清單移除"])
            try:
                _finish(control, proposal_id, "approve", "done", _flat(approver or "pipeline", 80), sha, stamp, releaseId=release, result="published")
                control.delete(PENDING + proposal_id + ".json")
            except Exception as e:
                outcome.warnings.append(f"更新待核准紀錄時失敗（{type(e).__name__}）；`portalctl pending` 可能還會列出 {proposal_id}")
            return outcome
    clash = _claim(control, proposal_id, "discard", "done", actor, sha, stamp, not_live, reason=why, **({"request": request_id} if request_id else {}))
    if clash:
        return Outcome("rejected", proposal=info, reasons=[clash])
    control.delete(PENDING + proposal_id + ".json")
    return Outcome("discarded", reasons=[why] if why else [], proposal=info)


def answered(store: Store, control: Store, proposal_id: str, action: str, request_id: str | None = None) -> Outcome | None:
    """For an approve / discard of a proposal that is not waiting (any more): the answer that is on record.

    The same request delivered twice gets its first answer again (the state machine retries a step whose reply was
    lost - by then the approval is live and the proposal has left the waiting list).  Somebody else's later request
    is told what was decided.  None when nothing was ever decided about this id.
    """
    if not isinstance(proposal_id, str) or not _PROPOSAL_ID.fullmatch(proposal_id):
        return None
    decision, _etag = _decision(control, proposal_id)
    if not decision:
        return None
    who = f"{_flat(decision.get('by'), 80)} 於 {_flat(decision.get('at'), 40)}"
    info = {"id": proposal_id, **({"sha256": decision["proposalSha256"]} if isinstance(decision.get("proposalSha256"), str) else {})}
    same = bool(request_id) and decision.get("request") == request_id
    if decision.get("decision") == "approve" and decision.get("state") == "done":
        release = decision.get("releaseId") if R.is_release_id(decision.get("releaseId")) else None
        if action == "approve" and same and release and decision.get("result") == "published":
            return _published_before(store, release, info)
        if release:  # what the proposal was is on record with the release it became
            try:
                kept = _dict(R.read_meta(store, release).get("proposal"))
                info = kept if kept.get("id") == proposal_id else info
            except R.ReleaseError:
                pass
        return Outcome("already_published" if action == "approve" else "rejected", release_id=release, proposal=info,
                       reasons=[f"這份提案已經由 {who} 核准並發布" + (f"（release {release}）" if release else "")])
    if decision.get("decision") == "discard":
        if action == "discard":
            return Outcome("discarded", proposal=info, reasons=[] if same else [f"這份提案已經由 {who} 捨棄"])
        return Outcome("rejected", proposal=info, reasons=[f"這份提案已經由 {who} 捨棄；要重新提出，請再發布一次那次研究的結果（或重跑研究）"])
    return None


# ---------------------------------------------------------------------------------------------- manual releases
def tool_file_problems(schema: dict, files: dict[str, bytes]) -> list[Problem]:
    """Problems in the test results and the platform inventory (types the schema does not define are skipped)."""
    problems: list[Problem] = []
    defined = schema.get("definitions") if isinstance(schema, dict) else None
    for rel in sorted(files):
        definition = next((d for pattern, d in TOOL_FILE_TYPES if fnmatch.fnmatch(rel, pattern)), None)
        if definition is None or not isinstance(defined, dict) or definition not in defined:
            continue
        try:
            doc = json.loads(files[rel])
        except ValueError as e:
            problems.append(Problem(rel, "", f"不是有效的 JSON（{e}）"))
            continue
        problems.extend(validate_definition(schema, definition, doc, rel))
        problems.extend(Problem(rel, where, "is not a finite number (NaN / Infinity cannot be stored as JSON)") for where in non_finite(doc))
    return problems


def data_problems(files: dict[str, bytes], release_id: str) -> list[Problem]:
    """Everything the validators can say about a set of files: research data (schema + cross-file) and tool files."""
    view = portal_view(files, release_id)
    return validate_docs(view.schema, view.docs) + tool_file_problems(view.schema, files)


def _under(rel: str, prefixes: Iterable[str]) -> bool:
    return any(rel == p.rstrip("/") or rel.startswith(p) for p in prefixes)


def merge_checkout(current: dict[str, bytes], tree: dict[str, bytes], *, base: dict[str, bytes] | None = None,
                   only: Iterable[str] | None = None, delete: bool = False, keep: Iterable[str] = ()) -> tuple[dict[str, bytes], list[str]]:
    """What a push from a checkout would make live, and the files it cannot decide (conflicts).

    ``base`` is the release the checkout was pulled from.  With it, each file is merged three ways: a file the checkout
    did not change keeps whatever is live now, a file only the checkout changed (or deleted) is taken from it, and a
    file both changed is a conflict.  Nothing somebody else published in the meantime is undone by accident.

    Without ``base`` nobody can tell who changed what: the checkout's files are added and replace what is live, nothing
    is removed unless ``delete`` asks for it, and a research file that differs from the live one is a conflict (it may
    be the agent's newer update).  ``only`` limits all of this to some folders.

    ``keep``: paths (a folder ends with "/") that the checkout has but that were not read from it - a file it does not
    publish without being asked, a symbolic link.  "Not read" is not "deleted": what is live there stays.
    """
    prefixes = None if only is None else [p.strip("/") + "/" for p in only if p.strip("/")]
    kept = [k.rstrip("/") for k in keep if k and k.rstrip("/")]
    files = dict(current)
    conflicts: list[str] = []
    for rel in sorted(set(tree) | set(current)):
        if prefixes is not None and not _under(rel, prefixes):
            continue
        if rel not in tree and any(rel == k or rel.startswith(k + "/") for k in kept):
            continue
        mine, theirs = tree.get(rel), current.get(rel)
        if mine == theirs:
            continue
        if base is None:
            if mine is None:
                if delete:
                    del files[rel]
                continue
            if theirs is not None and is_data_path(rel):
                conflicts.append(rel)
            files[rel] = mine
            continue
        was = base.get(rel)
        if mine == was:
            continue  # untouched in the checkout: what is live stays
        if theirs != was:
            conflicts.append(rel)  # changed on both sides (the checkout's version is used only when forced)
        if mine is None:
            files.pop(rel, None)
        else:
            files[rel] = mine
    return files, conflicts


#: what every page of the UI starts from: a release without one of these shows an error instead of the portal
CORE_FILES = (SCHEMA_PATH, SOURCES, CATALOG, "data/lifecycle.json", "data/litellm.json")


def publish_tree(store: Store, tree: dict[str, bytes], *, only: Iterable[str] | None = None, base_release: str | None = None,
                 bootstrap: bool = False, force: bool = False, allow_invalid: bool = False, delete: bool = False,
                 actor: str = "manual", note: str = "", now: dt.datetime | None = None, dry_run: bool = False,
                 untouched: Iterable[str] = ()) -> Outcome:
    """Publish files from a portal checkout (``dry_run``: run every check and report what would change, write nothing).

    The checkout is merged with what is live (``merge_checkout``): ``base_release`` is the release it was pulled from.
    ``force`` takes the checkout's version where both sides changed a file; ``allow_invalid`` publishes although the
    validators object.  They are separate switches on purpose: overriding one protection must not drop the other.
    """
    kind = "bootstrap" if bootstrap else "manual"
    try:
        current: R.Current | None = R.load_current(store)
    except R.NotBootstrapped:
        if not bootstrap:
            raise
        current = None
    if current is not None and bootstrap and not force:
        return Outcome("rejected", kind=kind, release_id=current.release_id,
                       reasons=[f"資料位置已經有上線的 release（{current.release_id}）；要從 checkout 更新請用 push"])
    live_id = current.release_id if current else None
    notes: list[str] = []
    conflicts: list[str] = []
    base_lost = False
    if current is None or bootstrap:
        files = dict(tree)  # the checkout as a whole
    else:
        base: dict[str, bytes] | None = None
        if base_release == current.release_id:
            base = current.files
        elif base_release:
            try:
                base = R.read_release(store, R.check_release_id(base_release), verify=False)[1]
            except R.ReleaseError:
                base_lost = True
                notes.append(f"checkout 記錄的來源 release {_flat(base_release, 40)} 已經讀不到：無法分辨哪些檔案是妳改的")
        files, conflicts = merge_checkout(current.files, tree, base=base, only=only, delete=delete, keep=untouched)
    changes = R.diff(current.files if current else {}, files)
    if current is not None and not R.changed_count(changes):
        return Outcome("unchanged", kind=kind, release_id=live_id, sequence=current.manifest.get("sequence"), warnings=notes)

    reasons: list[str] = []
    missing = [rel for rel in CORE_FILES if rel not in files]
    if missing:  # no switch publishes this: it is not "invalid data", it is no portal
        return Outcome("rejected", kind=kind, release_id=live_id, changes=changes, warnings=notes, reasons=[
            "這次發布之後會缺少 UI 必要的檔案：" + "、".join(missing) + "。checkout 裡的這些檔案不存在，或不是一般檔案（例如 symlink）；"
            "補上之後再發布"])
    try:
        problems_after = data_problems(files, "candidate")
    except R.ReleaseError as e:
        return Outcome("rejected", kind=kind, reasons=[str(e)], changes=changes)
    # only what this publish would add is refused: a problem that is already live must not freeze every later update
    fresh = new_problems(data_problems(current.files, current.release_id), problems_after) if current is not None else problems_after
    stopping = blocking(fresh)
    if stopping and not allow_invalid:
        reasons += [f"資料驗證：{line}" for line in shorten(stopping, 20)]
    # (a warning does not stop a person's own publish - the data is usable; they are told what will not work as written)
    notes += [f"資料提醒（不影響發布）：{line}" for line in shorten(advisory(fresh), 8)]
    if conflicts and not force:
        listed = "、".join(conflicts[:6]) + ("…" if len(conflicts) > 6 else "")
        if base_release and not base_lost:
            reasons.append(f"這 {len(conflicts)} 個檔案在 checkout 和上線的資料裡都改過（checkout 取自 release {base_release}，上線中的是 {live_id}）：{listed}。"
                           "先 pull（不會覆蓋妳改過的檔案）再處理，確定要以 checkout 為準才加 --force")
        else:
            origin = f"記錄的來源 release {_flat(base_release, 40)} 已經讀不到" if base_release else "沒有記錄是從哪個 release 取得的"
            reasons.append(f"這次會改到研究資料（{listed}），但 checkout {origin}，而上線中的是 {live_id}："
                           "無法分辨是妳改的，還是上線的資料比較新。先 pull 再修改，或只發布特定資料夾（--only），確定要覆蓋再用 --force")
    if reasons:
        return Outcome("rejected", kind=kind, release_id=live_id, reasons=reasons, changes=changes, warnings=notes)
    if dry_run:
        return Outcome("preview", kind=kind, release_id=live_id, changes=changes, warnings=notes)

    stamp = now or R.utc_now()
    release_id = R.new_release_id(stamp, files, salt=str(live_id))
    source: dict[str, Any] = {"kind": kind, "by": _flat(actor, 80)}
    if note:
        source["note"] = _flat(note, 300)
    meta = R.release_meta(release_id, files, created_at=R.iso(stamp), parent=live_id, source=source, changes=changes)
    pinned = R.write_release(store, release_id, files, meta)
    summary = note or ("第一個 release（由 portal checkout 建立）" if current is None else f"手動發布：{R.changed_count(changes)} 個檔案變更")
    entry = R.history_entry(release_id, published_at=R.iso(stamp), source=source, summary=summary, changes=changes)
    manifest = R.build_manifest(release_id, files, published_at=R.iso(stamp), source=source,
                                previous=current.manifest if current else None, entry=entry, release_sha256=pinned)
    try:
        R.switch_pointer(store, manifest, current.etag if current else None)
    except Conflict:
        return Outcome("rejected", kind=kind, reasons=["發布的同時有另一個發布完成了；請重新 pull 後再試一次"], changes=changes)
    return Outcome("published", kind=kind, release_id=release_id, previous_release_id=live_id, changes=changes, sequence=manifest["sequence"],
                   warnings=notes)


# ---------------------------------------------------------------------------------------------- rollback
def rollback(store: Store, to: str | None = None, *, actor: str, reason: str = "", now: dt.datetime | None = None,
             if_current: str | None = None, request_id: str | None = None, allow_unlisted: bool = False) -> Outcome:
    """Make an earlier release live again (default: the one that was live before the current one).

    The same write pauses automatic publishing: the next research run reads the restored data and would otherwise
    propose - and publish - the very change that was just rolled back.

    ``if_current`` (what a notification's rollback command carries) makes the request refer to one particular release:
    it does nothing once something else is live.  ``request_id`` identifies one request across its retries, so that a
    second delivery of a rollback that already happened reports that rollback instead of a refusal.

    The target has to be a release that was live: one the pointer's history lists.  A complete release can exist that
    never was (a publish wrote it and lost the race for the pointer - its proposal may still be waiting for approval);
    making that one live would publish something nobody decided to publish.  ``allow_unlisted`` is for the operator who
    knows the release is older than the history reaches.
    """
    manifest, etag = R.read_pointer(store)
    current_id = manifest["releaseId"]
    common = dict(kind="rollback", release_id=current_id)
    done = _dict(manifest.get("source"))
    if request_id and done.get("kind") == "rollback" and done.get("request") == request_id:
        return Outcome("rolled_back", kind="rollback", release_id=current_id, previous_release_id=done.get("from"), sequence=manifest.get("sequence"))
    if if_current and if_current != current_id:
        return Outcome("rejected", reasons=[f"上線中的已經不是 {_flat(if_current, 40)}（現在是 {current_id}）；這個 rollback 指令是針對那一版的。"
                                             "要回到特定版本請指定 release id"], **common)
    source = _dict(manifest.get("source"))
    if not to and source.get("kind") == "rollback":
        return Outcome("rejected", reasons=[f"上線中的版本本身就是一次 rollback 的結果（從 {_flat(source.get('from'), 40)} 回復到 {current_id}）；"
                                             "再回復一次只會回到被撤下的那一版。要換到別的版本請指定 release id（portalctl history）"], **common)
    target = to or manifest.get("previousReleaseId")
    if not target:
        return Outcome("rejected", reasons=["沒有更早的 release 可以回復"], **common)
    target = R.check_release_id(target)
    if target == current_id:
        return Outcome("unchanged", sequence=manifest.get("sequence"), **common)
    if not allow_unlisted and not R.was_live(manifest, target):
        return Outcome("rejected", reasons=[
            f"release {target} 不在發布紀錄裡：它可能從未上線過（發布時被另一個發布搶先，留下一份完整但沒有啟用的資料），"
            "也可能比紀錄保留的範圍更早。`portalctl history` 列出可以回復的版本；確定要回到這一版，用 portalctl rollback --to "
            f"{target} --unlisted"], **common)
    try:
        _meta, files = R.read_release(store, target)  # verifies every file: never point the UI at a damaged release
    except R.ReleaseError as e:
        return Outcome("rejected", reasons=[str(e)], **common)
    try:
        _cur_meta, current_files = R.read_release(store, current_id, verify=False)
        changes = R.diff(current_files, files)
    except R.ReleaseError:  # the live release is damaged - exactly when a rollback must still work
        changes = {"added": [], "modified": [], "removed": []}
    stamp = now or R.utc_now()
    why = _flat(reason, 300)
    source = {"kind": "rollback", "by": _flat(actor, 80), "from": current_id}
    if why:
        source["note"] = why
    if request_id:
        source["request"] = request_id
    entry = R.history_entry(target, published_at=R.iso(stamp), source=source, summary=why or f"回復到 {target}（原為 {current_id}）", changes=changes)
    flag = {"paused": True, "by": source["by"], "at": R.iso(stamp), "reason": f"rollback 到 {target}" + (f"：{why}" if why else "")}
    new_manifest = R.build_manifest(target, files, published_at=R.iso(stamp), source=source, previous=manifest, entry=entry, auto_publish=flag,
                                    release_sha256=R.release_sha(store, target))
    try:
        R.switch_pointer(store, new_manifest, etag)
    except Conflict:
        return Outcome("rejected", reasons=["回復的同時有另一個發布完成了；請確認目前狀態後再試"], **common)
    return Outcome("rolled_back", kind="rollback", release_id=target, previous_release_id=current_id, changes=changes,
                   sequence=new_manifest["sequence"])
