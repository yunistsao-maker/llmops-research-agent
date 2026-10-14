# 架構：UI 與資料更新分開

這份文件說明整個系統怎麼接在一起、每個決定的理由，以及壞掉時會發生什麼事。
要部署請看 [README](../README.md)；日常操作看 [runbook](runbook.md)；上線步驟與驗收條件看 [delivery plan](delivery-plan.md)。

## 1. 一張圖

```
                         ┌──────────────── control plane（何時跑、誰核准）─────────────────┐
  EventBridge Scheduler ─┤  Step Functions state machine  <stack>-update                   │
  （每週一 09:00 台北）   │   StartResearch → Wait/Check → Publish → Notify(SNS e-mail)     │
  手動：portalctl run     │   同一個入口也接受 approve / discard / rollback / pause / resume │
        Step Functions ───┤                                                                 │
        console           └──────┬──────────────────────────────┬──────────────────────────┘
                                 │ InvokeAgentRuntime            │ 讀 research/、寫 portal/ 與 pipeline/
                                 ▼ （background）                ▼
  ┌──────── research plane ────────────┐        ┌──────────────── data plane ────────────────────────┐
  │ AgentCore Runtime（llmops_research）│  讀    │ S3 bucket（private、versioned）                     │
  │  Strands agent + Claude（Bedrock）  │◄───────┤  portal/manifest.json        ← 指標（唯一會變的檔案）│
  │  上網查證 → 逐字引文 → 驗證關卡     │        │  portal/releases/<id>/…      ← 不可變的資料版本      │
  │  → proposal（只是提案）            │  寫    │  research/runs|proposals/…   ← agent 的研究結果      │
  │                                     ├───────►│  pipeline/pending|decided|rejected/ ← 待核准與紀錄   │
  └─────────────────────────────────────┘        └───────────────┬────────────────────────────────────┘
                                                                  │ OAC（只有 portal/ 讀得到）
                                                                  ▼
                                                   CloudFront distribution（唯讀、https、token、CORS）
                                                                  │  GET <DataBaseUrl>manifest.json
                                                                  │  GET <DataBaseUrl>releases/<id>/data/…
                                                                  ▼
  ┌──────── presentation plane ────────────────────────────────────────────────────────────────────┐
  │ React UI（Vite 靜態網站，Amplify Hosting）：執行時才決定資料從哪裡讀（config.json）             │
  │  頁尾顯示資料版本與來源；有另一個版本上線時提示重新載入                                        │
  └────────────────────────────────────────────────────────────────────────────────────────────────┘
```

三個平面各自部署、各自更新：

| 平面 | 內容 | 多久變一次 | 怎麼部署 |
| --- | --- | --- | --- |
| presentation | UI 的程式（`llmops-portal/web`） | 改畫面時 | Amplify（和現在一樣） |
| data | portal 的資料（schema、data、runs、config） | 每週、或手動 | 不需要部署：發布一個 release、切換指標 |
| research + control | agent、發布流程、排程 | 改研究邏輯或政策時 | `agentcore deploy`、兩個 CloudFormation stack |

**資料更新不會觸發 UI 的 build，UI 的部署也不會動到資料。** 兩邊唯一的約定是下一節的資料契約。

## 2. 資料契約（UI 與 pipeline 之間唯一的介面）

```
portal/
  manifest.json                         指標：目前上線的是哪一個 release。Cache-Control: no-cache
  releases/
    20261005T010000Z-3f9a1c2b/          一個 release：寫入後永不修改。Cache-Control: immutable（一年）
      release.json                      這一版的紀錄：檔案清單與 sha256、前一版、來源、實際的差異、每一項變更的理由與引文
      schema/llmops.schema.json
      data/catalog.json  lifecycle.json  litellm.json  requirements.json  sources.json
      data/digests/<日期>.json   data/models/<id>.json   data/internal/platform-inventory*.json
      runs/compat/*.json  runs/perf/*.json
      config/litellm.example.yaml
```

`manifest.json`（schema `portal-manifest/v1`）是 UI 原本 build 時產生的 `data/manifest.json` 的超集合：

| 欄位 | 用途 |
| --- | --- |
| `digests` `models` `compatRuns` `perfRuns` `hasInventory` `generatedAt` | UI 原本就在用的索引（頁面程式不用改） |
| `releaseId` `basePath` | 目前的 release，以及它的路徑（`releases/<id>/`） |
| `releaseSha256` | 這個 release 的 `release.json` 的 sha256：指標釘住檔案清單，檔案清單釘住每個檔案，所以改動 release 裡任何一個檔案都會被發現 |
| `publishedAt` `sequence` `previousReleaseId` | 發布時間、第幾次發布、前一版（rollback 的預設目標） |
| `source` | 這一版怎麼來的：`agent`（含 `proposalId`、`proposalSha256`、`runId`、`approvedBy`）、`manual`、`bootstrap`、`rollback` |
| `autoPublish` | 自動發布的暫停旗標（`{"paused": true, "by", "at", "reason"}`）。放在指標裡，所以「現在是不是暫停」和「切換指標」是同一次條件寫入 |
| `history` | 最近 60 次發布的摘要 |

