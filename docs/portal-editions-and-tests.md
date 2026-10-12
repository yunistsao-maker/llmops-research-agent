# 兩個 portal（完整版、乾淨版）與上線前測試結果

## 1. 兩個版本

| | 完整版（full） | 乾淨版（clean） |
| --- | --- | --- |
| 給誰 | 平台團隊 | 公司裡需要知道最新 LLM 資訊的所有人 |
| 頁面 | 全部 | 沒有 測試結果、發布流程、LiteLLM 與安全、上手指南；模型頁沒有 Onboarding 分頁；沒有「AI 安全測試（示範・模擬資料）」 |
| 資料 | 全部 | deep dive 拿掉 `onboarding`；沒有 `data/litellm.json`、`config/`、`security-tests.html` |
| 內部測試分頁 | 有（G6 含遮蔽版逐筆案例） | 有（G6 只有統計與總表） |
| 每日版本選單 | 有 | 有 |

兩個版本是**同一個 repo、同一個 main**，由 Amplify 環境變數 `PORTAL_EDITION` 決定（portal repo `tools/build_site.mjs`）。
乾淨版不只是把選單藏起來：那幾頁的資料根本不在它的網站檔案裡。

### 部署（專用帳號）

```bash
export LLMOPS_AWS_PROFILE=llmops-sandbox LLMOPS_ACCOUNT_ID=<專用帳號 id> AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
acct() { scripts/in_account.sh "$@"; }
```

1. Amplify console → Create new app → GitHub → `llmops-portal`、branch `main`（和完整版同一個 repo 與 branch）。
   環境變數：`PORTAL_EDITION=clean`、`PORTAL_DATA_BASE_URL=<DataBaseUrl>`（和完整版同一個值）。
   完整版的 app 不用改（沒有 `PORTAL_EDITION` 就是 full）。
2. 每次發布後兩個網站都要重新 build：

   ```bash
   acct aws amplify create-webhook --app-id <乾淨版 appId> --branch-name main --description "llmops pipeline (clean)" \
       --query webhook.webhookUrl --output text > /tmp/hook-clean && chmod 600 /tmp/hook-clean
   acct aws amplify list-webhooks --app-id <完整版 appId> --query "webhooks[0].webhookUrl" --output text > /tmp/hook-full && chmod 600 /tmp/hook-full
   acct aws cloudformation deploy --stack-name llmops-portal-amplify-trigger --template-file infra/amplify-trigger.yaml --capabilities CAPABILITY_IAM \
       --parameter-overrides StateMachineArn=<StateMachineArn> AmplifyWebhookUrl="$(cat /tmp/hook-full)" SecondAmplifyWebhookUrl="$(cat /tmp/hook-clean)"
   rm /tmp/hook-full /tmp/hook-clean
   ```

   （兩個網址都含 token：不要印出來、不要貼到任何地方。`NoEcho` 參數更新時要重新給值，所以兩個都要給。）
3. 乾淨版也要每日通知：再部署一份 `infra/ui-verify.yaml`，stack 名稱不同、`SiteName=clean`，`AmplifyAppId` 與 `SiteUrl` 換成乾淨版的：

   ```bash
   acct aws cloudformation deploy --stack-name llmops-portal-ui-verify-clean --template-file infra/ui-verify.yaml --capabilities CAPABILITY_IAM \
       --parameter-overrides SiteName=clean DataBucketName=<DataBucketName> NotificationTopicArn=<NotificationTopicArn> \
           StateMachineArn=<StateMachineArn> CodeBucket=<CodeBucket> CodeKey=<CodeKey> AmplifyAppId=<乾淨版 appId> \
           SiteUrl=https://main.<乾淨版 defaultDomain>
   ```

   乾淨版的信主旨會帶 `[clean]`，「一天一封」的紀錄是它自己的（`pipeline/notified/ui-clean-<日期>.json`），兩個網站各自寄
   「Daily update live」、「Daily update NOT complete」與「UI build FAILED」。

## 2. 上線前的測試結果（模型頁的「內部測試」分頁，兩個版本都有）

分頁依發布關卡分層：G3 API 相容性、G4 效能與容量、G5 品質評測、G6 AI 安全測試。最上面是每一層**最近一次上線前測試**的結論，
下面是全部紀錄（上線前的在前，回歸／臨時／未標示目的的在後）。模擬資料會標示「模擬資料」。G5 目前還沒有工具產生結果檔，分頁會說明。

### 上線前的流程（平台團隊）

在 portal repo（測試工具在 `tools/`），對要上線的模型跑三層測試，都加 `--purpose pre-release` 與模型的 catalog id：

```bash
python3 tools/compat_suite.py --gateway <GW>/v1 --routes claude-sonnet-5-5,claude-sonnet-5-5@bedrock-jp \
    --purpose pre-release --model-id claude-sonnet-5-5 --litellm-version v1.104.2 \
    --out runs/compat/$(date +%F)-claude-sonnet-5-5.json
python3 tools/perf_probe.py --gateway <GW>/v1 --route claude-sonnet-5-5 --purpose pre-release --model-id claude-sonnet-5-5 \
    --litellm-version v1.104.2 --profile zh-tw --effort medium --concurrency 4 --requests 40 \
    --slo-ttfat-ms 8000 --slo-ttlt-ms 60000 --client-location "Taipei HQ" --out runs/perf/$(date +%F)-claude-sonnet-5-5-zh-tw.json
python3 tools/sec_suite.py run --gateway <GW>/v1 --routes claude-sonnet-5-5,claude-sonnet-5-5-current \
    --incumbent claude-sonnet-5-5-current --purpose pre-release \
    --model-ids claude-sonnet-5-5=claude-sonnet-5-5 --judge bedrock:<judge model>
python3 tools/sec_suite.py publish latest --purpose pre-release --model-ids claude-sonnet-5-5=claude-sonnet-5-5
```

`sec_suite.py publish` 寫出兩個檔：`runs/security/<runId>.json`（彙總：每項結論、計數、測什麼／怎麼判／及格標準）與
`runs/security/<runId>.cases.json`（逐筆案例，公開版遮蔽）。分頁只顯示這個模型那一欄。

然後在 llmops-research-agent 發布（網站讀的是 data plane 上的 release，只 commit 到 repo 的 `runs/` 不會出現在網站上）：

```bash
portalctl push --portal ../llmops-portal --only runs --note "Claude Sonnet 5.5 上線前測試" --dry-run
portalctl push --portal ../llmops-portal --only runs --note "Claude Sonnet 5.5 上線前測試"
```

發布後兩個網站自動重新 build，結果出現在模型頁的「內部測試」。

- G6 在兩個版本的差別：乾淨版只有統計與項目總表（portal build 時刪掉 `*.cases.json`，manifest 也不列）；完整版多了逐筆案例。
- 逐筆案例是遮蔽版：資料集內容風險 medium 以上、`publish: false` 或不明的案例，題目、回應、judge 說明與 tool 呼叫都清空（`redacted: true`）。
  pipeline（`publish.unredacted_cases`）拒絕發布沒有 `public: true`、或有文字但風險不是 none／low 或沒標 `redacted: false` 的案例。
- 含全文的完整結果留在測試者電腦的 `.cache/secsuite/`；pipeline 會拒絕發布含 `cases` 的彙總檔。
- research agent 從來不會改 `runs/`（AGENTS.md 的規則），測試結果只有平台團隊能發布。
- 測試結果不分每日版本：任何日期的頁面都顯示目前所有的測試紀錄。
