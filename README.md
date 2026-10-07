# LLMOps Portal：research agent ＋ 自動更新 pipeline

portal 的 UI 放在 Amplify；資料由部署在 **Amazon Bedrock AgentCore Runtime** 的 research agent 研究、由 pipeline 驗證後發布到 S3，
UI 執行時從 S3（經 CloudFront）讀取。每週自動執行一次，也可以隨時手動啟動。**UI 的部署和資料的更新互不影響。**

```
 每週排程 / 手動 ─► Step Functions ─► AgentCore Runtime（agent：上網查證 → 附引文的提案）
                          │                         │ 寫 research/
                          │ Publish：重新驗證        ▼
                          └────────────────► S3 bucket ── portal/manifest.json ─► releases/<id>/…（不可變）
                                                              │ CloudFront（唯讀、token、CORS）
                                                              ▼
                                         React UI（Amplify）：執行時讀資料；頁尾顯示資料版本
```

| 想知道 | 看哪裡 |
| --- | --- |
| 整體架構、資料契約、發布的關卡、安全模型、壞掉時會怎樣 | [docs/architecture.md](docs/architecture.md) |
| 上線的步驟、驗收條件、搬進公司環境要確認的事、風險 | [docs/delivery-plan.md](docs/delivery-plan.md) |
| 日常操作：手動啟動、核准、rollback、手動發布、問題排除 | [docs/runbook.md](docs/runbook.md) |
| UI 怎麼改、Amplify 怎麼設定、本機預覽 | [ui/README.md](ui/README.md) |
| agent 本身：驗證關卡、payload、環境變數 | [docs/agent.md](docs/agent.md) |
| **獨立檢查找到什麼、怎麼處理，以及仍然存在的限制** | [docs/known-issues.md](docs/known-issues.md) |

> **和上一版（v1）的差別。** 上一版附了一份「尚未修正的已知問題」（第三輪檢查的結果），這一版把它們修正了，並因此改了一項政策：`auto` 模式下不經過人就上線的，
> 只剩**新增的內容**與既有紀錄上**日期、數字、版本**的變更——程式能逐字對照引文的那幾種；其他對既有紀錄的修改一律等人核准。
> template 的預設值也改成 `PublishMode=review`。
>
> 接著照妳的要求重新請了檢查者（第四輪）：44 項可以重現的問題、32 處測試沒有發現的缺口，這一版都處理了。其中三項是做法上的改變：
> **引文一律是頁面上完整的一列或一句**；程式**拿不準**「這個值是不是在講這個模型」時不再硬選一邊，而是讓提案帶著旗標等人核准；
> 「日期、數字、版本的變更」要**同類換同類**才算。每一項找到什麼、怎麼處理，以及仍然存在的限制，都在 [docs/known-issues.md](docs/known-issues.md)。
> **第四輪的修正本身還沒有再經過一輪獨立檢查**——這是建議先用 `review` 模式運轉的理由之一。
>
> **v2.1 和 v2 的差別只有測試和文件。** 打包 v2 的時候 mutation 檢查還沒跑完；跑完之後補了 7 處測試的缺口（程式本身沒有錯，是測試沒有守住那幾條規則），第 2 節的數字也更新成最後的結果。v2 的 README 說第四輪沒被發現的 32 處「各補了測試」，
> 那句話不夠精確：實際的狀態寫在第 2 節「mutation 檢查的細節」。
> `app/` 的程式、`pipeline/portal_pipeline/`、`scripts/`、`infra/`、`ui/` 與 v2 完全相同：已經照 v2 部署的話不需要重新部署。
>
> 已經部署過 v1 的話，三個部分都要更新：pipeline（Lambda bundle 與 `infra/pipeline.yaml`）、agent（`agentcore deploy`）、UI（重新執行一次 `patch_ui.py`——
> `src/dataSource.ts` 有更新——再 build）；data plane stack 沒有變。建議先更新 pipeline 再更新 agent：把關的是 pipeline，agent 還是舊版的那段時間，
> 它的提案過的也是新版的關卡。**已經存在的 stack 更新時會沿用原本的 `PublishMode`**（v1 的預設是 `auto`）：要改成 `review` 請在更新時明確指定。

---

## 1. 組成

| 部分 | 位置 | 做什麼 |
| --- | --- | --- |
| Research agent | `app/llmops_research/`、`agentcore/` | 讀上線中的資料與公開網頁，產出每一項都附逐字引文、通過 schema 驗證的**提案**。只讀 `portal/`、只寫 `research/` |
| Data plane | `infra/data-plane.yaml` | private S3 bucket（`portal/` 給 UI 讀、`research/` 是 agent 的結果、`pipeline/` 是待核准的提案）＋唯讀的 CloudFront |
| Update pipeline | `infra/pipeline.yaml`、`pipeline/` | Step Functions：啟動研究 → 等結果 → 對上線中的資料重新驗證 → 發布（或等待核准、拒絕）→ SNS 通知；每週排程 |
| 操作工具 | `scripts/portalctl.py` | 狀態、驗收、手動啟動、核准、rollback、pull／push |
| UI 改裝 | `scripts/patch_ui.py`、`ui/` | 把 `llmops-portal/web` 改成執行時從資料位置讀資料 |