為什麼是「指標 + 不可變的 release」而不是直接覆寫檔案：

- **一致性**：一次更新通常同時改好幾個檔案（lifecycle、sources、digest）。UI 先讀指標、再從同一個 release 讀所有檔案，
  所以不會讀到「一半新、一半舊」。已經打開的頁面繼續用它載入時的那一版，直到使用者重新載入。
- **原子切換與 rollback**：發布 = 寫好所有檔案後換掉一個小檔；rollback = 把指標指回舊的 release，幾秒內生效，不需要重跑研究或重新 build。
- **快取**：release 的檔案永不改變，可以讓 CDN 與瀏覽器快取一年；只有指標不快取。資料更新後不需要 invalidation。
- **稽核**：每個 release 的 `release.json` 記錄了改了什麼、依據哪一段引文、誰核准，頁尾的「變更紀錄」連結直接打開它。

**並行安全**：指標用 S3 conditional write（`If-Match` ETag）切換；release 的每個檔案與待核准的提案都用 `If-None-Match: *` 建立（已經存在就不覆寫）。
兩個 agent 提案同時發布時，後到的那一個會重新讀取上線中的資料、重做一次驗證再發布（最多四次），不會互相覆蓋。
手動 push 與 rollback 遇到同時發生的另一次發布則直接停下來，請操作者確認目前的狀態後再試。

**相容性規則**（UI 與資料各自部署，所以要有先後順序）：

- schema 的變更以「新增選用欄位」為原則。UI 必須容忍沒看過的欄位與缺少的選用欄位。
- 破壞性的變更分兩步：先部署能同時處理新舊格式的 UI，再發布新格式的資料。
- schema 本身（`schema/llmops.schema.json`）跟著資料走，放在每個 release 裡；agent 驗證提案用的是**上線中那一版**的 schema，不是它自己帶的。
- 改 schema 的順序：先 `portalctl pull`（讓 checkout 對齊上線的資料）→ 在 portal repo 改 `schema/types.ts` → `npm run schema` 產生
  `llmops.schema.json` → `portalctl push`（會用新 schema 驗證全部資料）。只改 schema 時可以不 pull：`portalctl push --only schema`。
- **新欄位要 agent 寫的時候，上線順序是 schema → Lambda bundle → agent。** 例：digest 項目與行動項目的 `topic`／`check`／`merges`。
  (1) portal repo 加上這三個欄位的 schema merge 之後，先 `portalctl push --only schema --portal ../llmops-portal`：agent 用上線中的 schema 驗證提案，
  schema 還不認得這些欄位時，帶著它們的 digest 會被 agent 與發布端的 schema 檢查擋下（`additionalProperties: false`）。
  (2) 再部署新的 Lambda bundle（`build_lambdas.py --upload` → 更新 pipeline stack）：發布端重新套用時檢查 `merges` 是不是先前項目的標題；
  舊的 bundle 不檢查，而 agent 不拿 `merges` 裡的日期對照引文，就是因為發布端會再確認一次。(3) 最後 `agentcore deploy`：新的 agent 才會寫這些欄位。

## 3. 一次更新怎麼走（state machine）

```
Init ─► Route ─┬─(input 有 action)──────────────────────────────┐
               └─► StartResearch ─► WaitForResearch(60s) ─► CheckResearch ─► ResearchDone
                                         ▲                                     │ running
                                         └─────────────────────────────────────┘
                                                                 accepted │        │ 其他（no_proposal …）
                                                                          ▼        ▼
                                                                       Publish ─► Notify ─► Outcome ─┬─► Done
                                                                                                     └─► NotPublished（rejected）
   任何一步出錯 ─► ReportFailure ─► NotifyFailure ─► Failed
                       └─(連報告都產生不出來)─► NotifyUnreported ─► Failed
```

| 步驟 | 做什麼 | 失敗時 |
| --- | --- | --- |
| StartResearch | 先檢查這次執行的 input（`action`、`mode`、`task`…：寫錯的話在這裡就停，不會等研究跑完才發現）和這個 function 自己的設定，再以 background 模式呼叫 AgentCore runtime，記下 run id 與結果會出現的位置。發布的設定（`PublishMode`、`AutoMaxOps`、`AutoAllowRemove`）屬於 Publish function：CloudFormation 參數只接受合法的值；有人手動把 function 的環境變數改壞時，是 Publish 這一步停下來（`ConfigError`），那次研究的結果仍然留在 `research/runs/`，設定修好後用 `portalctl publish s3://…/research/runs/<run>.json` 發布即可，不必重跑研究 | 不重試（避免重複啟動研究），直接回報 |
| Wait / CheckResearch | 每 60 秒看一次 `research/runs/<runId>.json` 出現了沒；超過 `ResearchTimeoutSeconds` 視為逾時 | 只讀不寫，所以服務層級的錯誤與逾時都會重試 |
| Publish | 對**現在上線的資料**重新驗證提案 → 發布、暫存待核准、或拒絕（下一節） | 服務層級的錯誤會重試；**重試到已經做完的那一次時，回報的是第一次的結果**（見下）。其他錯誤直接回報：指標還沒切換時資料維持原狀，通知會說明失敗是不是發生在發布的步驟 |
| Notify | SNS 通知：發布了什麼（實際的差異、理由與引文）、待核准（附核准指令）、被拒絕的原因 | 重試；仍然寄不出去時 execution 以 `PortalNotificationFailed` 結束（紅色、會觸發 alarm）：前一步已經做完，所以原因裡會請妳用 `portalctl status`／`pending` 確認結果 |

