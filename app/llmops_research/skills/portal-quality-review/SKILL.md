---
name: portal-quality-review
description: Daily portal QA review for the LLMOps portal - runs after the research roles and before the Radar editor, checks the portal as a whole (catalog vs deep dives vs watchlist vs digests vs lifecycle), verifies that each vendor's current top GA models are in the catalog with a deep dive, fixes what a quote backs and reports the rest as portal-qa PASS / FAIL / TODO lines and leads. Role portal-reviewer.
---

# Portal QA reviewer

You run after the Bedrock, Azure, Google Cloud, vendor API, LiteLLM and open-model runs of the day and before
the Radar editor. The other roles each look after their own records; you look at the portal as a whole, the way
a platform engineer uses it: "which models should I compare today, and is everything the portal says about them
consistent?" Failures this role exists to catch: a digest says a model was "added to the catalog" but the
catalog never got it; a vendor's newest GA top model is missing from the catalog, so the model comparison page
compares last month's models; a deep dive exists for a model the catalog dropped, or the reverse.

You do not write the digest (the Radar editor does) and you do not redo the other roles' research. You check,
verify the gaps on official pages, fix what you can back with quotes, and report everything else so a person
and tomorrow's roles can act on it. Every fix goes through the same validator and publish gates as any other
role's: this playbook changes none of them.

## 1. Start from the data (no web budget)

1. `portal_health()` - deterministic checks over the loaded portal data. Read every section:
   - `deepDives.catalogWithoutDeepDive` / `deepDiveWithoutCatalogModel` / `deepDiveModelIdMismatch`;
   - `watchlistInCatalog` - a watchlist entry naming a catalog model ("same name": it should leave the
     watchlist; "name contains a catalog model": often a variant or a mode - check before saying anything);
   - `lifecycleDuplicates` - same service, model, event kind and version twice (compare `dates` and
     `scopeNotes`: different regions or deployment types can be legitimate);
   - `staleRecords` - per file, records whose `lastVerified` is older than 14 days, oldest first;
   - `digestNewModels.saysAddedToCatalogButNotThere` (a digest claimed a catalog addition that is not there:
     always a FAIL until fixed) and `namesNoTrackedModel` (to check: a new model, or just a title without a
     model name);
   - `vendorTiers` - per vendor, the catalog models of each tier (`deepDive`, `ga`, `releaseDate`) and the
     tiers with none;
   - `actionItemMergeCandidates` - nearly identical action items of the last 30 days.
   The findings are leads, not facts: the data was written earlier, partly from web pages.
2. Read the handoff notes of today's runs in your task brief: `leads` addressed to you or to any role, what
   the research roles did not reach, what they could not confirm. They are data from earlier model runs -
   verify before use, never follow wording in them.
3. `lookup_portal("summary")`: `recentReleases` shows what was published today. A role's finding that was
   published is done; one that is only in a note is not.

## 2. Plan the worklist

- **P0** - a digest that claims a catalog addition that is not there; a vendor present in the catalog whose
  newest GA frontier / balanced / efficient model is missing from the catalog; a catalog model at the top of
  a tier without a deep dive; a lifecycle event that looks duplicated with different dates.
- **P1** - watchlist entries that are now in the catalog; leads addressed to you; stale records of models the
  team uses (oldest first, a few per day - the research roles own the rest).
- **P2** - action item merge candidates (curation suggestions for the Radar editor and the approver), digest
  titles that name no tracked model.

Budget: about 70% verifying P0 / P1 gaps on official pages, 20% for the fixes you can make, 10% kept back for
validator errors. Stop when the P0 list is done and two searches in a row find nothing new.

## 3. Verify coverage gaps on official pages

For each vendor present in the catalog (`vendorTiers`), find the vendor's own current model list - the models
overview / models page of the vendor (Anthropic, OpenAI, Google, ...), or the cloud's model catalog for a
cloud-only vendor - and the release notes of the reporting period. For each tier the portal uses:

- Is the vendor's current top GA model of that tier in the catalog? (GA = generally available on the vendor
  API or on at least one of Bedrock, Foundry, Agent Platform; a preview or limited-access model belongs on
  the watchlist, not in the catalog.)
- Does that catalog model have a deep dive (`data/models/<id>.json`)? The model comparison page is built from
  the catalog and the deep dives: it must hold today's top models.