發布的原則：agent 的輸出只是提案。Publish 步驟不呼叫模型、不上網，用和 agent 相同的程式把提案重新套用在**現在上線的資料**上，
再從「實際會造成的差異」判斷：只有研究資料可以改；每一版都可以在幾秒內 rollback。`PublishMode=review`（預設）是每一份提案都要一行指令核准。
`PublishMode=auto` 下，不經過人就上線的只有兩類：**新增的內容**（新紀錄、附加的備註、多引用一個來源、新的 digest），以及既有紀錄上
**日期、數字、版本**的變更——這三種值，agent 的驗證器會要求它逐字出現在一則「出自實際抓回的頁面、講的是這個模型、來源層級夠高」的引文裡。
其他的（刪除或清空、改寫原有的文字、改狀態或分類、改可信度標籤、換掉引用來源、改清單順序、變更太多、含有不是 portal 來源的連結、
疑似 prompt injection…）都等人核准。完整的規則在 [architecture](docs/architecture.md) 第 4 節。

---

## 2. 目前驗證到哪裡（請先讀這一節）

**已在開發環境實際執行並通過**（數字是交付前最後一次完整執行的結果）

| 項目 | 結果 |
| --- | --- |
| agent 離線測試 | **963 passed**（另有 23 個需要 `--live`／`PORTAL_REPO`／`LIVE_MODEL` 的測試預設跳過）：完整 agent loop（腳本化模型＋假網路）、驗證關卡（一項事實一則引文、對象、來源層級、版本與數字、digest）、prompt injection（含中文與隱形字元）、SSRF、預算、AgentCore HTTP 契約、讀取 release 指標、保護欄位 |
| agent 對真實網頁（`--live`）、與 portal 自己的驗證器交叉檢查 | 10 passed（另外 2 個需要部署好的 Gateway 或真實模型，跳過）、11 passed（2026-10-06 對這一版重新執行：Foundry、Anthropic、Google、Bedrock、OpenAI 的退役表與 LiteLLM 的 release notes 都讀得到、逐列可引用；交叉檢查用的是 Project 裡目前的資料與 `tools/validate_data.py`；細節在 [docs/agent.md](docs/agent.md)） |
| pipeline 測試 | **1450 passed**（3 個跳過：2 個只適用 S3 的 metadata、1 個在 root 身分下不適用；沒有安裝 Playwright 的環境是 1383 passed、6 個跳過）：release 的讀寫與完整性、S3 conditional write 的並行發布（moto）、發布的每一道關卡（什麼自己上線、什麼等人、什麼被拒絕）、核准／捨棄／rollback／暫停、重複送達的請求、四個 Lambda handler、通知訊息、state machine、`portalctl`、UI patch、UI kit 的資料層（用 node 執行）、本機資料位置、提案檢視器、`ui_smoke.py` 自己認不認得出壞掉的 UI |
| 測試本身夠不夠力 | mutation 檢查（故意把程式改壞一處，看測試發不發現）：在這一版上一共試了 352 處，349 處被測試發現，另外 3 處改了也沒有任何行為差異。其中 11 處是這次檢查才發現測試漏了、補上測試之後才被發現的（程式本身沒有錯）。第四輪檢查者當時沒被發現的 32 處都處理了。細節在這張表下面 |
| 獨立檢查 | 四輪，每一輪由沒有看過先前討論的檢查者寫出可以重現的攻擊腳本。第四輪的 44 支重現腳本在這一版上全部重新執行：每一項不是被擋下（退回或等人核准），就是列在已知的限制裡。每一輪找到什麼、怎麼處理、**修正之後仍然存在的限制**，都在 [docs/known-issues.md](docs/known-issues.md)。第四輪的修正本身尚未再經過獨立檢查 |
| state machine | 同一份定義用兩個獨立的 ASL 引擎執行（moto 的 Step Functions、專案內的嚴格直譯器），10 種情境的狀態、路徑、輸出、通知主旨都一致（第 10 種是這一版新增的：連失敗報告都產生不出來時，state machine 自己寄通知） |
| IaC | 兩個 template 通過 cfn-lint；IAM 邊界、環境變數與程式的對應、log group 等以測試檢查（檢查的是 template 的內容，不是 IAM 在 AWS 上的實際行為）；CloudFront Function 的程式以 Node 實際執行（13 種路徑；本機的資料位置模擬器用同一組案例比對） |
| Lambda bundle | 以 arm64 與 x86_64 實際打包（約 17 MB）。x86_64 版在隔離的 Python 環境（只有標準函式庫與 bundle）載入，並走完第一個 release → 待核准 → 核准發布 → rollback → 暫停／恢復；**arm64 版在這台 x86_64 的開發機上無法載入**，要到步驟 5 之後第一次執行才會被載入 |
| AgentCore 設定 | `scripts/configure_agent.py` 寫出的 `agentcore.json` 通過 `agentcore validate`（CLI 0.31.0） |
| UI | 用 portal 的原始碼（2026-09-28 的版本）套用 `patch_ui.py`，`tsc` 與 `vite build` 通過。headless Chromium：22 個頁面、99 個畫面，打包模式與資料位置模式顯示的文字逐一比對完全相同（2026-10-06 以這一版的 UI kit 重新 build、重新比對）。UI kit 的資料層另外用 node 直接執行：不是資料檔路徑的名稱（含 `../`）一律不發出 request |
| 端到端（本機，真的瀏覽器：`ui_smoke.py` 對真的 UI build，資料是妳 Project 裡目前的那一份） | 66 項檢查全部通過（2026-10-06，這一版的 UI kit 與 `ui_smoke.py`）：每個畫面都畫得出來、沒有錯誤（導覽列的頁面各看兩次，共 153 個畫面）；瀏覽途中發布新 release，開著的頁面維持原本的版本；同一個分頁重新打開之後，先前讀過的 17 個檔案都改從新的 release 讀；沒有任何資料是從網站本身讀的，資料位置只收到單純的 GET（在資料位置那一端記錄）；id 裡帶著 `../` 的連結不會讓頁面去讀別的 release；發布一個改了頁面文字的 release → 5 分鐘定時檢查提示 → 重新載入後**畫面上看得到改過的文字**，其他 12 個資料檔在各自的頁面上也是；rollback → 切回分頁時提示 → 換回原版時提示消失 → 重新載入後**每一個資料檔的畫面都回到原本的文字**；token 錯誤時頁尾顯示 HTTP 403，資料位置的路徑與 token 不在畫面、視窗標題、元素屬性與 console 裡；資料位置恢復後頁尾自己恢復 |
| 妳目前的資料 | 用 Project 裡目前的資料（22 個檔案，digest 到 2026-10-05）在本機實際執行：`portalctl bootstrap` 成功、`verify` 4 項通過；agent 的驗證器和 portal 自己的 `tools/validate_data.py` 交叉檢查 11 passed。bootstrap 會印出一則**資料提醒**（不影響發布）：`data/litellm.json` 的 `avoid` 有一筆 `versions` 寫成「< v1.101.0-rc.1（有啟用 semantic cache 的多租戶部署）」——括號裡的條件讓 Gateway 頁面無法拿它來比對版本（頁面只會顯示這一筆，不會對任何版本提出警告）；把條件移到 `reason`、`versions` 只留 `< v1.101.0-rc.1` 就可以 |

