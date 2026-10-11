---
name: google-cloud-model-research
description: Daily Google Cloud scope for the LLMOps portal - Gemini Enterprise Agent Platform (Vertex AI) model versions and retirement / no-sooner-than dates, partner models (Claude, open models as a service), quotas, regions and pricing for every catalog model with a Google Cloud offering. Role gcp-researcher.
---

# Google Cloud researcher

Scope: Google Cloud only (`cloud: "gcp"`, `service: "agent-platform"` - Gemini Enterprise Agent Platform,
formerly Vertex AI). The Gemini Developer API (`service: "gemini-api"`, ai.google.dev) belongs to the vendor
API researcher, not to you. Do not write a digest.

## Records in scope

- `data/catalog.json`: offerings with `cloud: "gcp"` (query "agent-platform"): `status`, `availableSince`,
  `providerModelId` / publisher model path, regions (global vs regional endpoints), quotas, `pricing[]`.
- `data/lifecycle.json`: events with `service: "agent-platform"` - many are `eol-no-sooner-than`.
- Deep dives: the Google Cloud rows of `availabilityMatrix` / `onboarding`.

## Sources (portal ids)

| What | Source ids |
| --- | --- |
| Model versions and lifecycle (release, retirement, "no sooner than") | `google-agentplatform-model-versions`, `google-vertex-model-versions` |
| Release notes (discovery and dated changes) | `google-agentplatform-release-notes`, `google-vertex-release-notes`, `google-cloud-release-notes` |
| Partner models (Claude) and their quotas | `google-agentplatform-opus55`, `google-agentplatform-sonnet55`, `agentplatform-claude-haiku55`, `google-agentplatform-claude-quotas` |
| Open models and OpenAI models as a service | `google-agentplatform-open-models`, `google-agentplatform-openai` |
| Pricing | `google-agentplatform-pricing` |

## Daily checklist

1. **Model versions page first (P0).** For each portal event, the row of the same model version: release
   date, "discontinuation date" / retirement, and whether the page says "no sooner than". Google often moves
   a retirement date or replaces a date with "no sooner than": the event kind follows the page. Pages under
   the old Vertex AI path and the new Agent Platform path can disagree for a while - earliest current
   official date in `date`, the other in `conflicts[]`.
2. **Partner models (P1).** Claude on Agent Platform: model ids with the `@` version suffix, regions (global
   endpoint vs regional), quotas per region. Anthropic's own "Claude on Vertex AI" page is the vendor's view;
   the Google page is the platform's - cite the platform page for platform facts.
3. **Pricing (P1).** Standard pay-as-you-go rows only; batch, provisioned throughput and long-context
   (> 200K) prices are variants.
4. **Discovery.** Release notes entries in the period (`list_links` on the release notes page), new partner
   or open models on Model Garden that the team's catalog tracks.

## Pitfalls

- "Gemini 3.8 Flash" vs "Gemini 3.8 Flash-Lite" vs "Gemini 3.8 Flash Image": different models.
- A retirement date for the Gemini API (ai.google.dev) is not a Google Cloud date - leave it to the vendor
  API researcher (report it in `checked` if you see it).
- Forum posts and aggregators (zentor, releasebot, digitalapplied) are discovery only.
