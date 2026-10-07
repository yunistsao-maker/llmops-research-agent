# LLMOps Portal: research agent + update pipeline — project notes for AI assistants

Three parts that are deployed and tested separately. Read `README.md` (what is and is not verified, deployment),
`docs/architecture.md` (the data contract, the publish gates, the security model) and `docs/known-issues.md` (what four rounds of
independent review found, what was done about it, and the limits that remain) before changing anything.

| Part | Code | Tests |
| --- | --- | --- |
| Research agent (AgentCore Runtime) | `app/llmops_research/` (entrypoint `main.py`, logic in `research/`) | `cd app/llmops_research && uv run --extra dev python -m pytest tests` (`--live` adds real-web checks) |
| Update pipeline (Step Functions + 4 Lambdas) and operator CLI | `pipeline/portal_pipeline/`, `scripts/` | `uv run --with-requirements pipeline/requirements-dev.txt python -m pytest pipeline/tests` |
| Infrastructure | `infra/data-plane.yaml`, `infra/pipeline.yaml` | `uvx cfn-lint infra/*.yaml`; `pipeline/tests/test_infra.py`, `test_state_machine*.py` |
| UI integration kit | `ui/files/`, `scripts/patch_ui.py`, `scripts/serve_data.py`, `scripts/ui_smoke.py`, `scripts/review_proposal.html` | `pipeline/tests/test_patch_ui.py`, `test_serve_data.py`, `test_ui_kit.py` (runs `dataSource.ts` with node); with Playwright: `test_ui_smoke.py`, `test_review_viewer.py`; `ui_smoke.py` against a built UI |

Rules that are easy to break:

- The agent only *proposes*. Nothing but `pipeline/portal_pipeline/publish.py` (via the Publish Lambda or `scripts/portalctl.py`) writes what the UI
  reads, and it never trusts a proposal: it replays it on the live release and validates again. Do not add a path around it.
- The publisher's gates are derived from the **real difference** between the live data and the replayed result (`research/portal/diff.py`), never from
  how an operation is spelled or from what the record says about itself. Flags the agent writes into a proposal (`validation.flags`,
  `validation.asOfCoverage`, `validation.lastVerifiedCoverage`) may only make the publisher stricter. Keep it that way: a new gate reads `Review`, not `proposal["ops"]`.
- What publishes without a person (`PublishMode=auto`) is deliberately narrow: new content (a record, an appended note, an added citation) and a
  date, number or version that changed on an existing record - the kinds of value the agent's validator holds against ONE cited quote that is on the
  page, about the right model and of a sufficient tier (`research/proposal.py`, `research/validate/subjects.py`). Everything else about an existing
  record waits for a person (`publish._diff_holds`, fed by `research/portal/labels.py`). Do not widen this without a way to check the new kind of
  value against a quote, and keep the two sides telling the same story: the validator warns the model about everything the publisher will hold.
  "A date / number / version changed" is a property of the CHANGE (`labels.is_typed`): like for like - never of what the new value looks like.
- Whose value a quote states is answered with yes, no or UNSURE (`subjects.judge`). Unsure is not an error and not a pass: the proposal is accepted
  with `validation.flags.uncertainSubject`, and any flag makes the publisher hold. When a rule cannot tell, answer unsure - do not pick the reading
  that suits the proposal, and do not reject what may be honest. The same for a value nothing can be held against (`flags.uncheckedValue`).
  A new kind of flag needs: the validator's warning, a sentence in `publish._KNOWN_FLAGS`, a banner in `scripts/review_proposal.html`, the table in
  `docs/agent.md`. (An unknown kind already holds.)
- A quote is a whole unit of its page - a table row, a sentence, a heading (`research/validate/units.py`); `record_quote` widens what the model typed
  and stores that, with the headings and the table head it stands under. Rules about subjects read units, never fragments.
- A list of records takes no second record with a key it holds (`patch.record_key`), a new record in front of existing ones waits, and a `sources`
  list only grows at its end (`labels.recited`).
- Work is bounded before it is done: a proposal holds at most `apply.MAX_PROPOSAL_VALUES` values and `checks.validate_definition` lists the first
  `MAX_SCHEMA_PROBLEMS` schema errors (the rest are counted, with a fingerprint, so `new_problems` still sees every change). The Publish function
  has 1 GB; `test_publish_gates.py` measures the worst case inside the bounds.