**mutation 檢查的細節**

- **第四輪檢查者的 259 處**（agent 111、pipeline 148；當時有 32 處沒被發現）在這一版上重跑：217 處被發現（其中 2 處是補了測試之後）；39 處因為那段程式在第四輪重寫過，
  原本的改法套不上去（見下一項）；3 處改了也沒有行為差異——其實是兩個改法：一個用 2,166 組輸入比對過，另一個是版本號 pattern 裡一個多餘的條件
  （同一段共用程式，agent 與 pipeline 各算一次），用 84 萬段隨機文字比對過。
- **套不上去的 39 處**照現在的程式重新寫成 55 處（同一種破壞，再加上重寫時新增的幾條規則）：都被發現，其中 5 處是補了測試之後。
- **針對第四輪新增的規則另外寫的 77 處**（agent 45、pipeline 32）：都被發現，其中 4 處是補了測試之後。
- **當時沒被發現的 32 處**現在的狀態：26 處被測試發現；2 處就是上面那個沒有行為差異的改法；4 處（3 個改法）所在的程式已經重寫，
  重寫後對應的改法都被發現（其中「帶標籤的版本號」在 pipeline 這一側是 v2.1 才補上測試的）。
- **這次檢查補上的 11 處測試缺口**：來源層級裡編碼過的斜線（`%2f`）；同一個來源引用兩次的錯誤訊息；表格列裡有不認得的名稱；
  「每千個 token」只跟緊接在後的數字走；句子以日期開頭、後面接著別的模型；儲存格自己寫著「Replacement: …」；digest 的引文講的是條目沒提到的模型；
  版本欄位裡新增的不是版本號的文字；已核准、rollback 之後又以別的方式上線的提案不能捨棄；rollback 之後其他資料檔的畫面要回到原本的文字
  （不只是撤下的文字消失）；帶標籤的版本（`-stable`、`-rc.1`）在發布關卡也是「版本換版本」。前 4 處在 v2 就補了，後 7 處是 v2.1 補的。

**尚未驗證（需要 AWS 帳號，我在這裡做不到）**