每一步的輸入輸出都是同一份 state（`input`、`run`、`research`、`publish`、`notification`、`error`），所以在 Step Functions 的
execution history 可以直接看到每一步拿到什麼、做了什麼決定。state 的大小有上限：不管提案多大，放進 state 的摘要都是截短過的。
被拒絕的更新以 `PortalUpdateRejected` 結束、流程錯誤以 `PortalUpdateFailed` 結束，兩者在 console 上都是紅色，並且都已經發過通知；
通知本身寄不出去的是 `PortalNotificationFailed`（沒有通知，靠 alarm 發現）。

失敗報告是 ReportFailure 這個 function 寫的；它不管 state 裡有什麼都會回一則訊息（input 裡只有半個 emoji 這類程式無法編碼的字元會先換成 `�`，
巢狀深到無法處理的 input 是一個被拒絕的請求）。如果連這個 function 都失敗或呼叫不到，state machine 自己會寄一則固定內容的通知
（主旨 `[LLMOps portal] FAILED: the failure report could not be written`，內文是一句英文說明加上那次 execution 的 ARN——這則訊息不讀 state 裡的任何東西）。

**同一個請求送到兩次只做一次。** Step Functions 在 Lambda 的回覆遺失時會重試同一步，這時發布（或核准、捨棄、rollback、resume）其實已經完成。
每一次 execution 有自己的識別碼（execution ARN 的雜湊，記在 release 的來源與決定紀錄裡）：同一個 execution 再送一次，拿到的是第一次的結果
——「已發布」連同當時的差異與 rollback 指令——而不是「沒有變更」或錯誤。另一個 execution 對同一份提案提出要求時，得到的是「已經由誰在什麼時候核准／捨棄」。

**最後一道保險**：每一次沒有成功的 execution（失敗、逾時、被中止）都會觸發一個 CloudWatch alarm，送到同一個 SNS topic。
平常它是跟在詳細通知後面的第二封信；只收到 alarm、沒有詳細通知時，代表流程連自己的錯誤都報不出來，要到 Step Functions 看那一次 execution。

**手動與排程走同一條路**：排程送 `{"request": {"kind": "weekly"}, "trigger": "schedule"}`；手動可以送任何 agent 接受的 request，
或送 `{"action": "approve" | "discard" | "rollback" | "pause" | "resume" | "publish", …}` 只執行 Publish 這一步，所以每個動作都有執行紀錄與通知。
操作者需要的權限集中在 pipeline stack 的 `OperatorPolicy`（output `OperatorPolicyArn`）：啟動、列出與查看 execution，讀 `portal/`、`pipeline/`、`research/`；
沒有任何寫入 bucket 的權限。

## 4. 發布的關卡（什麼會自動上線、什麼等人、什麼被拒絕）

**`auto` 模式下，不經過人就上線的只有兩種變更：**

1. **新增的內容**：一筆新紀錄、附加在後面的一則備註、多引用一個來源、一份新的 digest。
2. **既有紀錄上的日期、數字、版本改了**——這三種值可以逐字和引文比對，而且 agent 的驗證器已經確認過：值出現在一則引文裡、那一則引文講的是這個模型（不是同一頁的另一個模型）、引文所在頁面的來源層級夠高。
   算不算這一類，看的是**變更**而不是新值長什麼樣子：日期換成日期、數字換成數字、版本換成版本才算。一段文字、一個識別字串或一個分類被換成「看起來像日期」的值，是改寫，要等人。

除此之外，對一筆**已經存在**的紀錄做的任何事都等人核准：刪除、清空、改寫原有的文字、改狀態／分類／識別欄位、改可信度標籤、拿掉或換掉引用來源、改變清單的順序。
理由是這些事沒有一項能由程式對照引文判斷，而原有的內容是先前有人（或先前的研究）確認過的。下面兩張表是完整的規則；
這套規則經過四輪獨立檢查，仍然存在的限制列在 [known-issues](known-issues.md)。**template 的預設是 `PublishMode=review`**（每一份都等核准）；改成 `auto` 是妳的決定。

