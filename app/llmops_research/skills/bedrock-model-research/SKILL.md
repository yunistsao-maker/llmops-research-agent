---
name: bedrock-model-research
description: Daily Amazon Bedrock scope for the LLMOps portal - lifecycle (active / legacy / EOL), model cards, inference profiles and regions, and pricing for every catalog model with an AWS offering, plus models newly available on Bedrock. Role bedrock-researcher.
---

# Amazon Bedrock researcher

Scope: Amazon Bedrock only (`cloud: "aws"`, `service: "bedrock"`). Do not write a digest - the Radar editor
writes it later today from what you publish and report.

## Records in scope

- `data/catalog.json`: offerings with `cloud: "aws"` (`lookup_portal("data/catalog.json", query="bedrock")`):
  `status`, `availableSince`, `providerModelId`, `inferenceTargets[]` (scope `in-region` / `geo` / `global`
  with the profile id such as `us.anthropic.claude-sonnet-5-5`), `endpoints[]`, `pricing[]`.
- `data/lifecycle.json`: events with `service: "bedrock"` (query "bedrock"): `event` (retirement,
  eol-no-sooner-than, legacy, extended-access, deprecation), `date`, `scopeNote` (which regions), `conflicts[]`.
- `data/models/<id>.json` deep dives: the `availabilityMatrix` / `onboarding` parts about Bedrock.

## Sources (portal ids - look them up in data/sources.json for the URL)

| What | Source ids |
| --- | --- |
| Lifecycle, active and legacy tables | `aws-bedrock-lifecycle`, `aws-bedrock-lifecycle-legacy` |
| Model cards (one page per model: ids, regions, profiles, features) | `aws-bedrock-*-card`, `aws-bedrock-openai-cards` |
| Endpoints and APIs | `aws-bedrock-endpoints`, `aws-bedrock-responses-api` |
| What's New announcements | `aws-*-whatsnew` (discover new ones on the What's New feed for Bedrock) |
| Pricing | the Bedrock pricing page (add it as a new source when you fetch it and the portal has none) |

## Daily checklist

1. **Lifecycle tables first (P0).** Read both lifecycle pages. For every Bedrock event in the portal, compare
   the row of the same `providerModelId` and region scope: legacy date, EOL date, "no sooner than" wording,
   extended access. A model that moved from the active to the legacy table is a new `legacy` event, not an
   edit of the retirement event. Retirement dates often differ by region group - keep `scopeNote` exact and
   propose a separate event when the page gives a separate date.
2. **Models in the catalog (P1, oldest `lastVerified` first).** Open the model card: model id, the inference
   profile ids and their geographies (`global.` has no data-residency guarantee - keep that note), endpoint /
   API support (Converse, InvokeModel, Anthropic Messages, OpenAI-compatible), status (GA / preview).
3. **Pricing (P1).** Only the standard on-demand table counts for a standard price row. Batch, Priority,
   Flex, Provisioned Throughput and long-context tiers are variants: never copy them into a standard row; the
   validator holds a price that stands under such a heading.
4. **Discovery.** New models on Bedrock since the period start (What's New, the model cards index, the
   lifecycle "active" table). A model the catalog does not have becomes a new offering only when the card gives
   every required field; otherwise add a watchlist entry or an `unverified` line.

## Pitfalls

- A model card for "Claude Sonnet 5" is not evidence for "Claude Sonnet 5.5"; check the exact id on the row.
- Region lists change without an announcement: when a card lists new regions, update `inferenceTargets`
  or `scopeNote`, citing the card.
- An AWS ML blog or a re:Post answer is discovery: confirm it on the documentation page.
- `platformStatus` is the platform team's decision - never change it, even when AWS retires the model.
