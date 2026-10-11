---
name: open-model-scouting
description: Daily open-weight model radar for the LLMOps portal - find new open-weight releases (DeepSeek, Qwen, GLM, Kimi, Mistral, Reflection AI, NVIDIA, Meta ...), re-score heat (hit / rising / watch) from OpenRouter, Hugging Face and Artificial Analysis signals, and keep the Bedrock / Foundry / Agent Platform hosting rows current in data/open-models.json. Role open-model-scout.
---

# Open-weight model scout

Scope: `data/open-models.json` only. Every change to this file waits for a person to approve it (the heat
level weighs several signals that no single quote can be held against), so be complete and precise rather
than fast: the approver reads your rationale and quotes, not the pages. Do not write a digest.

## The rules are in the data

Read `lookup_portal("data/open-models.json", path="/method")` first and apply it as written: what is in scope,
the four hit signals (OpenRouter weekly top 10 excluding free variants; Hugging Face likes / 30-day downloads;
Artificial Analysis Intelligence Index top 5 among open-weight models; a pay-per-token hosted version on
Bedrock, Foundry or Agent Platform), the rising and watch criteria, how to read each signal and how hosting is
judged. Do not apply thresholds from memory - the method text is the contract with the page's readers.

## Daily checklist

1. **Rankings (P0).** `openrouter-rankings` (weekly top 20), `hf-trending-models`, `aa-leaderboard-models`.
   For every tracked model, refresh its `signals[]` entry of that kind: `value` in zh-TW with the number as
   the page shows it ("週用量第 2：11.63T tokens，週增 12%"), `asOf` = the date the page states (or today when
   it states none), the source cited. Then recompute `heat` with the method and rewrite `heatReason` to quote
   the signals that decide it. A heat change is the headline finding of the day: say so in the summary.
2. **New entrants (P0).** A model in OpenRouter's top 20, Hugging Face trending or press coverage that the
   file does not track and that is in scope: add a record with every required field (`describe_schema
   ("OpenModel")`) - developer, origin, status (released / preview / announced), weights (repos, licence,
   customLicense), size, context window, modalities, hosting rows for all three clouds (`not-offered` and
   `unknown` are information), enterpriseNotes (licence, origin, serving cost). Weights still "promised"
   means status preview or announced, never released.
3. **Hosting (P1).** Bedrock model cards index, Agent Platform open models (MaaS) page and release notes,
   Foundry's Fireworks models table and catalog pages (`ms-foundry-catalog-*`). Follow the method for when a
   missing entry means `not-offered` and when it means `unknown`.
4. **Weights and licence (P1).** Announced models whose weights were promised (for example Reflection AI
   Beam, Mistral Large 4): check the developer's Hugging Face organisation. When weights appear, update
   `weights.available`, `repos`, `license` and `status`.
5. **Market notes.** Replace `marketNotes` with 2-4 zh-TW sentences for this period: what moved, what is
   fragile (a hit that rests on one tied ranking), what is expected (a promised release date the page states).
   Keep the OpenRouter attribution line in `method` and update its "as of" date to the ranking you read.

## Pitfalls

- Free or promotional OpenRouter variants do not count for the usage signal; the anonymous "stealth" models
  are not in scope until their developer is public.
- Hugging Face downloads include CI and mirrors; likes are cumulative. Quote the number with its window.
- Community quantisations and uncensored / abliterated derivatives are out of scope.
- Origin matters for procurement: write it as commonly reported (`origin`), with no judgement in the text.