agent 的輸出只是**提案**。發布流程不信任它：用和 agent 相同的程式把提案重新套用在現在上線的資料上，再比對套用前後的資料。
**判斷依據是實際的差異，不是提案怎麼寫**：一個 `replace` 換上比較小的值就是刪除，換掉整個清單就是改寫每一筆，所以「會不會刪資料、
改了多少」都從差異算出來。通知與 `portalctl show` 最先列出的也是這份差異，其次才是提案自己的說法。

**一律拒絕（核准也沒有用，要重新研究）**

| 情況 | 理由 |
| --- | --- |
| run 的狀態不是 `accepted`，或 agent 的驗證報告有錯誤 | 沒有通過 agent 自己的驗證關卡 |
| 提案是依 agent 內建的 SAMPLE 資料產生的 | 不是針對上線中的資料（`PORTAL_DATA_URI` 沒設好） |
| 格式不對：欄位的形狀不符、`replace`／`remove` 沒有記錄原值、`add` 卻帶了原值、提案或 run 紀錄裡有 NaN 或 Infinity、id 不合規則 | 無法確認它是依目前的資料提出的，或寫出來的 JSON 瀏覽器讀不懂 |
| 太大：超過 500 個 operation、超過 2 MB、或裡面的值（文字、數字、陣列、物件全部算在內）超過 30,000 個 | 整個 portal 的資料大約兩萬個值；比它還大的不是一次研究的結果。這一條在任何處理之前就判斷：逐項處理這麼大的提案所需的記憶體會超過 Publish function 的上限（schema 錯誤同理：每個檔案只逐條列出前 200 個，其餘只計數） |
| 引文紀錄不像 agent 寫的：缺 https 網址、引文是空的、來源層級不是 `official`／`independent`／`community`、有不認得的欄位；operation 沒有引用引文，或引用了不存在的編號；提案記錄的資料版本（`portal.manifest`）不是 release id | 這些欄位會被存進 release、印在通知與 `portalctl show` 裡給核准的人看；它們不是 agent 寫出來的樣子時，整份紀錄就不可信 |
| `createdAt`／`today` 不是可以解讀的時間（含全形數字、結尾多一個換行）、`createdAt` 在未來、`today` 和 `createdAt` 差了一天以上 | 提案宣稱的時間會被寫進資料，而且所有和時間有關的規則都靠它 |
| 套用後有瀏覽器無法精確讀取的整數（超過 2^53 − 1）、日期欄位不是單純的 `YYYY-MM-DD`、或某個檔案大到 `push` 也不會接受（8 MB） | Python 存得進去，瀏覽器讀出來是另一個值 |
| 路徑中途用數字位置指到清單裡的某一筆（`/events/2/date`） | 位置只在讀取的那一刻有效；清單一變動，同一份提案改到的是另一筆。要用 id（`/events/<id>/date`） |
| 某一項變更的「原值」和上線中的資料不符 | 資料在研究之後被改過；套用會蓋掉別人的更新 |
| operation 建立在同一份提案裡前一個 operation 的結果上：改它剛新增或改過的東西、在清單被動過之後用位置指定 | 「原值」是對照今天的資料記錄的；這樣寫的話，顯示給人看的和實際被取代的就不是同一個東西 |
| operation 不是 `add`／`replace`／`remove`（沒有 `create`），或它的對象是 `sources.json`、既有的 digest、整個檔案、`asOf`／`generatedAt`／`schema` | 來源只能經由 `newSources` 新增、digest 只能整份新增、時間戳記由流程蓋；agent 的驗證器不會產生這些 operation |
| 新的 digest 的 `id` 不等於 `periodEnd`、期間在未來、或同一個 id 的 digest 已經存在 | digest 的檔名就是它的日期 |
| 新的 digest 的項目用 `merges` 列了一個不是先前 digest（期間結束日比它早）**同一區**項目標題的字串（行動項目：先前行動項目的 `action` 原文；比對前做和 portal Radar 相同的正規化） | `merges` 裡的日期不對照引文，因為它們應該是 portal 上已經有、上線時檢查過的標題；對不上的就是沒有人檢查過的文字，Radar 也找不到要合併的項目 |
| 套用後出現新的 schema 或跨檔檢查問題（含日期時間的格式、連結欄位不是單純的 `https://` 網址、`data/litellm.json` 裡 Gateway 頁面無法解析的版本） | UI 讀到會出錯，或連結會把人帶到別的地方 |
| 新增的紀錄和清單裡既有的一筆用同一個 key（`id`、`service`、`feature`、`alias`、`scope`、`endpoint`，release 的 `version`） | 頁面會顯示新的那一筆：名義上是新增，實際上改寫了既有的紀錄（包含團隊自己實測過的） |
| 改到研究資料以外的檔案（`runs/`、`data/internal/`、`config/`、schema） | 那些檔案由人與工具維護 |
| 改 `platformStatus`、新模型不是 `evaluating`、動到任何 `internal-test` 的事實（修改、搬移、新增、刪除） | 平台團隊的決定與內部實測，agent 不能代填 |
| 把 `litellm.status` 寫成或改離 `tested-*`、新增或修改 `measuredBy: internal` 的 benchmark、修改帶有內部實測的紀錄的識別欄位（`cloud`、`service`、`providerModelId`…） | 同上：這些欄位說的是「我們測過」，或決定了那個實測是對什麼量的 |
| 需要等待核准，但已經有一份 id 相同、內容不同的提案在等，或無法寫入待核准清單 | 待核准的提案不會被取代：別人看過的就是核准時發布的 |

