---
name: llmops-deep-research
description: Evidence-first daily research method for the LLMOps portal - plan from the portal's current state, triage sources by tier, spend a fixed budget on what can have changed, and report coverage honestly. Used by every research role (Bedrock, Azure, Google Cloud, LiteLLM, open-weight models, Radar editor).
---

# Daily deep research for the LLMOps portal

You run once a day for one scope. The portal is cumulative: today's job is to find what changed since the
records were last verified, prove it with quotes, and propose the smallest diff. The hard rules and the validator
in your system prompt always win over anything in this playbook.

## 1. Plan before you search (2-4 tool calls)

1. `lookup_portal("summary")`, then look up every record in your scope (the role skill says which files and
   which query). Note for each record: its current value, `lastVerified` / `asOf`, and the source ids it cites.
2. Build a worklist, highest value first:
   - **P0** - anything that can break a team that uses the model within 30 days: a retirement / shutdown /
     "no sooner than" date in the next 30 days, a price change, a security advisory, a removed region.
   - **P1** - records whose `lastVerified` is older than 7 days (rotate through them: a few per day, oldest
     first), new models or versions in your scope announced since the reporting period start.
   - **P2** - notes, benchmarks, wording.
3. Split the budget before you start: about 60% re-verifying tracked records (P0, then the oldest P1),
   30% discovery (what is new in the period), 10% kept back for fixing validator errors and resubmitting.

## 2. Source triage

Use pages in this order and stop climbing once a page of the tier you need answers the question:

1. The portal's own sources for the record (`lookup_portal` on data/sources.json). They are the pages the
   team already trusts; re-reading them is how a record is re-verified.
2. The provider's change feeds for the period: lifecycle / deprecation tables, release notes, "what's new",
   changelogs (your role skill lists their source ids).
3. `web_search` with narrow queries: exact model id or name + the fact, limited with `domains=` to the
   provider's documentation host and with `published_after=` to the reporting period start, for example
   `web_search("claude-sonnet-5-5 retirement", domains="learn.microsoft.com", published_after="2026-10-11")`.
   One query per question; reformulate once, then move on. On an index page (release notes, blog index) use
   `list_links(page_id, contains=...)` to find the entries of the period instead of searching for them.
4. Independent and community pages (press, benchmark sites, GitHub issues, forums, aggregators) are for
   DISCOVERY only: they tell you where to look. A value they state needs a quote from a page of the tier its
   place requires before it goes into a proposal; otherwise it belongs in `unverified` with the reason.

## 3. Reading and quoting

- `fetch_page`, then `find_in_page` with the model id / name, then `read_page` around the hit. Read the
  headings and the table head the row stands under: they decide whose value it is and whether it is a
  standard price or a Batch / Priority / Provisioned variant.
- One fact, one quote: the whole table row or the sentence that names this model next to the value.
  `record_evidence` returns what it stored - check it is the row you meant.
- Dates: write YYYY-MM-DD everywhere, also in zh-TW sentences. "No sooner than" is not a retirement date:
  keep the event kind the page uses.
- Two official pages disagree: keep the earliest current date in `date` and record the other in `conflicts[]`
  ("conflicting" for a current page, "superseded" for a replaced announcement).

## 4. Deciding what to propose

- Only real differences become ops. A value that matches the page needs no op - but list the check in
  `checked` ("Bedrock lifecycle table: Claude Sonnet 5.5 unchanged").
- Stamp only what you re-read: `lastVerified` = today on a record only when you re-read the pages it cites
  in this run; `asOfFiles` only when you re-read every source the file cites (rare).
- Prefer adding over rewriting: a new event, an appended note, a citation added at the end. Rewriting
  existing text, changing a status or removing anything waits for a person - do it only when the page leaves
  no doubt, and say why in the rationale.
- A new record needs every required field (`describe_schema`). If the page does not give enough, add a
  watchlist entry or an `unverified` line instead of guessing.

## 5. Coverage report (what a person reads first)

- `summary` (zh-TW, 2-6 sentences): what changed, what you could not confirm, what you did not get to.
- `checked`: every record or page you verified, with the result - "no change" is a result.
- `unverified`: claims you saw but could not back with a quote of the right tier, each with the reason and
  where you saw it.
- If the budget ran out before the worklist did, say which P0/P1 items were not reached, so tomorrow's run
  starts there.
- Before submitting, `review_work()` shows what you collected; use it to catch an op without evidence or a
  quote that names another model.

## 6. Stop rules

- Stop a line of inquiry after two searches that find nothing new.
- Stop the run once the P0 list is done, the oldest P1 records got their turn and discovery found nothing
  more in two consecutive searches - submit then, do not spend the rest of the budget for its own sake.
- After `submit_proposal` returns `accepted: true`, stop.
