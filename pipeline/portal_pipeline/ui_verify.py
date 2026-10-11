"""Daily "the UI is updated" notification: check the deployed site, not just the pipeline.

The update pipeline already reports every execution (published / pending / rejected / failed). What it cannot say is
whether a person opening the portal now sees today's data: that depends on the Amplify build that runs after the
pipeline (infra/amplify-trigger.yaml). This module closes that gap.

Two entry points (infra/ui-verify.yaml):

* ``on_amplify_event`` - EventBridge rule on "Amplify Deployment Status Change" for the portal branch.
  FAILED -> a failure message, every time. SUCCEED -> check the live site; when it shows today's merged Release Radar
  and the release that is live on the data location, send the day's single "daily update live" message.
* ``deadline_check`` - EventBridge Scheduler, once a day after the last research run should have finished. When no
  "daily update live" message went out today, check again and send either that message or "daily update NOT complete"
  with what is missing (silence is not success).

"Once a day" is a create-only marker ``<STATE prefix>notified/ui-<date>.json`` (S3 ``If-None-Match: *``), so two builds
finishing together, or a retried event, still send one message.

What the check reads from the site (all built by llmops-portal: tools/build_site.mjs, tools/build_radar.py):
  /                     answers 200 and is the app (has the root element)
  /data/manifest.json   ``release`` (the release the build fetched) and ``radarAsOf``
  /data/radar.json      the merged Release Radar: asOf, action items
Text from the site is data: it reaches the message only through ``notify.ext`` / ``notify.layout``.

Environment: SITE_URL (https://…, the branch URL), TOPIC_ARN, DATA_URI (s3://bucket/portal/), STATE_URI
(s3://bucket/pipeline/), optional STATE_MACHINE_ARN (adds today's research runs to the message), SITE_AUTH_SECRET_ARN
(Secrets Manager secret "user:password" when the site has Amplify basic auth), TIMEZONE (default Asia/Taipei).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import notify
from .storage import Conflict, NotFound, Store, open_store

POINTER = "manifest.json"
MAX_BODY = 8 * 1024 * 1024
TIMEOUT = 20
TRIGGER_LABEL = {"schedule-aws": "AWS", "schedule-azure": "Azure", "schedule-gcp": "Google Cloud",
                 "schedule-litellm": "LiteLLM", "schedule-digest": "每日紀錄（digest）", "schedule": "每週"}

Fetch = Callable[[str], tuple[int, bytes]]


@dataclass
class Check:
    """What the live site shows, and what is wrong with it (``problems`` empty = the UI is up to date)."""
    today: str
    site_ok: bool = False
    release_on_site: str | None = None
    release_live: str | None = None
    radar_as_of: str | None = None
    radar: dict = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# ------------------------------------------------------------------------------------------------- reading
def http_fetcher(auth: str | None = None) -> Fetch:
    headers = {"User-Agent": "llmops-portal-ui-verify", "Cache-Control": "no-cache"}
    if auth:
        headers["Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()

    def fetch(url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - https URL from configuration
                return r.status, r.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            return e.code, b""
    return fetch


def _json(fetch: Fetch, url: str) -> tuple[Any, str | None]:
    name = "data/" + url.rsplit("/", 1)[-1]
    try:
        status, body = fetch(url)
    except Exception as e:  # network error, timeout
        return None, f"讀不到網站的 {name}：{type(e).__name__}"
    if status != 200:
        return None, f"網站的 {name} 回 HTTP {status}"
    if len(body) > MAX_BODY:
        return None, f"網站的 {name} 太大"
    try:
        return json.loads(body), None
    except ValueError:
        return None, f"網站的 {name} 不是 JSON"


def today_in(tz: str, now: datetime | None = None) -> str:
    return (now or datetime.now(ZoneInfo(tz))).astimezone(ZoneInfo(tz)).date().isoformat()


def check_site(site_url: str, data: Store, fetch: Fetch, today: str) -> Check:
    c = Check(today=today)
    base = site_url.rstrip("/") + "/"
    try:
        status, body = fetch(base)
        c.site_ok = status == 200 and b'id="root"' in body
        if not c.site_ok:
            c.problems.append(f"網站首頁回 HTTP {status}" if status != 200 else "網站首頁不是 portal（找不到 root element）")
    except Exception as e:
        c.problems.append(f"連不到網站：{type(e).__name__}")
        return c
    manifest, err = _json(fetch, base + "data/manifest.json")
    if err:
        c.problems.append(err)
    elif isinstance(manifest, dict):
        c.release_on_site = manifest.get("release") if isinstance(manifest.get("release"), str) else None
        c.radar_as_of = manifest.get("radarAsOf") if isinstance(manifest.get("radarAsOf"), str) else None
    try:
        pointer = json.loads(data.get(POINTER).data)
        c.release_live = pointer.get("releaseId") if isinstance(pointer, dict) else None
    except NotFound:
        c.problems.append("資料位置沒有 manifest.json（還沒有任何 release）")
    except Exception as e:
        c.problems.append(f"讀不到資料位置的 manifest.json：{type(e).__name__}")
    if c.release_live and c.release_on_site and c.release_on_site != c.release_live:
        c.problems.append(f"網站顯示的是 release {c.release_on_site}，上線中的是 {c.release_live}（網站還沒重新 build）")
    elif c.release_live and not c.release_on_site:
        c.problems.append("網站的 manifest 沒有記錄 release（build 沒有從資料位置抓資料？檢查 Amplify 的 PORTAL_DATA_BASE_URL）")
    radar, err = _json(fetch, base + "data/radar.json")
    if err:
        c.problems.append(err)
    elif isinstance(radar, dict):
        c.radar = radar
        c.radar_as_of = radar.get("asOf") if isinstance(radar.get("asOf"), str) else c.radar_as_of
    if c.radar_as_of != today:
        c.problems.append(f"Release Radar 的版本是 {c.radar_as_of or '（沒有）'}，不是今天 {today}（今天的每日紀錄還沒發布或還沒上網站）")
    return c


def todays_runs(sfn: Any, state_machine_arn: str, tz: str, today: str) -> list[tuple[str, str]]:
    """(label, status) of the executions started today, oldest first. Best effort: an error gives an empty list."""
    start = datetime.fromisoformat(today).replace(tzinfo=ZoneInfo(tz))
    out = []
    try:
        page = sfn.list_executions(stateMachineArn=state_machine_arn, maxResults=50)
        for e in page.get("executions", []):
            if e["startDate"] < start or e["startDate"] >= start + timedelta(days=1):
                continue
            label = "手動"
            try:
                trig = json.loads(sfn.describe_execution(executionArn=e["executionArn"]).get("input") or "{}").get("trigger")
                label = TRIGGER_LABEL.get(trig, notify.clean(trig, 40) if trig else "手動")
            except Exception:
                pass
            out.append((e["startDate"], label, e["status"]))
    except Exception:
        return []
    return [(label, status) for _, label, status in sorted(out)]


# ------------------------------------------------------------------------------------------------- messages
def _action_lines(radar: dict) -> list[str]:
    items = [a for a in radar.get("actionItems", []) if isinstance(a, dict)]
    new = [a for a in items if a.get("isNew")]
    p0 = [a for a in items if a.get("priority") == "P0"]
    lines = [f"行動項目：{len(items)} 項進行中（今天新增 {len(new)}，P0 {len(p0)}）"]
    if new:
        lines.append("今天新增：")
        lines += [f"  - {notify.ext(a.get('priority'), 4)} {notify.ext(a.get('action'), 220)}" for a in new[:10]]
    if p0:
        lines.append("P0：")
        lines += [f"  - 到期 {notify.ext(a.get('due') or '持續', 12)}：{notify.ext(a.get('action'), 220)}" for a in p0[:10]]
    return lines


def daily_message(c: Check, site_url: str, runs: list[tuple[str, str]]) -> dict[str, str]:
    body = [
        f"今天（{c.today}）的資料已經上網站：Release Radar 版本 {c.radar_as_of}，release {c.release_on_site}。",
        f"網站：{site_url}",
        "",
        *_action_lines(c.radar),
    ]
    if runs:
        body += ["", "今天的研究：" + "、".join(f"{label} {'✓' if s == 'SUCCEEDED' else s}" for label, s in runs)]
    body += ["", "這封信表示網站實際顯示的是今天的合併版本與上線中的 release（由 ui-verify 讀取網站確認）。"]
    return {"subject": notify.subject(f"Daily update live: {c.today} ({c.release_on_site})"), "message": notify.layout("\n".join(body))}


def incomplete_message(c: Check, site_url: str, runs: list[tuple[str, str]]) -> dict[str, str]:
    body = [f"今天（{c.today}）的每日更新還沒有完整出現在網站上：", *[f"  - {notify.ext(p, 300)}" for p in c.problems], ""]
    if runs:
        body.append("今天的研究：" + "、".join(f"{label} {s}" for label, s in runs))
    else:
        body.append("今天沒有找到研究的執行紀錄（排程沒有觸發？）。")
    body += [f"網站：{site_url}", "處理：portalctl executions / portalctl status；Amplify console 的 build 紀錄。"]
    return {"subject": notify.subject(f"Daily update NOT complete: {c.today}"), "message": notify.layout("\n".join(body))}


def build_failed_message(detail: dict, site_url: str) -> dict[str, str]:
    job = notify.clean(detail.get("jobId"), 40)
    body = [f"Amplify build 失敗（app {notify.ext(detail.get('appId'), 40)}，branch {notify.ext(detail.get('branchName'), 60)}，job {job}）。",
            "網站維持上一次成功的版本；資料位置不受影響。", f"網站：{site_url}", "處理：Amplify console 看這個 job 的 build log（常見：validate_data.py 失敗、資料位置讀不到）。"]
    return {"subject": notify.subject(f"UI build FAILED (job {job})"), "message": notify.layout("\n".join(body))}


# ------------------------------------------------------------------------------------------------- handlers
@dataclass
class Deps:
    site_url: str
    data: Store
    state: Store
    publish: Callable[[dict[str, str]], None]
    fetch: Fetch
    tz: str = "Asia/Taipei"
    runs: Callable[[str], list[tuple[str, str]]] = lambda today: []
    now: Callable[[], datetime] | None = None


def _marker(today: str) -> str:
    return f"notified/ui-{today}.json"


def _send_daily_once(d: Deps, c: Check) -> bool:
    """Write the day's marker (create-only); only the writer sends. Returns whether this call sent the message."""
    record = {"date": c.today, "release": c.release_on_site, "radarAsOf": c.radar_as_of, "at": datetime.now(ZoneInfo("UTC")).isoformat()}
    try:
        d.state.put(_marker(c.today), json.dumps(record).encode(), content_type="application/json", if_none_match=True)
    except Conflict:
        return False
    d.publish(daily_message(c, d.site_url, d.runs(c.today)))
    return True


