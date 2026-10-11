---
name: litellm-release-research
description: Daily LiteLLM scope for the LLMOps portal - new releases (stable, nightly, rc), security advisories and CVEs, avoid-lists and supported lines in data/litellm.json, and the LiteLLM support of catalog models. Role litellm-researcher.
---

# LiteLLM researcher

Scope: LiteLLM, the team's gateway. `data/litellm.json` (releases, recommended, minimumBaseline,
supportedLines, avoid, advisories, pitfalls) and the `litellm` block of catalog models. Do not write a digest.

## Never touch

`status` values `tested-*`, benchmarks `measuredBy: "internal"` and anything labelled `internal-test`: they are
the team's own test results. You may report that a newer release exists; whether the team moves to it is
their decision (`recommended` changes wait for a person anyway when they are not a plain version change).

## Sources (portal ids)

| What | Source ids |
| --- | --- |
| Release list with dates | `pypi-litellm-json` (machine-readable: every version and its upload time), `pypi-litellm-rss` |
| Release notes | `litellm-release-notes`, `litellm-v*` pages, GitHub releases `litellm-v*-release` |
| Support policy | `litellm-version-support` |
| Advisories | `litellm-security-advisories` (GitHub), `osv-litellm-list`, `gitlab-litellm-list`, `osv-api-litellm` |
| Model support | `litellm-*` blog posts per model, `litellm-cost-map-main` (the bundled cost map) |

## Daily checklist

1. **New releases (P0/P1).** From PyPI JSON: every version uploaded since the newest release in the portal.
   Quote the row with the version and its date on the same line. Classify by the version string as the portal
   does: stable `v1.104.2`, release candidates `-rc.N`, nightly `-nightly`; a version is compared whole.
2. **Advisories (P0).** New GHSA / CVE entries for litellm: file them under the id the advisory gives
   (`CVE-...`, `GHSA-...`), with the affected range and the fixed version exactly as the advisory names them.
   An advisory whose affected range covers `recommended` or `minimumBaseline` is the most important finding of
   the day - say so in the summary.
3. **Avoid list and pitfalls (P1).** Release notes or the project's own incident posts that name broken
   versions (memory leaks, regressions): add to `avoid[]` with versions written as versions or ranges
   ("v1.81.16 – v1.83.6", "< v1.84.0"). A GitHub issue alone is community tier: report it in `unverified`.
4. **Model support (P1).** For new catalog models: the oldest LiteLLM version that routes the model and the
   oldest image with it in the bundled cost map, from the LiteLLM blog or release notes.

## Pitfalls

- "Python 3.9", "boto3>=1.40.0", "litellm-proxy-extras 0.2.1" are not LiteLLM versions.
- PyPI upload time is UTC; write the date the page shows, in full.
- Release notes pages are sometimes published days after the PyPI upload: the PyPI row is the release date.
