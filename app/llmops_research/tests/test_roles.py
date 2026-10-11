"""The daily research roles and the skills that are their playbooks (research/skills.py, skills/*/SKILL.md)."""

from __future__ import annotations

import datetime as dt

import pytest

from research import skills as S
from research.agent import parse_payload, system_prompt
from research.portal.data import recent_releases, release_base
from research.prompts import SYSTEM_PROMPT, build_task_prompt

TODAY = dt.date(2026, 10, 12)
LIMITS = {"searches": 8, "fetches": 14, "toolCalls": 60}


# --- the skills are packaged, well-formed and all in use ------------------------------------------------------------------------------
def test_every_role_loads_its_skills_and_every_skill_folder_belongs_to_a_role():
    used = {name for r in S.ROLES.values() for name in r.skills}
    folders = {p.name for p in S.SKILLS_DIR.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))}
    assert used == folders, f"skills without a role: {folders - used}; roles naming missing skills: {used - folders}"
    for r in S.ROLES.values():
        assert r.skills[0] == S.SHARED, f"{r.name}: the shared method comes first"
        for name in r.skills:
            skill = S.load_skill(name)
            assert skill.name == name and len(skill.description) > 40 and skill.body.startswith("# ")


def test_exactly_one_role_writes_the_digest():
    assert [r.name for r in S.ROLES.values() if r.digest] == ["radar-editor"]


def test_a_broken_skill_file_is_refused(tmp_path):
    def write(name: str, text: str) -> None:
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(text, encoding="utf-8")

    write("no-front", "# body")
    write("wrong-name", "---\nname: other\ndescription: something long enough\n---\n# body")
    write("no-description", "---\nname: no-description\n---\n# body")
    write("too-long", "---\nname: too-long\ndescription: d\n---\n" + "x" * (S.MAX_SKILL_CHARS + 1))
    for name, why in (("no-front", "front matter"), ("wrong-name", "folder name"), ("no-description", "description"),
                      ("too-long", "characters"), ("missing", "not packaged"), ("../etc", "not a skill name")):
        with pytest.raises(S.SkillError, match=why):
            S.load_skill(name, tmp_path)


# --- a request names its role -------------------------------------------------------------------------------------------------------
def test_a_role_is_validated_and_supplies_its_focus():
    task = parse_payload({"kind": "focus", "role": "bedrock-researcher"})
    assert task["role"] == "bedrock-researcher" and task["focus"] == ["lifecycle", "pricing", "new-models"]
    assert parse_payload({"kind": "focus", "role": "open-model-scout"})["focus"] == ["open-models"]
    assert parse_payload({"kind": "weekly", "role": "radar-editor", "includeDigest": True})["focus"] == ["new-models", "regulatory"]
    # a focus in the request wins over the role's default
    assert parse_payload({"kind": "focus", "role": "azure-researcher", "focus": ["lifecycle"]})["focus"] == ["lifecycle"]
    for bad in ("Bedrock", "admin", 3, ["bedrock-researcher"]):
        with pytest.raises(ValueError, match="unknown role"):
            parse_payload({"kind": "focus", "role": bad, "focus": ["lifecycle"]})
    with pytest.raises(ValueError, match="needs a non-empty focus"):
        parse_payload({"kind": "focus"})  # without a role a focus run still has to say what to research


def test_the_system_prompt_puts_the_hard_rules_before_the_playbook():
    assert system_prompt({"kind": "weekly"}) == SYSTEM_PROMPT  # no role: unchanged
    text = system_prompt({"kind": "focus", "role": "litellm-researcher"})
    assert text.startswith(SYSTEM_PROMPT)
    rest = text[len(SYSTEM_PROMPT):]
    assert "# Role: litellm-researcher" in rest and "hard rules and the validator above win" in rest
    assert rest.index('<skill name="llmops-deep-research">') < rest.index('<skill name="litellm-release-research">')
    assert "open-model-scouting" not in rest  # only the role's own skills


def test_a_research_role_is_told_not_to_write_the_digest():
    summary = {"manifest": {"kind": "release"}}
    task = parse_payload({"kind": "focus", "role": "gcp-researcher"})
    brief = build_task_prompt(task, summary, {}, TODAY, LIMITS)
    assert "Role: gcp-researcher" in brief and 'Do not write a digest (set "digest" to null)' in brief
    editor = parse_payload({"kind": "weekly", "role": "radar-editor", "includeDigest": True})
    assert "Finish with a weekly digest" in build_task_prompt(editor, summary, {}, TODAY, LIMITS)


# --- the editor sees what was published today -----------------------------------------------------------------------------------------
def test_the_pointer_s_recent_history_reaches_the_agent_bounded():
    history = [{"releaseId": f"20261012T0{i % 10}0000Z-0000000{i % 10}", "publishedAt": "2026-10-12T01:00:00Z", "kind": "agent",
                "summary": "x" * 900, "changed": 2, "by": "pipeline"} for i in range(30)]
    history.insert(3, "not an entry")
    recent = recent_releases(history)
    assert len(recent) == 12 and all(len(r["summary"]) == 300 and set(r) == {"releaseId", "publishedAt", "kind", "summary", "changed"} for r in recent)
    base, info = release_base({"schema": "portal-manifest/v1", "releaseId": "20261012T010000Z-00000001",
                               "basePath": "releases/20261012T010000Z-00000001/", "history": history[:2]})
    assert base == "releases/20261012T010000Z-00000001/" and len(info["recentReleases"]) == 2
    assert recent_releases(None) == [] and recent_releases([{"summary": "no id"}]) == []


# --- the daily run starts every role with a request the agent accepts ----------------------------------------------------------------
REPO = S.SKILLS_DIR.parents[2]


def _template(rel: str) -> dict:
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)  # CloudFormation tags: not needed here
    return yaml.load((REPO / rel).read_text(encoding="utf-8"), Loader=Loader)


def test_the_daily_orchestrator_runs_every_role_once_and_the_editor_last():
    states = _template("infra/daily-orchestrator.yaml")["Resources"]["DailyStateMachine"]["Properties"]["Definition"]["States"]
    tasks = [(name, s) for name, s in states.items() if s["Type"] == "Task"]
    roles = [s["Parameters"]["Input"]["request"]["role"] for _, s in tasks]
    assert sorted(roles) == sorted(S.ROLES) and len(roles) == len(set(roles))
    assert roles[-1] == "radar-editor"
    assert states["AllRoles"]["Result"] == roles  # "no roles given" means every role, in this order
    for name, s in tasks:
        task = parse_payload(s["Parameters"]["Input"]["request"])  # what the agent will receive (via the pipeline)
        assert task["focus"] and task["budget"]
        assert task.get("includeDigest", False) == S.ROLES[task["role"]].digest, name
        assert s["Catch"][0]["Next"] == s["Next"], f"{name}: a failed role must not stop the others"


def test_the_github_workflow_lists_the_same_roles():
    import yaml

    flow = yaml.safe_load((REPO / ".github/workflows/daily-research.yml").read_text(encoding="utf-8"))
    run = next(s["run"] for s in flow["jobs"]["start"]["steps"] if s.get("name", "").startswith("Start"))
    listed = [r for r in S.ROLES if f'"{r}"' in run]
    assert listed == list(S.ROLES)
    assert flow["permissions"] == {"id-token": "write", "contents": "read"}
    uses = [s["uses"] for s in flow["jobs"]["start"]["steps"] if "uses" in s]
    assert all(len(u.split("@")[1]) == 40 for u in uses), "actions are pinned to a commit"
