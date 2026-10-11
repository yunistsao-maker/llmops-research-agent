# 搬到公司專用帳號／sandbox 帳號

目標：research agent、pipeline、資料位置與 portal UI **全部跑在一個專用的 AWS 帳號**（公司專用帳號或 sandbox），
個人帳號裡的舊部署在新環境驗收後下架。這份文件只列指令；**每一步都由妳自己執行**，刪除類的步驟（第 6 節）一定要先看過盤點結果再做。

順序一覽：

| # | 做什麼 | 在哪個帳號 | 會不會刪東西 |
| --- | --- | --- | --- |
| 0 | 向公司／IT 申請帳號與權限 | — | 否 |
| 1 | 設定 profile 與帳號防呆 | 專用 | 否 |
| 2 | 盤點並**暫停**個人帳號的排程、封存資料 | 個人 | 否（停用排程可以再打開） |
| 3 | data plane → 第一個 release → UI → agent → pipeline → 排程（停用）→ 觸發 → ui-verify | 專用 | 否 |
| 4 | 手動跑一次、核准、確認通知與網站 | 專用 | 否 |
| 5 | 打開專用帳號的每日排程 | 專用 | 否 |
| 6 | 下架個人帳號的資源 | 個人 | **是**（逐項確認） |

---

## 0. 要先向公司／IT 確認的事

| 項目 | 為什麼需要 |
| --- | --- |
| 帳號 id（12 碼）與 region | AgentCore Runtime 與 Gateway 的 web-search connector 要在同一個 region 可用（目前用 `us-east-1`；`ap-northeast-1` 也可） |
| IAM Identity Center（SSO）permission set | 要能建立 CloudFormation stack、IAM role／policy、S3、CloudFront、Lambda、Step Functions、EventBridge（rule、API destination、Scheduler）、SNS、CloudWatch alarm、Amplify、Bedrock AgentCore |
| Bedrock 模型存取 | agent 用 `global.anthropic.claude-sonnet-5-5`（global cross-region inference）。公司 SCP 若限制 region，`global.` 可能被擋，要改成該區的 profile（例如 `us.`／`apac.`），並改 `agentcore/agentcore.json` 的 `MODEL_ID` |
| Permissions boundary | 公司帳號常要求新 role 附加 boundary：每個 template 都有 `PermissionsBoundaryArn` 參數（data plane 不建 role） |
| SNS e-mail 訂閱 | 通知寄到公司信箱；有些公司會擋外部服務寄信，先確認 |
| Amplify 連 GitHub | 兩個 repo 目前在個人 GitHub（`yunistsao-maker`）。公司帳號的 Amplify 要安裝 Amplify GitHub App 到這個帳號；**公司若要求程式放在公司的 GitHub organization，先 transfer repo 再做第 3.3 步** |
| Budgets／成本歸屬 | 建議設 AWS Budgets 告警（例如每月 USD 100）；公司若要求 cost allocation tag，用 `--tags` 加在每個 `cloudformation deploy` |

---

## 1. profile 與帳號防呆

```bash
aws configure sso --profile llmops-sandbox          # 照公司給的 SSO start URL、帳號、permission set 填
aws sso login --profile llmops-sandbox

cd llmops-research-agent
export LLMOPS_AWS_PROFILE=llmops-sandbox
export LLMOPS_ACCOUNT_ID=<專用帳號 id>
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
scripts/in_account.sh aws sts get-caller-identity    # 第一行印出 in_account: account <專用帳號 id>
```

**之後所有對專用帳號的指令都經過 `scripts/in_account.sh`**。它會：

- 只用 `LLMOPS_AWS_PROFILE` 的憑證，並清掉 shell 裡其他的 `AWS_ACCESS_KEY_ID`／`AWS_SESSION_TOKEN`／`AWS_DEFAULT_PROFILE`（個人帳號的 key 不會混進來）；
- 確認這個 profile 的帳號就是 `LLMOPS_ACCOUNT_ID`，不是就什麼都不執行；
- 確認 `agentcore/aws-targets.json`（有的話）只指向這個帳號；
- 拒絕指令裡自己帶 `--profile`。

方便起見：

```bash
acct()      { scripts/in_account.sh "$@"; }
portalctl() { scripts/in_account.sh uv run --with-requirements scripts/requirements.txt python scripts/portalctl.py "$@"; }
```

---

## 2. 個人帳號：先盤點、暫停、封存（不刪除）

個人帳號的指令**刻意不經過** `in_account.sh`，一律明確寫 `--profile <個人 profile>`，避免和專用帳號搞混。