處理提案的過程中任何沒有預料到的形狀，結果都是「拒絕並說明原因」，不會讓流程當掉。

時間戳記（檔案的 `generatedAt`／`asOf`、來源的 `accessed`、digest 的 `generatedAt`）一律由重新套用的程式蓋上，不採用提案裡寫的值，
而且**只會往後、不會倒退**：等了幾天才被核准的提案可以照常套用，它不會讓這段期間更新過的檔案看起來比較舊。

**通過檢查但等待核准（`pending`）**

| 情況 | 參數 |
| --- | --- |
| 發布模式是 `review`（部署設定，或這一次執行指定 `"mode": "review"`） | `PublishMode` |
| 自動發布處於暫停狀態（rollback 之後，或有人執行 `pause`） | `pause` / `resume` |
| 實際會刪除任何資料（一個欄位、清單裡的一項、整筆紀錄），或把原有的文字改成空的 | `AutoAllowRemove` |
| 改寫原有的文字（備註、摘要、說明、`replacement`…）。在後面**新增**一則備註不算 | 固定 |
| 修改既有紀錄的狀態、是／否、分類或識別字串（`event`、offering 的 `status`、功能支援、`providerModelId`、`tier`…），或在既有紀錄上新增這類欄位 | 固定：這類值沒有引文可以逐字對照 |
| 改變、新增或拿掉既有紀錄的可信度標籤（`verification`），不論調高或調低 | 固定 |
| 清單裡紀錄的順序變了（內容都還在），或新的紀錄、新的備註被放在既有的前面 | 固定：頁面顯示的是第一筆（例如第一張價格表）。新增的內容放在最後面才會自己上線 |
| 拿掉某筆資料原有的部分引用來源（換成別的來源也算）；調換既有引用的順序；改寫或刪除某個引用的備註 | 固定：引用來源只能從尾端增加 |
| 實際的變更超過 60 處，或提案超過 60 個 operation | `AutoMaxOps` |
| 單一個 operation 改寫了超過 12 個原有的值 | 固定 |
| 把某筆資料原有的引用來源全部拿掉 | `AutoAllowRemove` |
| 修改既有模型的識別欄位（`name`、`vendor`、`family`、`vendorModelId`） | 固定：`platformStatus` 是針對原本那個模型核定的 |
| 動到 `data/requirements.json`（需求、發布關卡、指標定義），包含只是更新它的 `asOf` | 固定 |
| 新增或修改了標示為 `derived` 的內容（由其他資料推算的值，沒有引文可以對照），包含這種紀錄底下的任何欄位，以及把標籤從 `derived` 改成別的 | 固定 |
| 把某個檔案標成「今天整份重新確認過」（`asOf`），或把某筆資料標成「今天對照來源確認過」（`lastVerified`），但這次研究沒有把它引用的來源頁面全部讀過——或提案沒有說它讀了哪些、說的數量和實際引用的不符 | 固定 |
| 把 `lastVerified` 設成提案當天以外的日期 | 固定 |
| 標成「今天讀過」（`accessed`）的來源裡，有提案引用的引文都不是出自它那個頁面的 | 固定：只有真的引用到的頁面才算讀過 |
| 提案產生的時間，比它要修改的檔案上次更新的時間還早 | 固定：那是別人更新之前做的研究 |
| 研究讀到的頁面或搜尋結果含有像是對 AI 下指令的內容（疑似 prompt injection）；或要發布的文字、提案的摘要、未能確認的項目、每一項的理由、引文紀錄的任何欄位裡有這種內容 | 固定 |
| agent 的驗證器在提案上留了任何旗標（`validation.flags`）：像指令的文字、沒讀過的網址、**某個值的引文講的是不是這個模型，程式拿不準**（`uncertainSubject`）、某個值沒有引文可以逐字對照（`uncheckedValue`）、digest 的項目這次沒有重新讀取來源或查無出處（`digestNotReread`／`digestUnsourced`），或這一版不認得的種類 | 固定：看的是有沒有旗標，不是訊息的措辭；旗標的內容讀不懂、或 `flags` 整個不是這一版認得的格式，也算有（清單在 [agent](agent.md) 第 2 節） |
| 要發布的文字（同上）裡有連結，而它不是 portal 的來源（既有的，或這份提案用 `newSources` 新增的），portal 上原本也沒有 | 固定 |

