# Delivery plan：把 UI（Amplify）和 agent 的資料更新接起來

目標：portal 的 UI 繼續放在 Amplify，資料改成由 research agent 研究、pipeline 驗證後發布到 S3，UI 執行時從 S3（經 CloudFront）讀取。
每週自動執行一次，也可以隨時手動啟動。UI 的部署與資料的更新互不影響。

架構與設計理由在 [architecture.md](architecture.md)，逐步指令在 [README](../README.md) 第 4 節，日常操作在 [runbook.md](runbook.md)。

## 交付內容

| 項目 | 位置 | 狀態 |
| --- | --- | --- |
| Research agent（AgentCore Runtime） | `app/llmops_research/`、`agentcore/` | 妳帳號上跑的是上一版。這一版加上「讀上線中的 release」、程式層級的欄位保護與更嚴的驗證規則，**要在步驟 4 重新部署，尚未在 AWS 執行過** |
| Data plane（S3 + CloudFront + runtime 用的 policy） | `infra/data-plane.yaml` | 新；取代原本的 `support.yaml`，設計成可以原地更新既有的 stack |
| Update pipeline（Step Functions、4 個 Lambda、排程、通知、alarm、操作者的 policy） | `infra/pipeline.yaml`、`pipeline/` | 新 |
| 操作工具 | `scripts/portalctl.py`、`scripts/configure_agent.py` | 新：狀態、驗收、手動啟動、核准、rollback、pull／push；把 stack 的 output 寫進 runtime 的設定 |
| UI 改裝 | `scripts/patch_ui.py`、`ui/` | 新：把 `llmops-portal/web` 改成執行時從資料位置讀資料 |
| 本機驗證工具 | `scripts/serve_data.py`、`scripts/ui_smoke.py`、`scripts/review_proposal.html` | 新：不需要 AWS 就能看到整個流程；在瀏覽器裡看一份提案 |
| 文件 | `README.md`、`docs/`、`ui/README.md` | 架構、這份計畫、runbook、agent、UI |

## 現況：哪些已經驗證、哪些還沒有

> 這一版修正了第三輪獨立檢查找到的問題，並把 `auto` 模式能自己發布的範圍收窄成「新增的內容，以及日期、數字、版本的變更」；
> 之後又請檢查者重跑了一輪（第四輪：44 項），這一版也都處理了——其中最大的改變是「程式拿不準的值不猜，帶著旗標等人核准」。
> 第四輪的修正本身還沒有再被獨立檢查過。每一輪找到什麼、怎麼處理、哪些限制仍然存在，都在 [known-issues.md](known-issues.md)。
> template 的預設是 `PublishMode=review`；Phase 2 再決定要不要改成 `auto`。

**在開發環境實際執行過**（數字與細節在 README 第 2 節）

- agent 的離線測試、pipeline 的測試（S3 conditional write、SNS、Step Functions 用 moto 模擬；state machine 用兩個獨立的 ASL 引擎交叉驗證）、
  兩個 CloudFormation template 的 cfn-lint、CloudFront Function 以 Node 實際執行。
- 對發布關卡與驗證規則做過 mutation check（刻意把程式改壞，確認測試會失敗），也請了四輪彼此獨立的審查找邏輯、基礎設施、UI 與文件的問題；
  每一輪的結果與處理方式見 [known-issues.md](known-issues.md)。
- Lambda bundle 以 arm64 與 x86_64 都實際打包。載入與完成一次發布只用 x86_64 版測過；**arm64 版要到步驟 5 才第一次被載入**。
- **UI**：用妳發布過的 portal 原始碼（2026-09-28 的版本）套用 `patch_ui.py`，`tsc` 與 `vite build` 通過；在 headless Chromium 裡
  逐頁逐分頁比對（22 個頁面、99 個畫面），打包模式與資料位置模式顯示的內容完全相同；`ui_smoke.py` 對這份 build 走完
  「瀏覽途中發布 → 開著的頁面不變、重開的頁面把先前讀過的檔案都改從新版讀 → 改掉頁面上的一段文字、提示重新載入後看得到新的文字 →
  rollback 後畫面回到原本的文字 → token 錯誤 → 恢復」，全部通過（這一版用的資料是妳 Project 裡目前的那一份）。
- **妳目前的資料**：Project 裡的 22 個檔案在本機實際通過 `portalctl bootstrap` 與 `verify`，並和 portal 自己的 `tools/validate_data.py` 交叉檢查；
  有一則不影響發布的資料提醒（README 第 2 節）。