```bash
export ME=<個人帳號的 profile 名稱>
R=us-east-1
aws sts get-caller-identity --profile $ME
aws cloudformation list-stacks --profile $ME --region $R --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE \
    --query "StackSummaries[?contains(StackName,'llmops')].[StackName,LastUpdatedTime]" --output table
aws scheduler list-schedules --profile $ME --region $R --query "Schedules[].[GroupName,Name,State]" --output table
aws events list-rules --profile $ME --region $R --query "Rules[].[Name,State]" --output table
aws amplify list-apps --profile $ME --region $R --query "apps[].[appId,name,defaultDomain]" --output table
aws bedrock-agentcore-control list-agent-runtimes --profile $ME --region $R --output table
aws bedrock-agentcore-control list-gateways --profile $ME --region $R --output table
aws s3 ls --profile $ME | grep -i llmops
```

把輸出貼回來（不要包含 token 或 key）就能對照第 6 節列出要下架的清單。

**暫停**（可以再打開，不刪任何東西）：盤點裡 `State=ENABLED` 的 llmops 排程逐一停用，自動研究就不會再在個人帳號產生費用：

```bash
# Scheduler 的排程（每日的五個、ui-verify 的 deadline）：
aws scheduler get-schedule --profile $ME --region $R --name <Name> --group-name <GroupName> > s.json   # 先存一份
# 用 console 停用最簡單：EventBridge → Schedules → 選取 → Disable
# pipeline stack 自己的每週排程：
aws cloudformation deploy --profile $ME --region $R --stack-name llmops-portal-pipeline --template-file infra/pipeline.yaml \
    --capabilities CAPABILITY_IAM --parameter-overrides ScheduleState=DISABLED
```

**封存**資料（只讀）：

```bash
mkdir -p ~/llmops-archive/$(date +%F)
aws s3 sync s3://<舊的 DataBucketName>/ ~/llmops-archive/$(date +%F)/ --profile $ME \
    --exclude "artifacts/*"                 # Lambda bundle 不需要
```

`portal/` 是所有 release 與 pointer，`research/` 是研究紀錄，`pipeline/` 是提案與狀態。新的帳號不需要它們也能運作（第 3.2 步從 portal repo 重新 bootstrap），封存是為了日後查歷史。

---

## 3. 專用帳號：部署

用**新 clone 的資料夾**部署，不要用原本部署個人帳號的資料夾：那裡的 `agentcore/.cli/deployed-state.json` 記錄的是個人帳號的 runtime。

```bash
git clone https://github.com/yunistsao-maker/llmops-research-agent.git ~/work/llmops-agent-sandbox
git clone https://github.com/yunistsao-maker/llmops-portal.git ~/work/llmops-portal
cd ~/work/llmops-agent-sandbox
```

### 3.1 data plane

```bash
openssl rand -hex 16          # 新的 DataAccessToken：存在密碼管理器，不要貼到聊天、issue 或 commit
acct aws cloudformation deploy --stack-name llmops-research-support --template-file infra/data-plane.yaml \
    --capabilities CAPABILITY_IAM --parameter-overrides DataAccessToken=<上面那個值>
acct aws cloudformation describe-stacks --stack-name llmops-research-support \
    --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
```

記下 `DataBucketName`、`PortalDataUri`、`DataBaseUrl`（**含 token**，只放在 Amplify 環境變數）。

### 3.2 第一個 release

portal repo 用合併後的 `main`（含 Release Radar roll-up 與 10-11 的資料）：

```bash
git -C ../llmops-portal checkout main && git -C ../llmops-portal pull && git -C ../llmops-portal status --short   # 要乾淨
export PORTAL_DATA_URI=s3://<DataBucketName>/portal/
portalctl bootstrap --portal ../llmops-portal --include data/open-models.json --dry-run
portalctl bootstrap --portal ../llmops-portal --include data/open-models.json
portalctl status
```

`--include data/open-models.json`：開源模型頁的資料（舊版的發布清單還不含它）。`radar/curation.json` 不必發布：Amplify build 用 repo 裡的那份。
`data/internal/platform-inventory*.json` 也會被發布到 token 網址後面；公司若不允許，先移出 checkout 再 bootstrap。

### 3.3 UI（新的 Amplify app）

1. Console：Amplify → Create new app → GitHub → `llmops-portal`、branch `main`（build 設定自動讀 repo 的 `amplify.yml`）。
2. 環境變數 `PORTAL_DATA_BASE_URL=<DataBaseUrl>`：build 時 `tools/fetch_release.mjs` 用它抓上線中的 release。
3. 需要的話開 Access control（basic auth）；ui-verify 讀網站要用的帳密放 Secrets Manager（`user:password`），第 3.8 步填 `SiteAuthSecretArn`。
4. 第一次 build 完成後：