其餘的提案在 `auto` 模式下直接發布。核准（`approve`）只跳過「等待」，**不跳過驗證**：核准時會對當下上線的資料再驗證一次。
核准指的是**某一份內容**：通知裡的核准指令帶著提案的 sha256，待核准的提案一旦存下就不會被取代——另一份 id 相同、內容不同的提案自動發布時，
等待中的那一份原封不動留著；同一份提案同時被核准與捨棄時只有一個會成立。被拒絕的核准仍然留在待核准清單裡（附上原因），要用 `discard` 清掉。
被捨棄的提案可以重新送出再核准（重新發布那一次研究的結果）；已經上線的提案不能被捨棄（要撤回它的內容是 rollback 的事），rollback 之後才可以。

**agent 自己也擋**：上面兩張表的多數情況，agent 的驗證器會先處理，而且用的是同一份程式（重新套用、差異比對、標籤與時間戳記的判斷）。
會被當成錯誤退回給模型修正的：新增或改動的日期、數字、版本不在**同一則**講這個模型、層級也夠的引文裡（規則在 [agent](agent.md) 第 2 節；引文講的是不是這個模型、程式拿不準的時候不退回，而是接受並加上旗標，由發布端等人）；用了清單裡已經有的 key；
operation 建立在前一個之上；路徑中途用了位置；單一 operation 改寫太多值；直接寫蓋章欄位；`lastVerified` 不是當天、或它引用的來源一個都沒讀；
`asOf` 一個來源都沒讀；把引用來源清空或重複引用；文字裡出現這次研究從頭到尾沒看過的連結；team 的測試欄位；digest 的 `topic` 格式不對、`merges` 不是先前項目的標題。
通過但會讓提案等人的，驗證器會先警告模型（「這一項會等人核准」），並記在提案裡：`validation.flags`（像指令的文字、沒讀過的網址、對象不確定、沒有引文可對照的值、digest 沒有重新讀取或查無出處的項目），
讀了幾個來源在 `validation.asOfCoverage` 與 `validation.lastVerifiedCoverage`。
發布流程對這些紀錄的態度是**只會因為它們變得更嚴格**：差異是自己算的，連結是自己對照 portal 的來源判斷的（研究宣稱讀過哪些頁面，它不採信）；
agent 的紀錄只能讓提案多等一個人，不能讓它少等。

**rollback 會暫停自動發布**，而且是同一次寫入：下一次研究讀到的是回復後的資料，很可能再提出同一個變更；所以 rollback 之後所有提案都先等待核准，
直到有人執行 `resume`。rollback 不是開關：上線中的版本本身就是 rollback 的結果時，不指定目標的 rollback 會被拒絕（否則第二個人照做一次就把壞掉的版本放回去了）；
通知信裡的 rollback 指令也只對它提到的那一版有效。rollback 的目標必須是**上線過**的版本（發布紀錄裡有的）：發布時被另一次發布搶先的 release
完整地留在 bucket 裡、卻從未上線過，它的提案可能還在等核准——指到它等於用 rollback 發布了一份沒人核准的提案。確定要回到紀錄以外的版本時，
直接操作資料位置並加上 `--unlisted`。

**人與工具發布的檔案也要驗證**：`portalctl push` 發布測試結果、平台清單、手動修改時，同樣檢查 schema（`CompatRun`、`PerfRun`、
`PlatformInventory` 與研究資料的七種檔案）與跨檔規則；只拒絕「這次會帶進來的新問題」，已經在線上的問題不會卡住無關的更新。
push 是**逐檔三方合併**：以 checkout 當初 pull 下來的那個 release 為基準，checkout 沒改的檔案維持上線中的版本、只有 checkout 改過的採用 checkout 的、
兩邊都改過的是衝突（預設停下來）。所以舊的 checkout 不會把 agent 的更新蓋掉，別人剛發布的測試結果也不會被刪掉。
衝突用 `portalctl pull` 解決：它以同一個基準把上線的 release 合併回 checkout，兩邊都改過的 `data/` JSON 逐筆合併（`portal_pipeline/jsonmerge.py`，
紀錄依 `patch.record_key` 對齊），只有兩邊把同一個值改得不一樣才停下來、列出 JSON pointer；合併後再 push。
`--force`（衝突時以 checkout 為準）與 `--allow-invalid`（驗證沒過仍然發布）是兩個分開的開關。
兩個開關都不能發布一份缺少 UI 必要檔案的資料（schema、`sources`、`catalog`、`lifecycle`、`litellm`）：那不是「資料有問題」，是整個 portal 打不開。
checkout 裡**沒有被讀取**的東西（不是 UI 會讀的檔案、symlink）不算「checkout 把它刪了」：上線中的那一份維持原狀。