- Web pages and model output are untrusted data. Do not weaken `research/web/safe_http.py`, the quote grounding in `research/evidence.py`, the
  validators in `research/validate/` and `research/proposal.py`, the protected fields in `research/portal/protected.py`, or the gates in `publish.py`
  (`refusals`, `holds` and the `_*_holds` functions); add a test with every change, and check that the test fails without the change.
- Text from elsewhere (model, web page, proposal field, error message) reaches a person only through `notify.clean` / `clean_block` / `ext`
  (notifications) or `portalctl`'s `c()`: no control or bidirectional characters, bounded length, and never at the start of a line of its own -
  what does not fit goes on in lines that begin with `┆`. The state that travels through the state machine is bounded too (`Outcome.to_state`).
- The step that reports failures must not fail: `handlers.report_failure` answers whatever the state holds, and when even that function cannot be
  called the state machine sends a fixed message built from its context object only (`NotifyUnreported`). Do not read the state there.
- `platformStatus`, facts labelled `internal-test`, the team's own test results (`litellm.status` `tested-*`, benchmarks `measuredBy: internal`, the
  identity of a record that carries an internal fact), `data/internal/` and `runs/` are never changed by the agent (enforced in code and, for reading,
  in the runtime's IAM policy).
- One request delivered twice has one effect and one answer: `publish_proposal`, `approve`, `discard`, `rollback` and `resume` take a `request_id`
  (a hash of the execution ARN) and a repeat gets what the first delivery got (`_published_before`, `answered`). A new action must do the same.
- `research/apply.py`, `research/portal/{checks,data,diff,labels,patch,protected}.py` and `research/validate/{injection,urls}.py` are shared: they are
  bundled into the Lambdas by `scripts/build_lambdas.py` (`SHARED`). They must stay importable without the agent's other dependencies; run both test suites after
  touching them.
- The data contract (`portal/manifest.json` -> immutable `portal/releases/<id>/`) is read by the UI (`ui/files/src/dataSource.ts`), the agent
  (`research/portal/data.py`) and the pipeline (`portal_pipeline/releases.py`). A change to it must be made in all three, additively.
- A release is immutable once written; every file is created with a create-only write and the pointer is replaced with an S3 conditional write.
  The pause flag lives in the pointer (`autoPublish`), so "is it paused?" and "switch the pointer" are one write. Never overwrite objects under `releases/`.
- The data location's URL contains the access token: keep it out of logs, notifications, command output and UI messages (`portalctl verify` prints `…`).
- The UI only sends simple GETs to the data location (no custom headers): the S3 origin does not answer CORS preflights. `scripts/ui_smoke.py` measures
  this on the data server's side, because a browser does not report preflights to the page.
- A page reads ONE release. With a data location `dataSource.ts` asks only for plain relative paths (`DATA_PATH`, the same pattern as `storage._KEY` - a test compares
  them): part of a path can come from the address bar. `scripts/patch_ui.py --check` is a text search for the spellings it lists; the proof that no
  page reads data around the data location is `scripts/ui_smoke.py`, which watches the requests and the text on the screen.
- `scripts/portalctl.py` acts directly on a data location named on the command line (or a local folder) and goes through the state machine only when
  both the data location and the state machine come from the environment (or `--state-machine` is given). Do not make a local command reach production.
  Every other variable that describes "the deployment" (`PIPELINE_STATE_URI`, `PIPELINE_RESEARCH_URI`) is applied only when the data location came
  from the environment too. `pull` writes everything or nothing.
- `scripts/patch_ui.py` edits are invertible: each one records what it replaced in `<web>/.patch_ui/state.json`. A new edit needs `applied`, `apply`
  (returning the inserted/original pairs) and a manual instruction; never restore a file from a snapshot.
- The publisher refuses a proposal whose evidence records carry a field it does not know (`publish._EVIDENCE_KEYS`): they are printed and stored for
  the person who approves. A new field in the agent's evidence record (`proposal._normalise`) therefore goes to the pipeline first - publisher, notify,
  viewer - and the Lambda bundle is deployed before the agent that writes it.
- `pipeline/tests/fixtures/accepted_run.json` is a real run record. Regenerate it with `pipeline/tests/fixtures/make_fixtures.py` after changing
  the bundled snapshot, the fixture portal files or the proposal format - do not edit it by hand. Its proposal id is spelled out in the pipeline tests
  (`conftest.PROPOSAL_ID` notices when it changed).
- Never run two test runs (or a test run and a script) at the same time while one of them rewrites source files (mutation checks do).
- `agentcore dev` reads env vars from `agentcore/.env.local`, not from the `envVars` in `agentcore.json`. Use `@aws/agentcore`
  (`npm i -g @aws/agentcore`), not the legacy Python starter-toolkit `agentcore`. The runtime's account-specific settings are written by
  `scripts/configure_agent.py`; the delivered `agentcore.json` contains none.
- User-facing text (CLI output, notifications, docs) is Traditional Chinese with English technical terms; code comments and identifiers are English.
- Generic AgentCore CLI context follows.

---

# AgentCore Project

This project contains configuration and infrastructure for an Amazon Bedrock AgentCore application.

The `agentcore/` directory is a declarative model of the project. The `agentcore/cdk/` subdirectory uses the
`@aws/agentcore-cdk` L3 constructs to deploy the configuration to AWS.

## Mental Model

The project uses a **flat resource model**. Agents, memories, credentials, gateways, evaluators, and policies are
independent top-level arrays in `agentcore.json`. There is no binding between resources in the schema — each resource is
provisioned independently. Agents discover memories and credentials at runtime via environment variables or SDK calls.
Tags defined in `agentcore.json` flow through to deployed CloudFormation resources.

## Critical Invariants

1. **Schema-First Authority:** The `.json` files are the source of truth. Do not modify agent behavior by editing
   generated CDK code in `cdk/`.
2. **Resource Identity:** The `name` field determines the CloudFormation Logical ID.
   - **Renaming** a resource will **destroy and recreate** it.
   - **Modifying** other fields will update the resource **in-place**.
3. **Schema Validation:** If your JSON conforms to the types in `.llm-context/`, it will deploy successfully. Run
   `agentcore validate` to check.
4. **Resource Removal:** Use `agentcore remove` to remove resources. Run `agentcore deploy` after removal to tear down
   deployed infrastructure.
5. **Invocation Input:** Validate runtime payloads and require text prompts to be strings. If a Strands app accepts a
   caller-supplied message history, normalize the history tail with `strip_trailing_tool_use()` before invocation.

## Directory Structure

```
myProject/
├── AGENTS.md               # This file — AI coding assistant context
├── agentcore/
│   ├── agentcore.json      # Main project config (AgentCoreProjectSpec)
│   ├── aws-targets.json    # Deployment targets (account + region)
│   ├── .env.local          # Secrets — API keys (gitignored)
│   ├── .llm-context/       # TypeScript type definitions for AI assistants
│   │   ├── README.md       # Guide to using schema files
│   │   ├── agentcore.ts    # AgentCoreProjectSpec types
│   │   └── aws-targets.ts  # AWS deployment target types
│   └── cdk/                # AWS CDK project (@aws/agentcore-cdk L3 constructs)
├── app/                    # Agent application code
└── evaluators/             # Custom evaluator code (if any)
```

## Schema Reference

The `agentcore/.llm-context/` directory contains TypeScript type definitions optimized for AI coding assistants. Each
file maps to a JSON config file and includes validation constraints as comments (`@regex`, `@min`, `@max`).

| JSON Config | Schema File | Root Type |
| --- | --- | --- |
| `agentcore/agentcore.json` | `agentcore/.llm-context/agentcore.ts` | `AgentCoreProjectSpec` |
| `agentcore/aws-targets.json` | `agentcore/.llm-context/aws-targets.ts` | `AwsDeploymentTarget[]` |

### Key Types

- **AgentCoreProjectSpec**: Root config with runtimes, memories, knowledge bases, credentials, evaluators, online evals and insights, gateways, policy engines, config bundles, A/B tests, harness registrations, datasets, payment managers, and capacity providers
- **AgentEnvSpec**: Agent configuration (build type, entrypoint, code location, runtime version, network mode)
- **Memory**: Memory resource with strategies (SEMANTIC, SUMMARIZATION, USER_PREFERENCE, EPISODIC) and expiry
- **Credential**: API key or OAuth credential provider
- **AgentCoreGateway**: MCP gateway with targets (Lambda, MCP server, OpenAPI, Smithy, API Gateway, web-search, knowledge-base)
- **Evaluator**: LLM-as-a-Judge or code-based evaluator
- **OnlineEvalConfig**: Continuous evaluation pipeline bound to an agent
- **OnlineInsightsConfig** _[preview]_: Continuous failure-pattern analysis bound to an agent
- **KnowledgeBase**: Managed Bedrock Knowledge Base auto-wired to a gateway
- **Harness**: Declarative agent — runtime + tools + skills + memory + observability without writing agent code
- **PolicyEngine** + **Policy**: Cedar policy engine with form-based guardrails (Bedrock content filters, prompt-attack, sensitive-info) or raw Cedar policies
- **PaymentManager** + **PaymentConnector**: x402-protocol payment orchestration with provider credentials (CoinbaseCDP, StripePrivy)
- **ConfigBundle**: Versioned runtime configuration as a separately-deployable resource
- **Dataset**: Curated session dataset for batch evaluation and recommendation runs
- **RuntimeEndpoint**: Named endpoint (e.g. `PROMPT_V1`) targeting a specific runtime version

### Common Enum Values

- **BuildType**: `'CodeZip'` | `'Container'`
- **NetworkMode**: `'PUBLIC'` | `'VPC'`
- **RuntimeVersion**: `'PYTHON_3_10'` | `'PYTHON_3_11'` | `'PYTHON_3_12'` | `'PYTHON_3_13'` | `'PYTHON_3_14'` | `'NODE_18'` | `'NODE_20'` | `'NODE_22'`
- **MemoryStrategyType**: `'SEMANTIC'` | `'SUMMARIZATION'` | `'USER_PREFERENCE'` | `'EPISODIC'`
- **GatewayTargetType**: `'lambda'` | `'mcpServer'` | `'openApiSchema'` | `'smithyModel'` | `'apiGateway'` | `'lambdaFunctionArn'` | `'connector'` (web-search, bedrock-knowledge-bases)
- **ModelProvider**: `'Bedrock'` | `'Gemini'` | `'OpenAI'` | `'Anthropic'`
- **PaymentProvider**: `'CoinbaseCDP'` | `'StripePrivy'`
- **PolicyEnforcementMode**: `'ACTIVE'` | `'PASSIVE'`
- **GuardrailContentFilter**: `'VIOLENCE'` | `'HATE'` | `'SEXUAL'` | `'MISCONDUCT'` | `'INSULTS'`

### Build Types

- **CodeZip**: Python source packaged as a zip and deployed directly to AgentCore Runtime.
- **Container**: Docker image built in CodeBuild (ARM64), pushed to a per-agent ECR repository. Requires a `Dockerfile`
  in the agent's `codeLocation` directory. For local development (`agentcore dev`), the container is built and run
  locally with volume-mounted hot-reload.

### Supported Frameworks (for template agents)

- **Strands** — Bedrock, Anthropic, OpenAI, Gemini
- **LangChain/LangGraph** — Bedrock, Anthropic, OpenAI, Gemini
- **GoogleADK** — Gemini
- **OpenAI Agents** — OpenAI
- **Autogen** — Bedrock, Anthropic, OpenAI, Gemini

### Protocols

- **HTTP** — Standard HTTP agent endpoint
- **MCP** — Model Context Protocol server
- **A2A** — Agent-to-Agent protocol (Google A2A)

## Deployment

Deployments are orchestrated through the CLI:

```bash
agentcore deploy    # Synthesizes CDK and deploys to AWS
agentcore status    # Shows deployment status
```

Alternatively, deploy directly via CDK:

```bash
cd agentcore/cdk
npm install
npx cdk synth
npx cdk deploy
```

## Editing Schemas

When modifying JSON config files:

1. Read the corresponding `agentcore/.llm-context/*.ts` file for type definitions
2. Check validation constraint comments (`@regex`, `@min`, `@max`)
3. Use exact enum values as string literals
4. Use CloudFormation-safe names (alphanumeric, start with letter)
5. Run `agentcore validate` to verify changes

## Harness Export

`agentcore export harness` converts a harness configuration into a deployable Strands Python agent under `app/<agentName>/`.

**After every export, you MUST read `app/<agentName>/EXPORT_NOTES.md` before proceeding.**

This file lists any manual follow-up items required before the agent will deploy or run correctly — missing files to create, IAM policies to add, or configuration steps the exporter could not automate. A clean export produces "No manual steps required." Complete every item in the file before running `agentcore deploy`.

```bash
agentcore export harness --name <harnessName>   # generates app/<agentName>/EXPORT_NOTES.md
cat app/<agentName>/EXPORT_NOTES.md             # read this before touching anything else
```

## CLI Commands

Run `agentcore --help` or `agentcore <command> --help` for full flags. Commonly used:

**Project lifecycle**

| Command | Description |
| --- | --- |
| `agentcore create` | Create a new project |
| `agentcore dev` | Run agent locally with hot-reload |
| `agentcore deploy` | Deploy to AWS |
| `agentcore invoke` | Invoke agent (local or deployed) |
| `agentcore capacity-provider delete-session` | Delete (deprovision) a live capacity provider session |
| `agentcore status` | Show deployment status |
| `agentcore validate` | Validate configuration |
| `agentcore package` | Package agent artifacts |
| `agentcore import` | Import resources from a Bedrock AgentCore Starter Toolkit project |

**Resources**

| Command | Description |
| --- | --- |
| `agentcore add <resource>` | Add agent, memory, credential, gateway, gateway-target, evaluator, online-eval, online-insights, knowledge-base, harness, policy-engine, policy, payment-manager, payment-connector, capacity-provider, config-bundle, dataset, runtime-endpoint |
| `agentcore remove <resource>` | Remove any resource |
| `agentcore export harness` | Export a harness to a Strands runtime agent under `app/<agentName>/` |

**Jobs (run, view, archive, lifecycle)**

| Command | Description |
| --- | --- |
| `agentcore run eval` | Run on-demand evaluation against agent traces |
| `agentcore run batch-evaluation` | Run evaluators across all sessions at scale |
| `agentcore run recommendation` | Optimize prompts or tool descriptions from real traces |
| `agentcore run insights` _[preview]_ | Run failure-pattern analysis across sessions |
| `agentcore run ab-test` | Start an A/B test (config-bundle or target-based) |
| `agentcore run ingest` | Start a fresh ingestion job for every data source on a deployed knowledge base |
| `agentcore view <type>` | List or view jobs (recommendation, batch-evaluation, ab-test, insights) |
| `agentcore archive <type>` | Delete a job on the service + clear local history |
| `agentcore stop <type>` | Stop a running batch-evaluation or ab-test |
| `agentcore promote ab-test` | Apply the winning variant to `agentcore.json` |
| `agentcore pause <type>` / `agentcore resume <type>` | Pause/resume a deployed online-eval, online-insights, or ab-test |

**Config bundles & datasets**

| Command | Description |
| --- | --- |
| `agentcore config-bundle versions` (alias `cb versions`) | List version history for a bundle |
| `agentcore config-bundle diff` | Diff two versions of a bundle |
| `agentcore config-bundle create-branch` | Create a new branch on an existing bundle |
| `agentcore dataset download` | Download a dataset version locally |
| `agentcore dataset publish-version` | Publish a new dataset version |
| `agentcore dataset remove-version` | Remove a dataset version |

**Observability & history**

| Command | Description |
| --- | --- |
| `agentcore logs` | Stream/search agent runtime logs |
| `agentcore logs evals` | Stream/search online-eval logs |
| `agentcore traces list` / `agentcore traces get` | List recent traces or download one to JSON |
| `agentcore evals history` | View past on-demand eval results |

**Utilities**

| Command | Description |
| --- | --- |
| `agentcore fetch access` | Fetch access info for deployed gateway or agent |
| `agentcore feedback` | Send feedback (with optional screenshot) to the AgentCore team |
| `agentcore update` | Check for and install CLI updates |
| `agentcore telemetry` | View or change telemetry preferences |