**還沒有驗證（需要 AWS 帳號）**

| 項目 | 風險 | 怎麼驗證 |
| --- | --- | --- |
| 兩個 stack 實際部署（IAM、CloudFront + OAC、Scheduler、SNS、alarm） | 中：template 通過 lint 與靜態檢查，但沒有在真的帳號建立過 | Phase 1 步驟 1、5 |
| CloudFront 實際的 token／CORS／快取行為 | 中：本機是照 template 寫的模擬器 | `portalctl verify --url … --origin …` 逐項檢查 |
| IAM 的實際行為（role 只能列出特定 prefix 時，讀不存在的物件回 404 還是 403） | 低：程式兩種都處理 | 步驟 5 的 `pause`／`resume` 檢查 |
| 既有 stack `llmops-research-support` 原地更新 | 中：會刪除舊的 trigger Lambda 與排程、新增 CloudFront；bucket 與 policy 保留 | 先用 change set 檢視（步驟 1 列了要看的三件事） |
| Lambda（arm64）在真實環境載入、呼叫 AgentCore runtime | 中 | 步驟 5 的檢查、步驟 6 的第一次手動執行 |
| 真實模型的研究品質 | 高：決定資料對不對 | Phase 2 的平行運轉 |
| 妳目前的 UI（比我手上的版本新）套用 patch | 低：script 認不得的地方會停下來、不寫入 | `patch_ui.py --check`，build 之後再跑 `ui_smoke.py`（確認沒有頁面繞過資料位置的是它） |
| 2026-10-05 之後才更新的資料能否通過 bootstrap 的驗證 | 低：Project 裡目前的資料已經在本機通過；規則和 portal 自己的 `validate_data.py` 相同，另外多了連結必須是 https 等幾項 | `portalctl bootstrap --dry-run` |
| Amplify：用 `VITE_DATA_BASE_URL` build、用 rewrite 當資料代理 | 低／未知：前者是 Vite 的標準做法但沒有在 Amplify 上跑過；後者只確認了設定語法 | 步驟 7；需要代理時用 `portalctl verify --url https://<UI>/portal-data/` |

## Phase 1 — 在 AWS 帳號接起來（預估 1～2 個工作天）

順序的原則：每一步都可以單獨驗證，而且在步驟 7 之前，現有的 portal UI 完全不受影響。
唯一的例外是舊的每週觸發：步驟 1 會把它移除，新的排程在步驟 8 才打開。

| # | 做什麼 | 驗收條件 |
| --- | --- | --- |
| 1 | 部署 data plane stack（原地更新既有的 `llmops-research-support`） | change set 裡 `DataBucket` 與 `RuntimeDataPolicy` 都是 Modify、Replacement 為 False；stack 的 output 有 `DataBaseUrl` |
| 2 | `portalctl bootstrap`：用 portal repo 目前的資料建立第一個 release | `bootstrap --dry-run` 沒有資料驗證問題、列出的檔案都是該公開給 UI 的；`portalctl status` 顯示上線版本 |
| 3 | `portalctl verify --url <DataBaseUrl> --origin <Amplify 網址>` | 全部 ✔（檔案 hash、no-cache／immutable、CORS、沒有 token 的路徑被拒絕） |
| 4 | `configure_agent.py --stack …` → `agentcore deploy` | `configure_agent.py --check --stack …` 通過；用 `invoke_agent.py` 跑一個 `verify` 任務，輸出有「portal data: release …」，不再是 SAMPLE |
| 5 | 打包 Lambda、部署 pipeline stack（**先用 `PublishMode=review`、`ScheduleState=DISABLED`**） | stack 建立成功；確認 SNS 訂閱；`portalctl pause` 與 `resume` 都成功並各收到一封通知 |
| 6 | 手動啟動一次小範圍的研究（`portalctl run --payload-json … --wait`） | 收到通知。有變更時是「approval needed」，內容列出實際的差異與引文，`approve` 後 `status` 顯示新的 release；沒有變更時是「no change」 |
| 7 | UI：`patch_ui.py` → build → `ui_smoke.py` → 在 Amplify 設定資料位置後部署 | 頁尾顯示資料版本；`ui_smoke.py` 全部 ✔；瀏覽器的 Network 面板看到資料來自資料位置 |
| 8 | rollback 演練（需要兩個 release），然後打開排程 | rollback 後 UI 回到前一版、狀態顯示自動發布已暫停；指定原本的版本再 rollback 一次、`resume` 後恢復；`ScheduleState=ENABLED` |

