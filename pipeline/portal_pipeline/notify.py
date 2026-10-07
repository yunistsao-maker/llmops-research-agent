"""The messages a person receives about a pipeline run (Amazon SNS: e-mail, chat integrations ...).

Subjects are ASCII and shorter than 100 characters because SNS rejects anything else; the body is zh-TW.

A message mixes two kinds of text: lines this module writes (among them the commands a person will paste to approve or
roll back) and text that came from a web page, a model or a file somebody edited.  The second kind never reaches a
message as it is: every such piece goes through ``clean`` - one line, no control or invisible characters, a length
limit - and is placed after a label on an indented line, so it cannot pass for a line of the first kind.

Nor can it be made to LOOK like one.  A long line is broken by whatever shows it, wherever its window ends - and text
that is padded to the right length would begin the next line of the window with a command of its author's choosing.  So
text from elsewhere is marked where it enters a line (``ext``), and every line that holds marked text is laid out before
it leaves this module (``layout`` / ``wrap``): at most ``WRAP`` columns, and what does not fit goes on in lines that
begin with ``┆``.  In a window that is at least that wide, every line a reader sees begins with something this module
wrote, and a command that was planted in the text is cut by those marks wherever it runs over a line.  The module's own
lines - the commands among them - hold no mark and are never touched.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from research.validate.injection import visible

SUBJECT_PREFIX = "[LLMOps portal] "
MAX_OPERATIONS = 8  # in a message; `portalctl show` prints all of them
MAX_CHANGES = 20
#: A message travels inside the state machine's state (256 KB in all, UTF-8): 16,000 characters are at most 48 KB.
MAX_MESSAGE_CHARS = 16_000
CLIPPED = "__truncated__"  # how publish.Review marks a value it cut short


def clean(text: Any, limit: int = 400) -> str:
    """Text from somewhere else as one harmless line of at most ``limit`` characters.

    What a reader cannot see is taken out (``visible``: zero-width and direction marks, soft hyphens, tag characters,
    half surrogates) and every control character becomes a blank - the same reading of the text that the publisher's
    checks use, so a link or an instruction cannot be spelled so that the check misses it and the message shows it.
    """
    flat = " ".join(visible(str(text if text is not None else "")).split())
    return flat if len(flat) <= limit else flat[: max(limit - 1, 0)] + "…"


def clean_block(text: Any, gutter: str = "", laid_out: bool = False) -> str:
    """Several lines of text from somewhere else, made safe to print: its line breaks stay, everything ``clean``
    removes goes, and every line is wrapped (``wrap``).  ``gutter``: what every line begins with, so that none of
    them begins with the text itself.  ``laid_out``: the text is a message this module laid out before (it comes back
    from the pipeline that sent it) - its lines are cleaned and otherwise left as they are, its commands whole.

    A line ends at a line feed and nowhere else.  ``str.splitlines`` also breaks at a form feed, a vertical tab, the
    information separators, NEL, U+2028 and U+2029 - and a JSON string keeps the last three unescaped, so a digest
    could bring its own line breaks into what is printed.  They are blanks here, like every other control character.
    """
    lines = str(text if text is not None else "").replace("\r\n", "\n").split("\n")
    cleaned = ["".join(" " if ch.isspace() else ch for ch in visible(line)).rstrip() for line in lines]
    if not laid_out:  # (the gutter stands in front of every line, the continued ones too)
        cleaned = [piece for line in cleaned for piece in wrap(line, WRAP - columns(gutter)).split("\n")]
    return "\n".join((gutter + line).rstrip() for line in cleaned)


#: how wide a line with text from elsewhere may be, in columns of a fixed-width font (see the module's docstring)
WRAP = 76
#: what a line begins with when it goes on from the line above
CONTINUED = "┆ "


#: Stands on both sides of text from elsewhere until the line it is on has been laid out.  A control character: ``clean``
#: turns every control character of the text itself into a blank, so text cannot bring a mark of its own.
MARK = "\x1f"


def ext(text: Any, limit: int = 400) -> str:
    """``clean(text, limit)``, marked as text from elsewhere - for a line that ``layout`` (or a function that calls it:
    every block below, ``_lines``, portalctl's ``say``) is going to see.  One plain word (an id, a path, an address:
    ``_WHOLE``) needs no mark: it holds no blank, so it is not a command, and nothing has to be done to it."""
    flat = clean(text, limit)
    return flat if not flat or _WHOLE.fullmatch(flat) else MARK + flat + MARK


def layout(text: Any, width: int = WRAP) -> str:
    """``text`` ready to be shown: every line that holds marked text is wrapped (``wrap``) and the marks are gone; the
    other lines - this module's own, a command among them - are left exactly as they are."""
    out = []
    for line in str(text).split("\n"):
        out.append(wrap(line.replace(MARK, ""), width) if MARK in line else line)
    return "\n".join(out)