| 項目 | 怎麼驗證 |
| --- | --- |
| 兩個 stack 實際部署：IAM、CloudFront＋OAC、EventBridge Scheduler、SNS、CloudWatch alarm | 第 4 節；template 只通過了 lint 與靜態檢查 |
| CloudFront 實際的 token／CORS／快取行為 | `portalctl verify --url <DataBaseUrl> --origin <UI 網址>` 會逐項檢查；本機用的是照 template 寫的模擬器。token 錯誤時的 403 帶不帶 CORS header 我無法確定（只影響頁尾顯示的是「HTTP 403」還是「連不到」） |
| IAM 的實際行為：role 只有特定 prefix 的 `s3:ListBucket` 時，讀不存在的物件是 404 還是 403 | 程式兩種都處理了（403 時改用 list 判斷）；步驟 5 最後的 `pause`／`resume` 會實際走一次 |
| 既有的 `llmops-research-support` stack 原地更新 | 第 4 節步驟 1 先看 change set |
| Lambda（arm64）在 AWS 上載入、呼叫 AgentCore runtime、Step Functions 實際執行 | 第 4 節步驟 5 的檢查與步驟 6 的第一次手動執行 |
| 這一版 agent 重新部署到 AgentCore | 第 4 節步驟 4；妳帳號上目前跑的是上一版 |
| 真實模型的研究品質 | [delivery plan](docs/delivery-plan.md) Phase 2：先用 `review` 模式和現有的每週研究並行比較 |
| 目前的 UI（我手上的原始碼是 2026-09-28 的版本）套用 patch；2026-10-05 之後才更新的資料 | `patch_ui.py --check`（有問題會停下來並說明，不會寫入）→ build → `ui_smoke.py`（確認沒有頁面繞過資料位置的是它）；資料用 `portalctl bootstrap --dry-run` |
| state machine 的新狀態 `NotifyUnreported`（用了 `States.Format`） | 在兩個本機 ASL 引擎上執行過、template 通過 cfn-lint；第一次更新 pipeline stack 時 CloudFormation 會驗證它的定義 |
| Amplify：`VITE_DATA_BASE_URL` 環境變數的 build、rewrite 當資料代理 | 步驟 7；rewrite 只確認了設定語法，見 [ui/README.md](ui/README.md) |

---

## 3. 快速開始（不需要 AWS）

需要 `uv` 與 Node 20+；UI 的部分另外需要 portal repo 放在旁邊的 `../llmops-portal`。指令都在這個專案的根目錄執行。

```bash
# 測試
(cd app/llmops_research && uv run --extra dev python -m pytest tests)                    # agent，約 1 分鐘
uv run --with-requirements pipeline/requirements-dev.txt python -m pytest pipeline/tests  # pipeline，約 7 分鐘（連同瀏覽器測試約 13 分鐘）
uvx cfn-lint infra/data-plane.yaml infra/pipeline.yaml
```

pipeline 的測試裡有 66 個需要 Playwright 與 Chromium 的瀏覽器測試（`test_ui_smoke.py`、`test_review_viewer.py`）；沒有安裝時這兩個檔案會自動跳過。
要一起跑的話，把上面的指令換成
`uv run --with-requirements pipeline/requirements-dev.txt --with-requirements scripts/requirements-ui.txt python -m pytest pipeline/tests`
（第一次先下載瀏覽器，見 [ui/README.md](ui/README.md) 第 4 節）。

在本機看完整的流程（UI ＋ 資料位置 ＋ 發布 ＋ rollback）。alias 用 `portalctl-local`，刻意和第 4 節操作正式環境的 `portalctl` 不同名；
指令列上有 `--data` 時，每個動作都直接操作那個資料夾，不會送到 AWS：

```bash
alias portalctl-local='uv run --with-requirements scripts/requirements.txt python scripts/portalctl.py --data ./_local/portal'

portalctl-local bootstrap --portal ../llmops-portal    # 用 portal repo 的資料建立第一個 release（在 ./_local/）；會先列出檔案再問妳
uv run --with-requirements scripts/requirements.txt python scripts/serve_data.py ./_local/portal    # 本機的資料位置 http://127.0.0.1:8787/（佔用這個終端機）
```

```bash
# 終端機 2：把 UI 指到它（先套用過 ui/README.md 第 1 節的 patch）。用環境變數，不改任何檔案
cd ../llmops-portal/web && VITE_DATA_BASE_URL=http://127.0.0.1:8787/ npm run dev
```

```bash
# 終端機 3（回到這個專案，先再定義一次上面的 alias）：改一筆資料、發布、再 rollback，同時看瀏覽器
portalctl-local status
portalctl-local pull --portal ../llmops-portal         # …然後改一個 data/*.json…
portalctl-local push --portal ../llmops-portal         # 切到別的分頁再切回來（或等 5 分鐘），右下角提示有新的資料版本
portalctl-local rollback --reason "試試看"             # 回到前一版；自動發布同時暫停
portalctl-local resume
git -C ../llmops-portal checkout -- data               # 測完把 portal repo 的資料改回來
```

`./_local/` 只是本機測試用（已在 `.gitignore`）。`bootstrap`、`pull`（以及把整個 checkout 發布出去的 `push`）會在 portal repo 留下
`.portal-release.json`（記錄 checkout 的資料來自哪個 release），建議加進 portal repo 的 `.gitignore`。`pull` 會讓 checkout 等於它記錄的那個 release：
上線已經移除、而妳沒有改過的檔案會從 checkout 刪掉；妳改過的檔案不會被覆蓋。

---

## 4. 部署到 AWS 帳號

**前置**

- AWS CLI v2 與憑證（要能建立 CloudFormation stack、IAM role／policy、S3、CloudFront、Lambda、Step Functions、EventBridge Scheduler、SNS、CloudWatch alarm）
- `uv`、Node 20+、`npm install -g @aws/agentcore`；能連到 PyPI 與 npm
- Bedrock 已可使用 Claude；portal repo 在 `../llmops-portal`
- agent 已經部署過的話從步驟 1 開始；全新的環境先做 [docs/agent.md](docs/agent.md) 的本機測試，再照下面的順序（步驟 4 會部署 agent）