checkout 裡只有 UI 會讀的檔案會被發布（固定的清單：schema、`data/` 底下的 `catalog`／`lifecycle`／`litellm`／`requirements`／`sources` 五個 JSON、
digest、模型檔、內部清單、`runs/` 的結果、`config/litellm.example.yaml`）；其他檔案——包含 `data/` 底下別的 JSON——要明確用 `--include` 才會帶上去，
避免筆記、草稿或含有金鑰的設定檔跟著 release 出去。

## 5. 資料的 source of truth 換了位置

| | 以前 | 現在 |
| --- | --- | --- |
| 上線的資料在哪裡 | portal repo 的 `data/`，build 時打包進網站 | S3 上 `manifest.json` 指到的 release |
| 研究結果怎麼進來 | agent 提案 → PR → 人工 merge → Amplify rebuild | agent 提案 → pipeline 驗證 → 發布 release（或等待核准） |
| 人工修改 | 改 repo、開 PR | `portalctl pull` → 改 → `portalctl push`（或繼續用 PR 審核，merge 後 push） |
| 測試結果、平台清單 | 工具寫進 repo | 工具產生檔案 → `portalctl push --only runs,data/internal` |
| 歷史 | git log | `manifest.json` 的 `history`、每個 release 的 `release.json`、S3 versioning |

portal repo 的 `data/` 變成**工作副本與種子**：第一次用它建立第一個 release（`bootstrap`），之後要手動修改時先 `pull`
（pull 也是三方合併：不會覆蓋妳在 checkout 裡改過的檔案，兩邊都改過的會列為衝突；上線已經移除、而妳沒有改過的檔案會從 checkout 刪掉，
這樣 checkout 才真的等於它記錄的那個 release）。
如果希望資料的歷史也留在 git，可以排程執行 `portalctl pull` 後 commit（見 delivery plan 的後續項目）。

## 6. 安全模型

| 面向 | 做法 |
| --- | --- |
| 網頁是不可信的輸入 | agent 端：SafeFetcher（只允許 https、擋私有網段與 metadata IP）、引文必須逐字出現在抓回的頁面上、信任層級由網域決定、偵測像是對 AI 下指令的文字（英文與中文；全形、形似字母、隱形字元等寫法先還原再比對）。頁面的內文、標題、大綱、連結都掃；搜尋結果的標題、摘要、日期與網址也掃，命中的不給模型看，日期欄位只放行真的日期。這是啟發式的偵測，會漏、偶爾也會誤判，所以它只決定「要不要請人看一眼」，不是唯一的防線 |
| 模型輸出是不可信的輸入 | 發布端：不呼叫模型、不上網；重新套用與驗證；關卡從實際的差異判斷；只允許改研究資料；有疑慮的提案等人 |
| 通知訊息與指令列輸出 | 所有外來的文字（模型、網頁、提案的每個欄位、錯誤訊息）一律壓成單行、去掉控制字元與方向控制字元、截斷，無法偽裝成訊息裡給人執行的指令，也無法操縱終端機；subject 只有 ASCII |
| 最小權限 | runtime 角色：讀 `portal/*`（明確拒絕 `data/internal/` 與 `runs/`）、寫 `research/*`，自己的結果只讀得到 role 之間的交接紀錄 `research/handoff/*`（[daily-agents-design](daily-agents-design.md) 4.1）。Publish function：讀寫 `portal/*` `pipeline/*`、讀 `research/*`，不能刪除 `portal/` 底下任何東西。其餘 function 只有各自需要的動作；state machine 只能呼叫這四個 function 與發通知 |
| bucket | private、BlockPublicAccess、強制 TLS、SSE-S3、versioning；只有 CloudFront（OAC，限定這個 distribution）讀得到 `portal/*` |
| 資料位置的存取 | 網址第一段是 token（CloudFront Function 檢查後移除）；可再加 WAF（例如辦公室 IP allow list）。**token 是分享連結等級的保護，不是身分驗證**（見下） |
| 誰可以發布 | 有 `OperatorPolicy`（透過流程）或 S3 寫入權限（`--direct`）的人。發布紀錄裡的操作者名稱（`--by`）是自己填的，方便辨識，不是身分證明；要查證是誰做的，看 Step Functions 的 execution 與 CloudTrail |

**信任邊界在哪裡。** 發布流程防的是「模型被網頁帶偏、或自己出錯」：它不信任提案的內容，一切從重新套用的結果判斷。
它**沒有**防「研究紀錄本身被偽造」：能寫入 `research/` 的身分（runtime 的 role，或拿到同等權限的人）等於能提出任何提案——內容仍然要通過 schema、
保護欄位與上面每一道從差異算出來的關卡，但引文是不是真的出自那個頁面，發布流程無從查證。所以 `research/` 的寫入權限要當成「可以提案」的權限來管；
需要更高的保證時用 `PublishMode=review`，由人對照引文與來源。

**公開讀得到的東西。** `manifest.json` 與 `release.json` 隨資料位置公開給 UI 的使用者：發布紀錄的摘要、`--by`、`--reason`、`--note`、提案的理由與引文都在裡面。
不要在這些欄位放不該公開的內容。