套用 patch 之後、設定資料位置之前，UI 仍然讀打包的資料（差別只有頁尾那一行）。所以 UI 的切換是「設定一個環境變數後重新部署」，要退回只要把它拿掉。

## Phase 2 — 平行運轉與調校（預估 2～4 週，每週花不到一小時）

| 做什麼 | 完成條件 |
| --- | --- |
| 維持 `review`，每週排程照跑 | 連續收到每週的通知 |
| 和現有的每週 Claude 研究任務並行，逐項比較兩邊的結果 | 兩邊的差異都有解釋（agent 漏掉的、agent 多找到的、判斷不同的） |
| 依比較結果調整：研究預算（`MAX_SEARCHES`／`MAX_FETCHES`）、起點頁面、prompt、`AutoMaxOps` | 連續 2～3 次提案可以不修改就核准 |
| 決定發布模式：改成 `auto`，或維持 `review`。判斷的材料：這幾週「不修改就核准」的提案裡，有多少屬於 `auto` 會自己發布的那一類（新增的內容、日期／數字／版本的變更） | 記錄決定與理由（誰負責看每週通知） |
| 停用舊的每週任務與 PR 流程 | 只剩一條更新路徑 |

`auto` 與 `review` 的差別只有「通過所有檢查的提案要不要等人」。`auto` 自己發布的只有新增的內容，以及既有紀錄上日期、數字、版本的變更；
其他對既有紀錄的修改都等待核准，完整的清單在 architecture 第 4 節：刪除或清空、改寫原有的文字、改狀態／分類／識別欄位／可信度標籤、換掉引用來源、
改清單順序、變更太多、動到需求檔、把檔案標成今天確認過卻沒讀完來源、疑似 prompt injection、含有不是 portal 來源的連結，
以及**程式拿不準的值**（引文裡不只一個模型名稱、有它不認得的名稱、日期有兩種讀法、digest 裡沒有出處的短寫日期…）。
所以即使改成 `auto`，每週仍然會有一部分更新需要妳核准——這是設計，不是故障。頁尾會標示自動發布的版本「未經人工逐項審核」。

平行運轉時值得記錄的兩個數字：每週的提案裡有幾份是因為「程式拿不準」而等人的（太多代表規則對常見的頁面版面太保守，可以回報調整），
以及 agent 有幾次因為驗證退回而交不出提案（`research finished without a proposal`；真實模型遇到這些規則改不改得過來，只能在這裡看到）。

## Phase 3 — 搬進公司環境

要先和公司的雲端／資安團隊確認的事（我無法從這裡得知，會影響部署方式）：

| 問題 | 影響 |
| --- | --- |
| 目標 region 有沒有 AgentCore Runtime、Gateway 的 web-search connector、要用的 Claude inference profile | 沒有的話 agent 要放在別的 region，或改用其他搜尋來源 |
| runtime 能不能對外連線（`networkMode: PUBLIC`，或 VPC + NAT） | agent 要讀公開網頁；不能對外就無法研究 |
| 建立 IAM role 是否需要 permissions boundary、命名或 tag 規則 | `pipeline.yaml` 有 `PermissionsBoundaryArn` 參數；AgentCore CLI 建立的 role 要另外處理 |
| 是否必須使用 SSE-KMS | 目前是 SSE-S3。改 KMS 時，key policy 要允許 CloudFront（OAC）、runtime role 與 Publish function 解密 |
| 可不可以用 CloudFront、Amplify；對外服務是否一定要 WAF | `data-plane.yaml` 有 `WebAclArn`；不能用 Amplify 時 UI 是純靜態檔，放 S3 + CloudFront 即可 |
| UI 要不要登入（SSO） | 要的話資料位置必須有對等的保護：見 architecture 第 6 節的三種做法 |
| `data/internal/platform-inventory.json` 的資料分級 | 它含內部路由名稱，會隨 release 發布到資料位置；不適合的話不要發布這個檔案（bootstrap 前移出 checkout），UI 會顯示 example |
| 發布紀錄會公開的內容 | `manifest.json`／`release.json` 裡有操作者名稱（`--by`）、原因、提案的理由與引文，UI 的使用者讀得到 |
| log 保存期限、告警要送到哪裡 | `LogRetentionDays` 參數；SNS topic 可以接 e-mail 以外的管道 |
| 誰可以核准、誰可以 rollback | 把 pipeline stack 的 `OperatorPolicyArn` 掛到對應的人身上：可以啟動與查看 execution、讀資料與提案，不能直接寫 bucket |
| 誰可以寫 `research/` | 等於「可以提案」的權限（architecture 第 6 節的信任邊界）；平常只有 runtime 的 role |