```bash
acct aws amplify list-apps --query "apps[].[appId,defaultDomain]" --output table     # appId；網站是 https://main.<defaultDomain>
portalctl verify --url <DataBaseUrl> --origin https://main.<defaultDomain>
```

頁尾的資料版本應該等於 `portalctl status` 的 releaseId。

### 3.4 agent

```bash
cp agentcore/aws-targets.example.json agentcore/aws-targets.json
# 編輯：account = 專用帳號 id、region = 3.1 的 region
acct uv run --with-requirements scripts/requirements.txt python scripts/configure_agent.py --stack llmops-research-support
cd agentcore && ../scripts/in_account.sh agentcore validate && ../scripts/in_account.sh agentcore deploy && ../scripts/in_account.sh agentcore status && cd ..
acct uv run --with-requirements scripts/requirements.txt python scripts/invoke_agent.py --out verify.json --payload-json \
    '{"kind":"verify","claims":["claude-sonnet-4-5 在 Microsoft Foundry 的退役日是 2026-11-30"],"dryRun":true}'
```

輸出裡要看到 `portal data: release <releaseId>`；看到「內建的 SAMPLE 快照」代表 `PORTAL_DATA_URI` 沒生效。記下 runtime ARN。
（`agentcore` 指令要在有 `agentcore.json` 的資料夾執行；`in_account.sh` 用的是 repo 的 `agentcore/aws-targets.json`，從哪裡呼叫都一樣。）

### 3.5 pipeline（review、排程停用）

```bash
acct uv run --with boto3 python scripts/build_lambdas.py --upload s3://<DataBucketName>/artifacts/      # 印出 CodeBucket、CodeKey
acct aws cloudformation deploy --stack-name llmops-portal-pipeline --template-file infra/pipeline.yaml --capabilities CAPABILITY_IAM \
    --parameter-overrides DataBucketName=<DataBucketName> AgentRuntimeArn=<runtime ARN> CodeBucket=<CodeBucket> CodeKey=<CodeKey> \
        NotificationEmail=<公司信箱> PortalUrl=https://main.<defaultDomain> PublishMode=review ScheduleState=DISABLED \
        PermissionsBoundaryArn=<有的話>
acct aws cloudformation describe-stacks --stack-name llmops-portal-pipeline --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
export PIPELINE_STATE_MACHINE_ARN=<StateMachineArn>
```

確認 SNS 訂閱信，然後 `portalctl pause --reason "部署檢查"`、`portalctl resume`、`portalctl status`（各收到一封通知）。
pipeline 自己的每週排程維持 `DISABLED`：每日研究由 3.6 的每日執行負責。

### 3.6 每日執行（7 個 role）與 GitHub 觸發

不再用 `infra/daily-schedules.yaml`（五個 EventBridge 排程）：改成一個依序跑 7 個 role 的 state machine，由 GitHub Actions 每天啟動。
細節與 GitHub 端的設定在 [daily-agents-design.md](daily-agents-design.md) 第 6、7 節。

```bash
acct aws cloudformation deploy --stack-name llmops-daily --template-file infra/daily-orchestrator.yaml --capabilities CAPABILITY_IAM \
    --parameter-overrides PipelineStateMachineArn=<StateMachineArn> NotificationTopicArn=<NotificationTopicArn> PermissionsBoundaryArn=<有的話>
acct aws cloudformation deploy --stack-name llmops-github-oidc --template-file infra/github-oidc.yaml --capabilities CAPABILITY_IAM \
    --parameter-overrides DailyStateMachineArn=<llmops-daily 的 DailyStateMachineArn> PermissionsBoundaryArn=<有的話>
```

GitHub 的 environment `llmops-daily` 與四個變數先建好，但 workflow 的排程要到第 5 節才讓它生效（merge 到 main 之前不會跑）。

### 3.7 發布後自動重建網站

```bash
acct aws amplify create-webhook --app-id <appId> --branch-name main --description "llmops pipeline" \
    --query webhook.webhookUrl --output text > /tmp/amplify-hook && chmod 600 /tmp/amplify-hook   # 網址含 token，不要印出來
acct aws cloudformation deploy --stack-name llmops-portal-amplify-trigger --template-file infra/amplify-trigger.yaml --capabilities CAPABILITY_IAM \
    --parameter-overrides StateMachineArn=<StateMachineArn> AmplifyWebhookUrl="$(cat /tmp/amplify-hook)" PermissionsBoundaryArn=<有的話>
rm /tmp/amplify-hook
```