def columns(text: str) -> int:
    """How many columns ``text`` takes in a fixed-width font, at most (East Asian wide and ambiguous characters: two)."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WFA" else 1 for ch in text)


def _cut(token: str, room: int) -> tuple[str, str]:
    """``token`` split where ``room`` columns are full (at least one character goes into the first part)."""
    used = 0
    for i, ch in enumerate(token):
        used += columns(ch)
        if used > room and i:
            return token[:i], token[i:]
    return token, ""


#: a word is laid out in runs: what is printable ASCII, and what is not ("：" belongs to the sentence, "s3://bucket/x" does not)
_RUNS = re.compile(r"[\x21-\x7e]+|[^\x21-\x7e]+")
#: A run of ASCII that is kept in one piece however long it is: an address, a path, an id, a checksum, a number - so
#: that it can still be opened or pasted.  Holding no blank it is not a command; and nothing in it is expanded into one
#: by a shell (no ``$``, no back-tick, no brace, no quote, no redirection).
_WHOLE = re.compile(r"[A-Za-z0-9._~:/?#\[\]@!&()*+,;=%-]+")
#: how much may stand on a line in front of a run that is too long for any line (a label), for the run to stay behind it
_LABEL = 24
#: the two columns at the end of a line are kept for one of these, so that a sentence's last mark is not a line of its own
_CLOSERS = frozenset("。，、；：！？）」』】》〉…")
_SPARE = 2


def wrap(text: Any, width: int = WRAP) -> str:
    """``text`` with every line that is wider than ``width`` columns continued on lines that begin with the line's own
    indentation and ``┆``.  Lines are broken at blanks and between ASCII and other text; text that is wider than a
    line is cut - except a run that is an address, a path or an id (``_WHOLE``), which stays in one piece and is the
    last thing on its line."""
    out: list[str] = []
    for line in str(text).split("\n"):
        if columns(line) <= width:
            out.append(line)
            continue
        body = line.lstrip(" ")
        indent = " " * min(len(line) - len(body), 16)
        lead = indent + "  " + CONTINUED
        fresh = max(width - columns(lead) - _SPARE, 24)  # what a continued line has room for
        current, room, used, spare = indent, max(width - len(indent) - _SPARE, 24), 0, _SPARE

        def flush() -> None:
            nonlocal current, room, used, spare
            out.append(current.rstrip())
            current, room, used, spare = lead, fresh, 0, _SPARE

        for word in body.split(" "):
            glue = " " if used else ""
            for run in _RUNS.findall(word):
                while run:
                    need = columns(run) + len(glue)
                    if need <= room:
                        current, room, used, run = current + glue + run, room - need, used + need, ""
                    elif used and not glue and run[0] in _CLOSERS and room < columns(run[0]) <= room + spare:
                        current, room, used, spare, run = current + run[0], 0, used + 1, 0, run[1:]
                    elif _WHOLE.fullmatch(run):
                        if used and not (columns(run) > fresh and used <= _LABEL):
                            flush()  # it fits on a line of its own, or what stands here is more than a label
                            glue = ""
                            continue
                        current, used = current + glue + run, used + need
                        run = ""
                        flush()
                    elif room - len(glue) < 4 and used:
                        flush()
                        glue = ""
                        continue
                    else:
                        piece, run = _cut(run, room - len(glue))
                        if run and run[0] in _CLOSERS and spare:
                            piece, run = piece + run[0], run[1:]
                        current, used = current + glue + piece, used + 1
                        flush()
                    glue = ""
        if used:
            out.append(current.rstrip())
    return "\n".join(out)


def subject(text: str) -> str:
    """An SNS-safe subject: printable ASCII, one line, fewer than 100 characters, not starting with whitespace."""
    flat = re.sub(r"[^\x20-\x7e]", "?", " ".join(str(text).split()))
    return (SUBJECT_PREFIX + flat)[:99].rstrip()  # SNS: "less than 100 characters long"


def _lines(*parts: Any) -> str:
    """Join the parts that have something to say; "" is a blank line (never two in a row, none at the ends).  Every
    part is laid out on the way (``layout``): what comes back holds no mark and is ready to send."""
    out: list[str] = []
    for part in parts:
        if part is None or part == []:
            continue
        text = layout(part)
        if text == "" and (not out or out[-1] == ""):
            continue
        out.append(text)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _bullets(items: Any, limit: int = 12, width: int = 400) -> str:
    items = items if isinstance(items, list) else []
    shown = [f"  - {ext(i, width)}" for i in items[:limit]]
    if len(items) > limit:
        shown.append(f"  …（還有 {len(items) - limit} 項）")
    return layout("\n".join(shown))


def _value(value: Any, limit: int | None = 160) -> str:
    """A JSON value on one line (``limit=None``: all of it)."""
    if isinstance(value, dict) and set(value) == {CLIPPED}:
        return ext(value[CLIPPED], 10**9 if limit is None else limit) + "…（已截斷）"
    return ext(json.dumps(value, ensure_ascii=False), 10**9 if limit is None else limit)


def _proposal_block(p: Any) -> str:
    if not isinstance(p, dict) or not p:
        return ""
    files = p.get("opsByFile") if isinstance(p.get("opsByFile"), dict) else {}
    by_file = "、".join(f"{ext(f, 120)} ×{ext(n, 6)}" for f, n in list(files.items())[:20])
    # (an answer about a proposal that is no longer at hand - decided earlier, by somebody else - knows its id and
    #  little more: what is not known is left out, not printed as "0 operations")
    counted = "ops" in p
    return _lines(
        f"提案：{ext(p.get('id'), 100)}" + (f"（run {ext(p.get('runId') or '—', 100)}，依據 {ext(p.get('basedOn') or '—', 100)}）" if counted else ""),
        f"摘要：{ext(p.get('summary'), 1200)}" if p.get("summary") else None,
        (f"提案內容：{ext(p.get('ops', 0), 6)} 個 operation" + (f"（{by_file}）" if by_file else "")
         + f"、新來源 {ext(p.get('newSources', 0), 6)} 筆、引文 {ext(p.get('evidence', 0), 6)} 則") if counted else None,
        f"Digest {ext(p.get('digestId'), 40)}：{ext(p.get('digestHeadline'), 500)}" if p.get("digestId") else None,
        f"未能確認的項目：{ext(p.get('unverified'), 6)} 項（請在 portal 的「本期尚未驗證」核對）" if p.get("unverified") else None,
    )


def review_block(review: Any, *, limit: int | None = MAX_CHANGES, width: int | None = 160, proposal_id: str = "") -> str:
    """What publishing really changes, as the publisher computed it from the live data (``publish.Review.to_doc``).

    This is the part to judge a proposal by: it does not depend on how the proposal describes itself.
    ``limit=None`` / ``width=None`` (a person asked to see everything) cut nothing.
    """
    if not isinstance(review, dict):
        return ""
    counts = review.get("counts") if isinstance(review.get("counts"), dict) else {}
    changes = [c for c in review.get("changes") or [] if isinstance(c, dict)]
    shown = changes if limit is None else changes[:limit]
    hidden = len(changes) - len(shown) + (review.get("more") if isinstance(review.get("more"), int) else 0)
    stamps = counts.get("stamps") or 0
    moved = counts.get("reordered") or 0
    lines = [f"實際變更（與上線中的 {ext(review.get('base'), 40)} 逐值比對）：修改 {counts.get('modified', 0)}、新增 {counts.get('added', 0)}、"
             f"刪除 {counts.get('removed', 0)}" + (f"、順序變動 {moved}" if moved else "") + (f"；另有時間戳記 {stamps} 處" if stamps else "")]
    for c in shown:
        kind = c.get("kind")
        where = f"{ext(c.get('file'), 120)}{ext(c.get('path'), 240)}"
        if kind == "modified":
            lines += [f"  ~ {where}", f"      {_value(c.get('before'), width)} → {_value(c.get('after'), width)}"]
        elif kind == "removed":
            lines += [f"  - {where}", f"      刪除 {_value(c.get('before'), width)}"]
        elif kind == "reordered":
            lines += [f"  ⇅ {where}", f"      順序 {_value(c.get('before'), width)} → {_value(c.get('after'), width)}"]
        elif kind == "added":
            lines += [f"  + {where}", f"      新增 {_value(c.get('after'), width)}"]
        else:  # a kind this version does not know: shown as it is, never as something it is not
            lines += [f"  ? {where}（{ext(kind, 20)}）", f"      {_value(c.get('before'), width)} → {_value(c.get('after'), width)}"]
    if hidden > 0:
        lines.append(f"  …另有 {hidden} 處" + (f"（完整內容：python scripts/portalctl.py show {ext(proposal_id, 100)}）" if proposal_id else ""))
    sources = [s for s in review.get("newSources") or [] if isinstance(s, dict)]
    if sources:
        lines.append(f"新增的來源（{len(sources)}）：")
        for s in sources if limit is None else sources[:limit]:
            lines.append(f"  + {ext(s.get('id'), 80)}（{ext(s.get('publisher'), 80)} · {ext(s.get('type'), 40)}）{ext(s.get('title'), 200)}")
            lines.append(f"      {ext(s.get('url'), 400)}")
    digest = review.get("digest")
    if isinstance(digest, dict):
        sections = digest.get("sections") if isinstance(digest.get("sections"), dict) else {}
        lines.append(f"新的 digest {ext(digest.get('id'), 60)}（{ext(digest.get('period'), 60)}）：{ext(digest.get('headline'), 500)}")
        if sections:
            lines.append("      各段的項目數：" + "、".join(f"{ext(k, 60)} ×{ext(n, 6)}" for k, n in sections.items()))
    as_of = review.get("asOf")
    if isinstance(as_of, dict) and as_of:
        dates = sorted({ext(d, 20) for d in as_of.values()})
        lines.append(f"「資料查核至」（asOf）改為 {'、'.join(dates)}：" + "、".join(ext(f, 120) for f in as_of))
    return layout("\n".join(lines))


def operations_block(operations: Any, *, limit: int | None = MAX_OPERATIONS, width: int = 300) -> str:
    """Why: the rationale and the quotes the proposal gives for each of its operations (``operations`` as in release.json).

    What an operation changes is not repeated here - ``review_block`` shows that, computed independently.
    """
    ops = [op for op in operations or [] if isinstance(op, dict)] if isinstance(operations, list) else []
    if not ops:
        return ""
    shown = ops if limit is None else ops[:limit]
    verbs = {"add": "新增", "replace": "修改", "remove": "刪除", "create": "建立檔案"}
    lines = [f"提案的理由與引文（{len(ops)} 個 operation" + (f"，列出前 {len(shown)} 個" if len(shown) < len(ops) else "") + "）："]
    for i, op in enumerate(shown, 1):
        lines.append(f"  {i}. {verbs.get(str(op.get('op')), ext(op.get('op'), 20))} {ext(op.get('file'), 120)}{ext(op.get('path'), 240)}")
        if op.get("rationale"):
            lines.append(f"       理由：{ext(op['rationale'], width)}")
        evidence = [e for e in op.get("evidence") or [] if isinstance(e, dict)] if isinstance(op.get("evidence"), list) else []
        for ev in evidence if limit is None else evidence[:2]:
            lines.append(f"       依據（{ext(ev.get('tier'), 20)} · {ext(ev.get('publisher'), 60)}）：「{ext(ev.get('quote'), width)}」")
            # where on the page it stands: a row says little without the headings of its columns and of its section
            if ev.get("header"):
                lines.append(f"       表頭：{ext(ev['header'], 200)}")
            if ev.get("under"):
                lines.append(f"       位置：{ext(ev['under'], 200)}")
            lines.append(f"       {ext(ev.get('url'), 400)}")
        if not evidence:
            lines.append("       （這一項沒有附引文）")
    return layout("\n".join(lines))


def _changes_block(changes: Any) -> str:
    out = []
    changes = changes if isinstance(changes, dict) else {}
    for key, label in (("added", "新增"), ("modified", "修改"), ("removed", "移除")):
        files = changes.get(key) if isinstance(changes.get(key), list) else []
        if files:
            out.append(f"{label} {len(files)} 個檔案：" + "、".join(ext(f, 160) for f in files[:8]) + ("…" if len(files) > 8 else ""))
    return layout("\n".join(out))


def _fit(*parts: Any) -> str:
    """The message, cut if it is still too long (the callers below choose how much detail fits first)."""
    message = _lines(*parts)
    return message if len(message) <= MAX_MESSAGE_CHARS else message[: MAX_MESSAGE_CHARS - 20] + "\n…（訊息過長，已截斷）"


#: (changes shown, operations shown): tried in this order until the message fits.  What really changes is given up
#: last, the proposal's own account of itself first - and never the commands, which are not part of either.
DETAIL = ((MAX_CHANGES, MAX_OPERATIONS), (MAX_CHANGES, 3), (MAX_CHANGES, 0), (8, 0), (0, 0))


def for_outcome(outcome: dict, *, portal_url: str = "", commands: dict[str, str] | None = None,
                operations: list[dict] | None = None) -> dict[str, str]:
    """``outcome`` is ``Outcome.to_state()``.  ``commands`` are ready-to-paste commands for the follow-up actions and
    ``operations`` the proposal's rationale and quotes, so that the reader can judge a change without opening anything.

    ``portal_url`` is the address of the UI, for people to open.  The data location is never mentioned: its URL carries
    the access token and a notification is forwarded more freely than that should be.
    """
    commands = commands or {}
    status, p = outcome.get("status"), outcome.get("proposal") if isinstance(outcome.get("proposal"), dict) else {}
    rid, pid = clean(outcome.get("releaseId"), 40), clean(p.get("id"), 100)  # (for the subject; in the text: ext(...))
    where = f"在 portal 查看：{ext(portal_url, 300)}" if portal_url else None
    full = f"完整內容與引文：python scripts/portalctl.py show {ext(pid, 100)}" if pid else None
    notes = outcome.get("warnings") if isinstance(outcome.get("warnings"), list) else []
    after = _lines("注意：" if notes else None, _bullets(notes) if notes else None)
    reasons = _bullets(outcome.get("reasons"))

    def detailed(build: Any) -> str:
        message = ""
        for change_limit, op_limit in DETAIL:
            what = review_block(outcome.get("review"), limit=change_limit, proposal_id=pid)
            why = operations_block(operations, limit=op_limit) if op_limit else ("（提案的理由與引文沒有列在這裡）" if operations else "")
            message = build(what, why)
            if len(message) <= MAX_MESSAGE_CHARS:
                break
        return _fit(message)

    if status == "published":
        return {"subject": subject(f"published {rid} ({clean(outcome.get('changed', 0), 6)} files)"),
                "message": detailed(lambda what, why: _lines(
                    f"已發布新的資料版本 {ext(rid, 40)}（前一版 {ext(outcome.get('previousReleaseId') or '無', 40)}）。UI 下次載入就會讀到，不需要重新部署。",
                    "", _proposal_block(p), _changes_block(outcome.get("changes")), "", what, "", why, "", where,
                    f"如需撤回這一版：{commands['rollback']}" if commands.get("rollback") else None, "", after))}
    if status == "pending":
        return {"subject": subject(f"approval needed: {pid}"),
                "message": detailed(lambda what, why: _lines(
                    "有一份提案通過了驗證，但需要人工核准後才會發布：", reasons, "", _proposal_block(p), "", what, "", why, full, "",
                    f"核准並發布：{commands['approve']}" if commands.get("approve") else None,
                    f"捨棄：{commands['discard']}" if commands.get("discard") else None,
                    "核准的是上面這份內容（指令裡的 sha256 會核對）；核准時會再對上線中的資料驗證一次，資料若已變動，提案會被拒絕。"))}
    if status == "rejected":
        return {"subject": subject(f"update NOT published: {pid or clean(outcome.get('kind'), 20)}"),
                "message": _fit("這次的更新沒有發布，portal 資料維持原狀。原因：", reasons, "",
                                _proposal_block(p), "", f"目前上線的版本：{ext(rid, 40)}" if rid else None, "", after)}
    paused = bool(outcome.get("autoPublishPaused"))
    resume_hint = _lines("自動發布目前是暫停的：之後的提案都會等待核准。" if paused else None,
                         f"恢復自動發布：{commands['resume']}" if paused and commands.get("resume") else None)
    if status == "rolled_back":
        return {"subject": subject(f"rolled back to {rid}"),
                "message": _fit(f"已回復到資料版本 {ext(rid, 40)}（原為 {ext(outcome.get('previousReleaseId'), 40)}）。", _changes_block(outcome.get("changes")), "",
                                where, "", resume_hint,
                                "（暫停的原因：下一次研究讀到的是回復後的資料，很可能再提出同樣的變更；確認沒問題後再恢復。）" if paused else None)}
    if status == "paused":
        return {"subject": subject("automatic publishing paused"),
                "message": _fit("自動發布已暫停。研究照常執行，但每一份提案都會等待核准。", reasons, "", resume_hint)}
    if status == "resumed":
        return {"subject": subject("automatic publishing resumed"),
                "message": _fit("已恢復自動發布（發布模式為 review 的部署仍然每份提案都要核准）。", reasons)}
    if status == "discarded":
        return {"subject": subject(f"proposal discarded: {pid}"),
                "message": _fit(f"已捨棄待核准的提案 {ext(pid, 100)}。", reasons)}
    if outcome.get("kind") == "control":  # e.g. resume when nothing was paused
        return {"subject": subject(f"no change ({clean(status, 30)})"),
                "message": _fit("沒有變更。", reasons, "", f"上線中的版本：{ext(rid, 40)}" if rid else None, resume_hint)}
    label = "這份提案的內容已經在上線的資料裡" if status == "already_published" else "這次沒有需要變更的內容"
    return {"subject": subject(f"no change ({clean(status, 30)})"),
            "message": _fit(f"{label}；上線中的版本仍是 {ext(rid, 40) or '（未知）'}。", reasons, "", _proposal_block(p), "", after)}


def for_research(status: str, *, run_id: str = "", detail: str = "", errors: list[str] | None = None) -> dict[str, str]:
    """A research run that ended without anything to publish."""
    return {"subject": subject(f"research finished without a proposal ({clean(status, 30)})"),
            "message": _fit(f"研究 run {ext(run_id, 100)} 已結束，但沒有可發布的提案（status = {ext(status, 40)}）；portal 資料維持原狀。",
                            f"說明：{ext(detail, 1200)}" if detail else None,
                            "驗證未通過的原因：" if errors else None, _bullets(errors or [], limit=20) if errors else None)}


def for_failure(error: str, cause: str, *, step_hint: str = "", execution: str = "", data_untouched: bool = True) -> dict[str, str]:
    """``data_untouched``: the failing step cannot have changed what is live (it ran before anything was published)."""
    state = ("portal 資料沒有變更。" if data_untouched else
             "失敗發生在發布的步驟：資料可能已經更新，也可能沒有；請用 `python scripts/portalctl.py status` 確認目前上線的版本。")
    return {"subject": subject(f"FAILED: {clean(error or 'error', 60)}"),
            "message": _fit(f"LLMOps portal 的更新流程失敗。{state}", "",
                            f"錯誤：{ext(error, 200)}" if error else None, f"原因：{ext(cause, 3000)}" if cause else None,
                            f"步驟：{ext(step_hint, 80)}" if step_hint else None, f"Execution：{ext(execution, 300)}" if execution else None, "",
                            "可以在 Step Functions 的 execution history 看每一步的輸入與輸出；修正後重新啟動一次 execution 即可。")}
