---
name: vendor-api-model-research
description: Daily vendor-direct API scope for the LLMOps portal - Anthropic API, OpenAI API and Gemini Developer API deprecations, model pages and pricing for models the team calls without a cloud in between, plus each vendor's new releases (which usually reach the clouds days later). Role vendor-api-researcher.
---

# Vendor API researcher

Scope: the vendors' own APIs (`cloud: "vendor-direct"`; lifecycle `service` values `anthropic-api`,
`openai-api`, `gemini-api`). New models appear here first, so you are also the early warning for the cloud
researchers: report a vendor release in `checked` even when no cloud offers it yet. Do not write a digest.

## Records in scope

- `data/lifecycle.json`: events with `service` `anthropic-api`, `openai-api` or `gemini-api`.
- `data/catalog.json`: model-level facts that come from the vendor (context window, max output, release date,
  capabilities) - the vendor's page is the official source for what a model IS, whatever cloud serves it.
- `data/catalog.json` `watchlist`: announced but not yet available models.

## Sources (portal ids)

| Vendor | Deprecations | Models / release | Pricing | Change feed |
| --- | --- | --- | --- | --- |
| Anthropic | `anthropic-deprecations` | `anthropic-models-overview`, `anthropic-*-overview`, `anthropic-*-whatsnew` | `anthropic-pricing` | `anthropic-release-notes` |
| OpenAI | `openai-deprecations` | `openai-models`, `openai-*-model` | `openai-pricing` | `openai-changelog`, `openai-news` |
| Google (Gemini API) | `google-gemini-api-deprecations` | `google-gemini-api-models`, `google-gemini38-model` | `google-gemini-api-pricing` | `google-gemini-api-changelog` |

## Daily checklist

1. **Deprecation pages (P0).** Each vendor keeps one table of deprecated models with shutdown / retirement
   dates. Compare every vendor-direct event; add events for models the catalog tracks that were newly
   scheduled. A vendor's date is NOT a cloud's date - never copy it into a Bedrock / Foundry / Agent Platform
   event (the validator holds a page of another platform).
2. **Change feeds for the period (P1).** Release notes / changelog entries since the period start: new models,
   new snapshots, price changes, API behaviour changes that affect the gateway (parameters removed, new
   required headers). Quote the entry under its date heading.
3. **Model facts (P1, oldest `lastVerified` first).** Context window, max output tokens, knowledge cutoff,
   release date - from the vendor's model page.
4. **Watchlist.** An announced model that is now available: report it; add a catalog model only when the
   vendor page fills every required field.

## Pitfalls

- Marketing posts announce "coming soon"; the model page or changelog says when it is actually available.
- An X / social post by a vendor employee is community tier at best - discovery only.
- Snapshot ids (`-2026-09-22`) and aliases (`-latest`) are different records; keep the one the portal tracks.