### 3.8 每日「網站已更新」通知

```bash
acct aws cloudformation deploy --stack-name llmops-portal-ui-verify --template-file infra/ui-verify.yaml --capabilities CAPABILITY_IAM \
    --parameter-overrides DataBucketName=<DataBucketName> NotificationTopicArn=<NotificationTopicArn> StateMachineArn=<StateMachineArn> \
        CodeBucket=<CodeBucket> CodeKey=<CodeKey> AmplifyAppId=<appId> SiteUrl=https://main.<defaultDomain> \
        SiteAuthSecretArn=<有 basic auth 才填> PermissionsBoundaryArn=<有的話>
```

---

## 4. 驗收

```bash
portalctl run --wait --payload-json '{"kind":"focus","focus":["lifecycle"],"budget":{"searches":8,"fetches":12}}'
portalctl pending && portalctl show <proposalId> && portalctl approve <proposalId>
```

核准後依序應該看到：Amplify 自動開始 build（3.7）→ build 成功 → 信箱收到 `Daily update live: <日期>`（3.8）。
當天 11:30 前沒收到，會收到 `Daily update NOT complete`，內文寫缺什麼（[runbook](runbook.md) 的 ui-verify 一節）。
接著做 README 步驟 8 的 rollback 演練（`portalctl rollback` → `status` → `rollback --to` → `resume`）。

## 5. 打開每日執行

1. GitHub → Actions → Daily LLMOps research → Run workflow，`roles` 填 `litellm-researcher`、勾 `wait`：job 綠色、收到 pipeline 的通知信。
2. 把 `.github/workflows/daily-research.yml` merge 到 main：之後每天 07:17（台北）自動啟動。

每天約 07:20–10:00 依序跑 7 個 role；`PublishMode=review` 時每一份提案都等妳核准，核准後網站自動更新並寄出當天的通知。
（不用 GitHub 的話：`llmops-daily` stack 設 `ScheduleState=ENABLED`，由 AWS 的排程啟動，兩者擇一。）
跑幾天、提案品質穩定後再考慮 `PublishMode=auto`（先讀 [known-issues](known-issues.md)）。

---

## 6. 下架個人帳號（會刪除資源：每一步先確認）

前提：第 4 節已通過、第 2 節的封存已完成。依照下面的順序，**一次一步**，每步之後重新跑第 2 節的盤點確認：

| 順序 | 資源 | 指令（個人 profile） | 備註 |
| --- | --- | --- | --- |
| 1 | 每日排程 | `aws cloudformation delete-stack --stack-name <daily-schedules 或 daily-orchestrator 的 stack> --profile $ME --region $R` | 盤點時找到的名稱；沒有就跳過 |
| 2 | 重建觸發、ui-verify | 同上，`amplify-trigger`、`ui-verify` 的 stack | 沒部署過就跳過 |
| 3 | pipeline | `aws cloudformation delete-stack --stack-name llmops-portal-pipeline --profile $ME --region $R` | state machine、Lambda、SNS、alarm 一起刪除 |
| 4 | AgentCore runtime 與 gateway | 在**原本**部署個人帳號的資料夾：`agentcore remove …` 之後 `agentcore deploy`（AWS_PROFILE=$ME） | 先 `agentcore remove --help` 看要移除的項目；完成後 `list-agent-runtimes`／`list-gateways` 應該是空的 |
| 5 | data plane stack | `aws cloudformation delete-stack --stack-name llmops-research-support --profile $ME --region $R` | bucket 是 `Retain`，不會被刪 |
| 6 | 舊 bucket | console：S3 → bucket → **Empty**（會連同所有版本），再 Delete | 有 versioning，`aws s3 rm --recursive` 刪不掉舊版本；確認封存完整後再做 |
| 7 | 舊 Amplify app | console：Amplify → app → Delete app | 先確認網址已經改用新的（書籤、分享出去的連結） |
| 8 | 其他 | CloudWatch log group（`/aws/lambda/llmops-*`、AgentCore 的 log）、ECR repository（若 agentcore 建過）、Secrets Manager、SNS 訂閱 | 用 Tag Editor／Resource Explorer 搜尋 `llmops` |

最後：隔天看 Cost Explorer（依 service 分組）確認個人帳號的 Bedrock、AgentCore、Lambda、CloudFront 都歸零。

## 回退

- 新帳號有問題、個人帳號尚未下架：把第 2 節停用的排程重新打開即可（資料都還在）。
- 已經下架：用封存的 `portal/` 內容不需要還原，從 portal repo 重新 bootstrap（第 3.2 步）就能在任一帳號重建資料位置。
