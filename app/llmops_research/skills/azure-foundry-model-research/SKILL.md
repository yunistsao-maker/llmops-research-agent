---
name: azure-foundry-model-research
description: Daily Microsoft Foundry (Azure OpenAI and Foundry Models) scope for the LLMOps portal - retirement schedule, models sold directly by Azure, Claude on Foundry, deployment types, regions and pricing for every catalog model with an Azure offering. Role azure-researcher.
---

# Microsoft Foundry researcher

Scope: Microsoft Azure only (`cloud: "azure"`, `service: "foundry"`): Azure OpenAI in Foundry, Foundry Models
sold directly by Azure, and partner models hosted on Foundry (Claude). Do not write a digest.

## Records in scope

- `data/catalog.json`: offerings with `cloud: "azure"` (query "foundry"): `status`, `availableSince`,
  `providerModelId` (the deployment model name), `inferenceTargets[]` (deployment types: Global Standard,
  Data Zone Standard, Standard / regional, Provisioned), `pricing[]`, endpoint notes.
- `data/lifecycle.json`: events with `service: "foundry"` (query "foundry") - the largest group in the portal.
- Deep dives: the Foundry rows of `availabilityMatrix` / `onboarding`.

## Sources (portal ids)

| What | Source ids |
| --- | --- |
| Retirement schedule (legacy, deprecation, retirement dates per model version) | `ms-foundry-retirement-schedule` |
| Models sold directly by Azure, region availability | `ms-foundry-models-sold-by-azure`, `ms-foundry-region-availability` |
| Claude on Foundry | `ms-foundry-claude-models`, `ms-foundry-claude-hosting`, `ms-foundry-claude-howto` |
| Deployment types, endpoints, API versions | `ms-foundry-deployment-types`, `ms-foundry-endpoints`, `ms-azure-openai-v1-api` |
| Announcements (discovery) | `ms-foundry-*-blog`, `ms-techcommunity-*`, `ms-techcommunity-foundry-blog-index` |
| Catalog entries (partner / Fireworks-hosted models) | `ms-foundry-catalog-*`, `ms-foundry-fireworks-models` |

## Daily checklist

1. **Retirement schedule first (P0).** The page lists each model AND version (for example a dated snapshot)
   with deprecation / retirement dates, sometimes separately for Standard and Provisioned or Global and
   regional deployments. Match the portal event by model name and version; a different version is a different
   event. When the page now says "no earlier than" or moved a date, change the date (typed change) and record
   the previous date in `conflicts[]` as "superseded" when the page states it.
2. **Catalog offerings (P1, oldest first).** Region availability per deployment type, model version that a
   new deployment gets, status (preview / GA). Claude on Foundry has its own hosting and region rules
   (`ms-foundry-claude-hosting`) - keep them separate from Azure OpenAI rules.
3. **Pricing (P1).** Global, Data Zone and regional prices differ; Batch and Provisioned are variants. Quote
   the row of the deployment type the portal's price row names.
4. **Discovery.** The Foundry blog index and the Tech Community blog for models added in the period; the
   retirement schedule for newly scheduled retirements of models the team uses.

## Pitfalls

- "Azure OpenAI" product pages and "Foundry" pages sometimes show different dates for the same model: keep
  the earliest current official date in `date` and the other in `conflicts[]`.
- Microsoft Q&A answers and community trackers (foundrymap, vorplabs) are discovery, never the value source.
- A model name inside a longer one ("GPT-6 Astra Mini" vs "GPT-6 Astra") is another model.