之後的工作項目：

1. 建立 dev／prod 兩套 stack（stack 名稱不同即可，bucket 各自獨立）。
2. 把部署寫進公司的 CI/CD：跑測試（README 第 3 節的三個指令）→ `build_lambdas.py --upload` → `cloudformation deploy` → `agentcore deploy`。
3. UI 加上登入與資料位置的對等保護。
4. 指定負責人：每週通知由誰看、失敗通知與 alarm 由誰處理（[runbook](runbook.md) 的排除表）。
5. 交接：帶負責人走一次 runbook 的「核准」「rollback」「手動發布測試結果」。

## Phase 4 — 之後可以做的（都不是上線的前提）

- 資料歷史留在 git：排程執行 `portalctl pull` 後 commit（也讓人工修改可以走 PR 審核）。
- 發布後自動檢查 UI：CloudWatch Synthetics canary 開首頁，出現 `.page-error` 或 `.data-status-error` 就告警（甚至自動 rollback）。
- 通知接到 Slack／Teams；在 UI 上做核准頁面（需要一個有身分驗證的 API）。
- AgentCore Observability 與 Evaluations：用每次 run 的驗證報告當評分素材，追蹤研究品質的變化。
- 測試結果與平台清單的自動發布：`tools/compat_suite.py`、`perf_probe.py`、`sync_inventory.py` 跑完後接 `portalctl push --only runs,data/internal --yes`。

## 風險與對策

| 風險 | 對策 |
| --- | --- |
| agent 讀到錯誤或惡意的網頁，錯的資料自動上線 | 一項事實要有一則「逐字出自頁面、講的是這個模型、來源層級夠高」的引文；`auto` 只自己發布新增的內容與日期／數字／版本的變更，其他都等人；疑似 injection 的頁面與文字等人、保護欄位、關卡從實際差異判斷、每項變更附來源、頁尾標示未經逐項審核、秒級 rollback；預設 `review` |
| 資料更新讓 UI 壞掉 | 發布前的 schema 與跨檔驗證（含測試結果與平台清單）；UI 的 error boundary 讓導覽列與其他頁面照常；rollback |
| UI 與資料的格式不同步 | 相容性規則（architecture 第 2 節）；改 UI 或 schema 後跑 `ui_smoke.py` |
| 官方頁面改版，agent 讀不到 | `tests/test_live.py --live` 是設計好的 canary；研究沒有結果時會收到通知，資料維持原狀 |
| 排程默默停止，或流程連錯誤都報不出來 | 每週應該收到一封通知（包含「沒有變更」）；沒有成功的 execution 會觸發 alarm；頁尾有發布日期 |
| token 網址外流 | 換一個 `DataAccessToken` 重新部署，再更新 UI 的設定；需要真正的存取控制時用 WAF 或登入 |
| 手上的 checkout 是舊的，手動發布把 agent 的更新蓋掉 | `push` 逐檔和上線的資料合併，兩邊都改過的檔案會停下來；只發布特定資料夾用 `--only` |
| 本機測試的指令不小心打到正式環境 | 指令列有 `--data` 時一律直接操作那個位置，待核准清單也用它旁邊的那一份（不用環境變數裡正式環境的）；每個動作開始前都會印出它走的路；本機用另一個 alias 名稱 |
| 有人照通知信再做一次 rollback，把壞掉的版本放回去 | rollback 不是開關：信裡的指令只對它提到的那一版有效，上線中的版本本身是 rollback 的結果時要明確指定目標 |

## 成本的組成（沒有估算金額）

主要是每次研究的模型 token（由 `MAX_*` 預算上限控制，`result.usage` 會回報實際用量）與 AgentCore Runtime 的執行時間。
S3、CloudFront、Lambda、Step Functions、Scheduler、SNS、CloudWatch alarm 在這個資料量與每週一次的頻率下都很小。