每一步都可以單獨驗證。**在步驟 7 之前，現有的 portal UI 完全不受影響**（它仍然讀打包的資料）。要注意的是步驟 1 會移除舊 stack 的每週觸發
（`TriggerFunction`／`WeeklySchedule`），新的排程到步驟 8 才打開；中間這段時間沒有自動研究。

```bash
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1    # stack 與 runtime 所在的 region（妳目前的部署在 us-east-1）；兩個都設，AWS CLI 與 boto3 才一致
alias portalctl='uv run --with-requirements scripts/requirements.txt python scripts/portalctl.py'
```

### 步驟 1：data plane（S3 ＋ CloudFront）

```bash
# 既有的 stack 先建立 change set 來看，不執行
aws cloudformation deploy --stack-name llmops-research-support --template-file infra/data-plane.yaml \
    --capabilities CAPABILITY_IAM --no-execute-changeset
```

在 console（或 `aws cloudformation describe-change-set`）確認三件事再繼續：

- `DataBucket` 是 **Modify**，`Replacement` 是 False（不是 Replace：bucket 與裡面的資料都留著）；
- `RuntimeDataPolicy` 也是 Modify、`Replacement` 是 False（否則它的 ARN 會變，runtime 的設定要跟著改）；
- 舊的 `TriggerFunction`、`WeeklySchedule` 等資源是 Remove，CloudFront 相關資源是 Add。

```bash
# 確認後執行（CloudFront 第一次建立要幾分鐘）
aws cloudformation deploy --stack-name llmops-research-support --template-file infra/data-plane.yaml --capabilities CAPABILITY_IAM
aws cloudformation describe-stacks --stack-name llmops-research-support \
    --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
```

記下 output：`DataBucketName`、`PortalDataUri`、`DataBaseUrl`（另外的 `OutputUri`、`RuntimeDataPolicyArn` 步驟 4 會自動讀）。
這份專案不含舊的 `support.yaml`；要退回舊的定義，用妳原本的 template 重新部署同一個 stack 即可（bucket 不會被動到）。

| 參數 | 預設 | 說明 |
| --- | --- | --- |
| `DataAccessToken` | 空（＝用 stack 的 unique id） | 資料網址的第一段。建議自己指定：`openssl rand -hex 16` 產生一次並保存。之後更新 stack 不必再給（沿用上一次的值）；要換 token 時才給新的。`none` ＝ 不需要 token |
| `AllowedOrigins` | `*` | 允許從瀏覽器讀資料的網頁來源。建議維持 `*`（保護靠的是 token 網址）；UI 有多個網址時一定要維持 `*`，原因見 [ui/README.md](ui/README.md) |
| `WebAclArn` | 空 | 選用：AWS WAF web ACL（scope CLOUDFRONT，必須建在 us-east-1），例如公司 IP allow list |
| `PriceClass`、`ResultRetentionDays`、`BucketName` | `PriceClass_200`、365、空 | CloudFront 的節點範圍、研究紀錄保存天數、指定 bucket 名稱（既有的 stack 不要改） |

### 步驟 2：第一個 release

```bash
export PORTAL_DATA_URI=s3://<DataBucketName>/portal/
git -C ../llmops-portal status --short                       # 應該是乾淨的：發布的是 checkout 裡現在的內容
portalctl bootstrap --portal ../llmops-portal --dry-run     # 檢查資料（schema、跨檔規則、測試結果與平台清單的格式），列出會發布的檔案
portalctl bootstrap --portal ../llmops-portal               # 再列一次並詢問；確認後發布
portalctl status
```

- 發布的是 UI 會讀的檔案：schema、`data/` 的研究資料、digest、模型 deep dive、`runs/` 的測試結果、`config/litellm.example.yaml`，
  **以及 `data/internal/platform-inventory*.json`（平台內部清單）**。內部清單不適合放在 token 網址後面的話，先把它移出 checkout 再 bootstrap（UI 會顯示 example）。
  其他檔案（筆記、原始匯出檔、別的 yaml）不會被發布；真的需要時用 `--include`。
- 如果 `portal/` 底下有以前 `sync_portal_data.py` 上傳的平面複本，`bootstrap` 會提醒並印出刪除的指令（它們不再被讀取）。

### 步驟 3：驗收 data plane

```bash
portalctl verify --url <DataBaseUrl> --origin <UI 的網址，例如 https://main.d111.amplifyapp.com>
```

逐項檢查：每個檔案的 sha256、`manifest.json` 不被快取、release 檔案可長期快取、CORS 允許 UI 的來源、沒有 token 的路徑被拒絕。輸出不會印出 token。

### 步驟 4：讓 agent 讀上線中的資料

交付的 `agentcore/agentcore.json` **不含任何帳號的設定**，而且這份專案刻意不含每個帳號自己的三個檔案：
`agentcore/aws-targets.json`（部署到哪個 account／region）、`agentcore/.cli/deployed-state.json`（已經部署了什麼）、`agentcore/.env.local`。