**資料位置的保護等級要和 UI 一致。** UI 是公開網站時，token 網址就夠了。UI 放在 SSO 或密碼後面時，資料位置也要有對等的保護，
否則知道網址的人可以繞過登入直接讀 JSON（包含 `data/internal/platform-inventory.json` 的內部路由名稱）。做法由簡到繁：

1. **WAF IP allow list**（`WebAclArn` 參數）：只允許公司網路與 VPN 的出口 IP。
2. **同源代理**：讓 UI 的網域把某個路徑轉給資料位置（Amplify 的 rewrite、或同一個 CloudFront 的第二個 origin），
   UI 的 `dataBaseUrl` 寫成相對路徑（例如 `/portal-data/`）。資料請求和 UI 走同一層登入，token 留在伺服器端設定裡。
3. **同一個 distribution 加上登入**（Cognito／公司 IdP + Lambda@Edge，或內部 ALB + OIDC）。

UI 只發出不帶自訂 header 的 GET（simple request），不需要 CORS preflight；新增頁面時請維持這一點。UI 自己的訊息不會印出資料位置的網址。

## 7. 壞掉時會怎樣

| 情況 | 結果 | 使用者看到 | 處理 |
| --- | --- | --- | --- |
| 研究失敗、逾時、沒有提案 | 不發布 | 資料維持原狀 | 看通知；修正後重新啟動一次 |
| 提案被拒絕 | 不發布，原因寫在通知與 `pipeline/rejected/` | 資料維持原狀 | 多半是資料在研究期間被改過：重跑即可 |
| 發布到一半中斷（Lambda 逾時） | release 的檔案可能已寫入，但指標沒有切換 | 資料維持原狀 | 重跑；沒被指到的 release 不影響任何人 |
| 指標切換之後才出錯（例如清不掉待核准的紀錄） | 已經發布；錯誤只會是通知裡的一行提醒 | 新的資料 | 照提醒處理（通常是 `discard` 那一筆紀錄） |
| 發布了錯的資料 | — | 錯的資料 | `portalctl rollback`（幾秒），自動發布隨之暫停 |
| 某個檔案的格式讓頁面畫不出來 | — | 讀到那個檔案的頁面顯示錯誤訊息；導覽列與頁尾照常，其他頁面正常 | rollback，或修正後 push |
| 資料位置讀不到（token 錯、CORS、WAF、CDN 故障） | — | 每一頁顯示載入失敗，頁尾說明讀不到資料並自動再試；恢復後頁尾提示重新載入 | `portalctl verify --url …` 會指出是哪一項 |
| 有人直接改了 bucket 裡 release 的檔案 | — | 可能讀到被改過的內容 | `portalctl verify` 以 sha256 比對會發現（指標釘住 `release.json`，`release.json` 釘住每個檔案）；rollback 到完好的版本 |
| 流程連錯誤都報不出來（state 超過大小上限、被中止…） | 不發布 | 資料維持原狀 | CloudWatch alarm 會送到同一個 topic；看那一次 execution |
| 每週排程沒有執行 | 沒有新的發布 | 頁尾的發布日期停在舊的日期 | 每週應該收到一封通知；`portalctl executions` |

## 8. 幾個取捨

- **`auto` 是選項，預設是 `review`**：需求是全自動，而安全性來自上面兩節的關卡、頁尾明確標示「未經人工逐項審核」、
  每項變更都附引文、以及秒級的 rollback。`review` 就是「全自動研究 + 一行指令核准」；真實模型的研究品質還沒有驗證過，所以先這樣跑一段時間。
  `auto` 能自己發布的範圍刻意很窄（新增的內容，與日期、數字、版本的變更）：程式能對照引文確認的才交給程式，其他的留給人。
- **Step Functions 輪詢 S3，而不是等 callback**：agent 在 AgentCore 的 microVM 裡以 background 模式執行，結果寫到 S3 是它既有的介面；
  輪詢讓 pipeline 不需要改 agent，也不需要讓 agent 拿到呼叫 Step Functions 的權限。
- **一個 Lambda bundle 四個 function**：共用同一份發布邏輯與資料模型（和 agent 用的是同一份程式），避免兩邊驗證規則不一致。
  boto3 隨 bundle 一起打包，因為 S3 conditional write 與 `bedrock-agentcore` 都需要夠新的版本。
- **UI 用 patch script 而不是整包取代**：UI 還在持續修改；script 只動資料載入這一層（8 個檔案裡的 13 處，另外新增 4 個檔案：三個程式檔與 `public/config.json`），
  每一處只換掉它認得的那一段，認不得就停下來；每一項修改都可以單獨撤回。
- **資料位置用 CloudFront 而不是直接開放 S3**：bucket 保持 private，同時得到 https、快取、token、WAF 的掛載點，之後要加登入也在同一個位置。