def handle_amplify(event: dict, d: Deps) -> dict:
    detail = event.get("detail") if isinstance(event, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    status = detail.get("jobStatus")
    if status == "FAILED":
        d.publish(build_failed_message(detail, d.site_url))
        return {"result": "build-failed-notified"}
    if status != "SUCCEED":
        return {"result": "ignored", "jobStatus": notify.clean(status, 20)}
    today = today_in(d.tz, d.now() if d.now else None)
    if d.state.exists(_marker(today)):
        return {"result": "already-notified", "date": today}
    c = check_site(d.site_url, d.data, d.fetch, today)
    if not c.ok:  # e.g. a build after the cloud runs but before today's digest: the deadline check reports if it stays so
        return {"result": "not-yet", "problems": c.problems[:5]}
    return {"result": "notified" if _send_daily_once(d, c) else "already-notified", "date": today}


def handle_deadline(d: Deps) -> dict:
    today = today_in(d.tz, d.now() if d.now else None)
    if d.state.exists(_marker(today)):
        return {"result": "already-notified", "date": today}
    c = check_site(d.site_url, d.data, d.fetch, today)
    if c.ok:
        return {"result": "notified" if _send_daily_once(d, c) else "already-notified", "date": today}
    d.publish(incomplete_message(c, d.site_url, d.runs(today)))
    return {"result": "incomplete-notified", "problems": c.problems[:5]}


def _deps_from_env() -> Deps:  # pragma: no cover - wiring, exercised in AWS
    import boto3
    site = os.environ["SITE_URL"]
    if not site.startswith("https://"):
        raise ValueError("SITE_URL must be an https:// URL")
    auth = None
    if os.environ.get("SITE_AUTH_SECRET_ARN"):
        auth = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SITE_AUTH_SECRET_ARN"])["SecretString"]
    sns, topic = boto3.client("sns"), os.environ["TOPIC_ARN"]
    tz = os.environ.get("TIMEZONE", "Asia/Taipei")
    sm = os.environ.get("STATE_MACHINE_ARN")
    sfn = boto3.client("stepfunctions") if sm else None
    return Deps(
        site_url=site, data=open_store(os.environ["DATA_URI"]), state=open_store(os.environ["STATE_URI"]),
        publish=lambda m: sns.publish(TopicArn=topic, Subject=m["subject"], Message=m["message"]),
        fetch=http_fetcher(auth), tz=tz, runs=(lambda today: todays_runs(sfn, sm, tz, today)) if sm else (lambda today: []),
    )


def on_amplify_event(event: Any, context: Any = None) -> dict:  # pragma: no cover - thin wrapper
    return handle_amplify(event if isinstance(event, dict) else {}, _deps_from_env())


def deadline_check(event: Any = None, context: Any = None) -> dict:  # pragma: no cover - thin wrapper
    return handle_deadline(_deps_from_env())