- 把這份專案解壓縮**覆蓋在原本的專案資料夾上**：那三個檔案會原封不動留著，deploy 更新的是同一個 runtime。
  `agentcore.json` 則會被換成交付的版本；妳改過的其他設定（模型、預算…）要自己搬回來。
- 解壓縮到**新的資料夾**：把原本資料夾的 `aws-targets.json` 與 `.cli/deployed-state.json` 複製到同一個位置
  （少了 `aws-targets.json`，`agentcore validate` 會直接說找不到它）。從來沒有部署過的話，照 [docs/agent.md](docs/agent.md) 第 5 節建立。

然後：

```bash
# 把 stack 的三個 output 寫進 agentcore.json：PORTAL_DATA_URI、OUTPUT_URI，以及 additionalPolicies 的 RuntimeDataPolicyArn
uv run --with-requirements scripts/requirements.txt python scripts/configure_agent.py --stack llmops-research-support
agentcore validate && agentcore deploy
agentcore status                                                        # 記下 runtime ARN
```

`additionalPolicies` 少了那個 ARN 的話，deploy 會把它從 runtime role 拿掉（role 是照這個檔案設定的），agent 就讀不到 `portal/`、寫不了 `research/`；
`configure_agent.py --check` 可以隨時檢查。

```bash
# 確認 agent 讀的是上線中的資料：輸出裡 "portal data:" 開頭的那一行應該是 "portal data: release <releaseId>（s3://…）"
uv run --with-requirements scripts/requirements.txt python scripts/invoke_agent.py --out verify.json --payload-json \
    '{"kind":"verify","claims":["claude-sonnet-4-5 在 Microsoft Foundry 的退役日是 2026-11-30"],"dryRun":true}'
```

那一行如果寫的是「內建的 SAMPLE 快照」，代表 `PORTAL_DATA_URI` 沒有生效（這種提案 pipeline 會拒絕發布）。

### 步驟 5：pipeline

```bash
# 打包 Lambda 並上傳；最後一行印出 CodeBucket 與 CodeKey
uv run --with boto3 python scripts/build_lambdas.py --upload s3://<DataBucketName>/artifacts/

aws cloudformation deploy --stack-name llmops-portal-pipeline --template-file infra/pipeline.yaml --capabilities CAPABILITY_IAM \
    --parameter-overrides DataBucketName=<DataBucketName> AgentRuntimeArn=<runtime ARN> CodeBucket=<CodeBucket> CodeKey=<CodeKey> \
        NotificationEmail=<妳的 e-mail> PortalUrl=<UI 的網址> PublishMode=review ScheduleState=DISABLED
aws cloudformation describe-stacks --stack-name llmops-portal-pipeline --query "Stacks[0].Outputs[].[OutputKey,OutputValue]" --output table
export PIPELINE_STATE_MACHINE_ARN=<StateMachineArn>
```

收到 SNS 的訂閱確認信後點確認。這裡先用 `PublishMode=review`、`ScheduleState=DISABLED`：先手動跑、每一份都看過，再打開排程（步驟 8）。

接著花一分鐘確認 pipeline 的權限與通知是通的（不啟動研究、不改資料內容）：

```bash
portalctl pause --reason "部署檢查"      # 走一次 Publish function：讀寫 manifest.json、發通知
portalctl resume
portalctl status                         # 「自動發布　正常」
```

兩個指令都應該成功，並各收到一封通知。失敗的話看 [runbook](docs/runbook.md) 的問題排除（這是 arm64 的 Lambda bundle 第一次被載入、IAM 第一次被實際使用的地方）。

| 參數 | 預設 | 說明 |
| --- | --- | --- |
| `PublishMode` | `review` | `review`：每一份提案都等核准。`auto`：新增的內容與日期／數字／版本的變更通過檢查就發布，其他對既有紀錄的修改仍然等人（[architecture](docs/architecture.md) 第 4 節） |
| `AutoMaxOps`、`AutoAllowRemove` | 60、`false` | `auto` 模式下，變更超過這個數量、或會刪除資料／清空文字的提案等待核准 |
| `ScheduleExpression`、`ScheduleTimezone`、`ScheduleState` | `cron(0 9 ? * MON *)`、`Asia/Taipei`、`ENABLED` | 每週排程 |
| `ScheduleRequest` | `{"kind":"weekly"}` | 排程送給 agent 的 request |
| `NotificationEmail`、`PortalUrl` | 空 | 通知的收件人；通知裡提到的 portal 網址（不要填資料位置的網址，它含 token） |
| `ResearchTimeoutSeconds` | 2400 | 等研究結果的上限；要大於 runtime 的 `MAX_RUNTIME_SECONDS`（1500） |
| `PermissionsBoundaryArn` | 空 | 選用：公司要求建立 role 時附加的 permissions boundary |
| `AgentQualifier`、`LogRetentionDays` | 空、90 | runtime endpoint 名稱；Lambda log 保存天數 |

output 的 `OperatorPolicyArn` 是給操作者的 managed policy：啟動與查看 execution、讀資料與待核准的提案，沒有任何寫入 bucket 的權限。
要讓其他人核准或 rollback，把它掛到對方的 user／role 上即可。