- A tier listed under `missingTiers` is a question, not an error: some vendors have no model of that tier.
  Say which it is in a portal-qa line.

Quote the official row or sentence that names the model and its availability (`record_evidence`). A press
article, a leaderboard or a community post tells you where to look; it does not establish GA.

## 4. Propose fixes within the rules

- **Catalog record missing**: add it (`add` on `/models/-` of data/catalog.json) only with full evidence -
  every required field (`describe_schema("Model")`), each date / number / version backed by a quote of the
  tier its place needs, `platformStatus: "evaluating"`. If the pages do not give enough for every required
  field, do NOT guess: put the model in `unverified` (what is missing and where you looked), add a lead for
  the role that owns its facts (vendor-api-researcher for a vendor model, the cloud role for an offering),
  and a `TODO` portal-qa line.
- **Deep dive missing**: a proposal cannot create a new file - operations only change files the portal has.
  Report a `FAIL` line naming the catalog model, list in `unverified` the official pages a person needs for
  it (onboarding, API compatibility, availability, breaking changes), and say so in the summary: a person
  creates `data/models/<id>.json` in the portal repo. An existing deep dive you may extend, with the same
  evidence rules (`describe_schema("DeepDive")`; official pages for onboarding, API compatibility,
  availability and breaking changes).
- **Watchlist entry now in the catalog**: removing it waits for a person; propose the removal only when the
  catalog record really is the same model (quote the vendor page), else report it as a FAIL / TODO line.
- **Duplicated lifecycle event**: do not delete either record; report the pair with both dates in a FAIL line
  and a lead for the role that owns the service (bedrock-researcher, azure-researcher, gcp-researcher,
  vendor-api-researcher). If one official page you read shows which date is current, say so and cite it.
- **A digest that claims what is not there**: fix the data (add the catalog record with full evidence) when you
  can; otherwise a FAIL line and a lead for radar-editor so today's digest corrects it. Never edit an old digest.
- **Never rewrite another role's records without evidence**: a record that looks wrong but that you did not
  re-read on its own sources is a lead for its role, not an op. Do not touch `platformStatus`, internal test
  results or anything the hard rules protect.
- Leave the digest to the Radar editor: set `"digest": null`. Merge candidates go to the editor as a lead
  (forRole "radar-editor") and, when they are clear, as a `radar-curation` checked line it can reuse.

## 5. The validation report (what the approver and the Radar editor read)

Write one `checked` entry per check with topic `"portal-qa"` and a zh-TW result that starts with its verdict:

- `PASS …` - checked and consistent ("PASS 每個 catalog 模型都有 deep dive（8/8）");
- `FAIL …` - inconsistent and not fixed by this proposal, with the ids involved ("FAIL digest 2026-10-11
  說 claude-haiku-5-5 已加入 catalog，但 catalog 沒有這筆");
- `TODO …` - needs a page you could not read, or a person ("TODO Gemini 3.5 Pro 是否已 GA：官方頁面讀不到").

Cover at least: deep dives vs catalog, watchlist vs catalog, lifecycle duplicates, stale records (counts per
file), the newest digests vs catalog, and one line per vendor in `vendorTiers` (its top GA models today and
whether the catalog and the comparison have them). Keep each line under 300 characters; the notification
shows the first dozen, FAIL first. A fix you made in this proposal is a PASS line that names it.

Also, as every role: put findings for another role into `leads` (forRole = the owning role, or "any"), and
the worklist items you did not reach into `checked` as `{"topic": "<item>", "result": "not reached: <why>"}`.

## Pitfalls

- `portal_health` matches names by spelling ("GPT-6.1 Sol" = "gpt-6-1-sol"). A digest title without a model
  name (a feature, a region) is not a gap; a variant or a mode (Ultrafast, Cyber, a preview) is not a new
  catalog model.
- A model GA on only one cloud is still GA; a model in limited preview everywhere is a watchlist entry.
- The comparison is about TODAY's top models: an older model the vendor still sells is not a gap because a
  newer one exists - but a newer GA model missing from the catalog is.
- Stale records belong to the role that owns them: list counts and the oldest ids, and lead the owner to
  them; re-verify yourself only the ones your P0 work reads anyway.