### 步驟 6：第一次手動執行

```bash
portalctl run --wait --payload-json '{"kind":"focus","focus":["lifecycle"],"budget":{"searches":8,"fetches":12}}'
```

研究（上限 25 分鐘）完成後會收到通知。有變更的話主旨是「approval needed」，內文先列出**實際會造成的差異**（原值 → 新值），再列提案自己的理由、引文與來源網址。

```bash
portalctl pending
portalctl show <proposalId>            # 完整內容；加 --out p.json 可以用 scripts/review_proposal.html 打開
portalctl approve <proposalId>         # 或 discard <proposalId> --reason "…"
portalctl status                       # 上線版本換成新的 release
```

主旨如果是「no change」，代表這次研究沒有找到需要改的地方：流程是通的，只是沒有東西可以核准。步驟 8 的 rollback 演練需要兩個 release，
這時可以換一個 `focus` 再跑一次，或用手動發布產生第二個：在 checkout 裡放一份新的測試結果（或改一個檔案）之後
`portalctl push --portal ../llmops-portal --only runs --note "演練"`。checkout 和上線的資料完全相同時，push 會回報「沒有變更」，不會產生新的 release。

### 步驟 7：UI

```bash
python scripts/patch_ui.py ../llmops-portal/web --check
python scripts/patch_ui.py ../llmops-portal/web
(cd ../llmops-portal/web && npm run build)             # 這時還是打包的資料
uv run --with-requirements scripts/requirements-ui.txt python scripts/ui_smoke.py \
    --dist ../llmops-portal/web/dist --portal ../llmops-portal      # 選用：在真的瀏覽器裡逐頁檢查（第一次見 ui/README.md 第 4 節）
```

commit（**連同 `web/.patch_ui/`**）之後，在 Amplify 的環境變數加上 `VITE_DATA_BASE_URL=<DataBaseUrl>` 並重新部署（或在 `public/config.json` 寫 `dataBaseUrl`）。
頁尾出現「資料版本 …」就表示 UI 已經在讀資料位置。要退回只要把變數拿掉重新部署。細節與其他設定方式在 [ui/README.md](ui/README.md)。

### 步驟 8：rollback 演練，然後打開排程

演練需要至少兩個 release（`portalctl history`）。分開執行，每一步都看一下結果：

```bash
portalctl rollback --reason "演練"                      # 回到前一版，自動發布同時暫停
portalctl status                                        # 上線的是前一版；UI 切回分頁（或重新整理）後也是
portalctl rollback --to <剛才的 releaseId> --reason "演練結束"    # 回到原本那一版（rollback 不是開關：要指定回到哪一版）
portalctl resume                                        # 恢復自動發布
```

```bash
aws cloudformation deploy --stack-name llmops-portal-pipeline --template-file infra/pipeline.yaml --capabilities CAPABILITY_IAM \
    --parameter-overrides ScheduleState=ENABLED                 # 其他參數沿用原值
```

和現有的每週研究並行幾週、確認提案的品質之後，再決定要不要把 `PublishMode` 改成 `auto`（同樣的指令，`--parameter-overrides PublishMode=auto`），
並停用舊的每週任務。決定之前請先讀 [docs/known-issues.md](docs/known-issues.md) 的「仍然存在的限制」：`auto` 自己發布的範圍雖然窄，新增的文字與新紀錄
仍然是沒有人逐項看過就上線的。完整的階段與驗收條件在 [docs/delivery-plan.md](docs/delivery-plan.md)。

### 移除

```bash
aws cloudformation delete-stack --stack-name llmops-portal-pipeline       # 排程、state machine、Lambda、SNS topic、alarm
```

data plane stack 的 bucket 是 `Retain`：刪除 stack 之後 bucket 與資料都還在，確定不要了再自己清空並刪除（它有 versioning，舊版本也要刪）。
`artifacts/` 裡的 Lambda bundle 不會自動清理。runtime 用 AgentCore CLI 移除（`agentcore remove …` 之後 `agentcore deploy`）。
UI 把 `VITE_DATA_BASE_URL` 拿掉重新部署就回到打包模式；`patch_ui.py --revert` 撤回程式的修改。

---

## 5. 日常操作

```bash
portalctl status                          # 上線版本、是否暫停、待核准
portalctl run --wait                      # 立刻跑一次每週研究
portalctl show <proposalId> / approve <proposalId> / discard <proposalId>
portalctl rollback --reason "…"           # 回到前一版（自動發布隨之暫停，確認後 resume）
portalctl push --portal ../llmops-portal --only runs,data/internal    # 發布測試結果與平台清單
portalctl verify --url <DataBaseUrl>      # UI 讀不到資料時
```

設定了 `PORTAL_DATA_URI` 與 `PIPELINE_STATE_MACHINE_ARN` 兩個環境變數時，approve／discard／rollback／pause／resume 會經由 state machine 執行
（有執行紀錄、會發通知）；每個動作開始前都會印出它走的是哪一條路。指令列上有 `--data`（或資料位置是本機資料夾）時一律直接操作那個位置；
`run` 與 `executions` 只存在於 state machine，這種情況下它們會拒絕執行並說明原因。`portalctl publish <本機檔案>` 在會經由 pipeline 的環境裡要加
`--direct` 才會執行（那是直接寫入資料位置）。`--by`、`--reason`、`--note` 會寫進 `manifest.json`，UI 的使用者讀得到。

不用 CLI 也可以手動啟動：Step Functions console 對 `llmops-portal-pipeline-update` 按 Start execution，input 填 `{}`。
完整的操作、通知的意思與問題排除表在 [docs/runbook.md](docs/runbook.md)。

---

## 6. agent 的 request

`portalctl run --payload-json`、`ScheduleRequest`、Step Functions input 的 `request` 都是同一種 JSON：

| 欄位 | 說明 |
| --- | --- |
| `kind` | `weekly`（預設：整週巡檢＋weekly digest）· `focus`（只查指定範圍）· `verify`（驗證一組說法）· `prompt`（自由指示） |
| `focus` | `lifecycle` · `litellm` · `pricing` · `new-models` · `regulatory` |
| `claims`、`urls`、`instruction` | `verify` 的說法、起點網址（https）、文字指示 |
| `budget` | `{"searches":10,"fetches":15,"toolCalls":80}`，只能調低部署上限 |

其餘欄位、事件串流、直接呼叫 agent（不經過 pipeline）的方式見 [docs/agent.md](docs/agent.md)。

---

## 7. 專案結構

```
agentcore/                 AgentCore CLI 專案設定（agentcore.json、aws-targets*.json、cdk/）
app/llmops_research/       research agent
  main.py                  AgentCore 入口（/invocations、/ping、背景模式）
  research/                agent 與工具、引文落地、驗證關卡、SafeFetcher
    portal/                portal 資料模型：載入（跟著 release 指標）、JSON patch、差異比對（diff.py）、標籤與時間戳記的判讀（labels.py）、
                           schema 與跨檔檢查、保護欄位
    validate/              引文比對、日期與數字、引文講的是不是這個模型（subjects.py）、注入掃描（injection.py）、文字裡的連結（urls.py）
    apply.py               把提案套用到資料上（agent 與 pipeline 共用同一份）
  tests/                   離線測試、test_live.py（--live）、test_portal_parity.py（PORTAL_REPO）
pipeline/
  portal_pipeline/         storage（S3／資料夾）· releases（資料契約）· publish（關卡、核准、rollback、手動發布）
                           notify（通知訊息）· agentcore（啟動研究）· handlers（四個 Lambda 入口）
  tests/                   含 fixtures/：真的 agent run 紀錄、portal 的範例檔、UI 原始檔、測試用的假 UI
infra/
  data-plane.yaml          S3、CloudFront（OAC、token、CORS）、runtime 用的 managed policy
  pipeline.yaml            Step Functions、4 個 Lambda、EventBridge Scheduler、SNS、alarm、操作者用的 managed policy
scripts/
  portalctl.py             操作工具
  configure_agent.py       把 data plane stack 的 output 寫進 agentcore.json
  build_lambdas.py         打包 Lambda bundle（含相依套件）並上傳
  patch_ui.py              改裝 UI；ui_smoke.py：在瀏覽器裡檢查改裝後的 UI
  serve_data.py            本機的資料位置（模擬 CloudFront）
  invoke_agent.py          直接呼叫 agent；review_proposal.html：在瀏覽器裡看一份提案
  apply_proposal.py、sync_portal_data.py   舊流程（提案 → PR、平面同步），見 docs/agent.md 第 8 節
ui/files/                  patch_ui.py 複製進 UI 的檔案：dataSource.ts、DataStatus.tsx、ErrorBoundary.tsx、config.json
docs/                      architecture、delivery-plan、runbook、agent、known-issues（獨立檢查的結果與仍然存在的限制）
```

---

## 8. 設計取捨（細節在 architecture）

- **指標＋不可變的 release**：一次更新是原子的、可以幾秒內 rollback、檔案可以快取一年、每一版都有稽核紀錄。
- **資料的 source of truth 在 S3**，portal repo 的 `data/` 是工作副本：手動修改前先 `pull`（不會覆蓋妳改過的檔案）。
- **關卡看的是實際的差異，不是提案怎麼寫**：一個 `replace` 可以夾帶刪除或改寫一整個清單，所以「會不會刪資料、改了多少」都從套用前後的資料比對出來。
- **token 網址是分享連結等級的保護，不是身分驗證**。UI 需要登入時，資料位置要有對等的保護（WAF、同源代理或同一層登入）。
- **自動發布是選項，不是前提，而且範圍刻意很窄**：程式只自己發布它能對照引文確認的變更（新增的內容；日期、數字、版本），
  狀態、分類、文字改寫這類「沒有任何程式能拿去和引文比對」的修改留給人。安全性來自這些關卡、頁尾標示「未經人工逐項審核」、每項變更附引文、以及 rollback；
  預設是 `review`，跑一段時間再決定。
- **UI 用 patch 而不是整包取代**：只動資料載入這一層，認不得的地方就停下來，每一項修改都可以單獨撤回。
