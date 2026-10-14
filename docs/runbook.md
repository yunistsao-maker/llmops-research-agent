# Runbook：日常操作與問題排除

> template 的預設是 `PublishMode=review`：每一份提案都等妳核准。`auto` 模式能自己發布的範圍、以及這套關卡仍然存在的限制，
> 分別在 [architecture](architecture.md) 第 4 節與 [known-issues](known-issues.md)。

所有指令都在專案根目錄執行。`portalctl` 是 `scripts/portalctl.py` 的簡稱：

```bash
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1       # stack 所在的 region（兩個都設：AWS CLI 與 boto3 才一致）
export PORTAL_DATA_URI=s3://<DataBucketName>/portal/          # data plane stack 的 PortalDataUri
export PIPELINE_STATE_MACHINE_ARN=<StateMachineArn>           # pipeline stack 的 StateMachineArn
alias portalctl='uv run --with-requirements scripts/requirements.txt python scripts/portalctl.py'
```

**動作經由哪裡執行**（每個動作開始前都會印出來）：

- 上面兩個環境變數都設定了：approve／discard／rollback／pause／resume 透過 state machine 執行，有執行紀錄、會發通知。
- 指令列上有 `--data`（或資料位置是本機資料夾）：一律直接操作那個位置，不會碰 state machine。本機測試因此不會誤打正式環境。
  這時待核准清單用的是那個資料位置旁邊的 `pipeline/`；環境變數 `PIPELINE_STATE_URI`（正式環境的待核准清單）**不會**套用在指令列指定的資料位置上，
  要用別的清單就明確加 `--state <位置>`。每個動作開始時印出的那一行會寫出它用的是哪一份清單。
  `PIPELINE_RESEARCH_URI`（這個部署的研究結果放在哪裡）也是同樣的規則：指令列指定了資料位置時不套用，要指定就用 `--research <位置>`。
- `--direct`：不經由 state machine，直接讀寫 S3。這是 pipeline 壞掉時的備援，需要 bucket 的寫入權限，而且不會發通知。
- 看狀態的指令（`status`、`pending`、`show`、`history`、`verify`）一律直接讀 S3。
- `run` 與 `executions` 只存在於 state machine：在其他動作會改成「直接操作」的情況下（指令列有 `--data`、資料位置是本機資料夾、或加了 `--direct`），
  它們會拒絕執行並說明原因，不會拿環境變數裡的 state machine 去配指令列指定的資料位置。確定就是那一個 state machine 時，明確加上 `--state-machine <ARN>`。
- `publish <本機檔案>`：state machine 讀不到本機檔案。在會經由 pipeline 的環境裡要加 `--direct` 才會執行——那是直接寫入資料位置，不受 stack 的 `PublishMode` 約束
  （預設仍然先送審，`--mode auto` 才會通過關卡後直接上線）。

`--data`、`--state`、`--state-machine`、`--research`、`--by`、`--direct` 寫在子指令前面或後面都可以。

**需要的權限**：pipeline stack 的 output `OperatorPolicyArn` 是為操作者準備的 managed policy，掛上去就能做這份 runbook 裡除了 `bootstrap`／`push`／`--direct`
以外的所有事：`states:StartExecution`、`ListExecutions`（state machine）、`DescribeExecution`（它的 execution），以及讀 `portal/`、`pipeline/`、`research/`
（`status`、`history`、`pending`、`show`、`verify`、`pull` 都只讀取）。`bootstrap`、`push`、`publish <檔案> --direct` 與其他加了 `--direct` 的動作直接寫 bucket，
需要另外給 `portal/*`、`pipeline/*` 的 `s3:PutObject`（和 Publish function 的 role 相同等級的權限，只給需要的人）。

`--by`（預設是登入名稱）、`--reason`、`--note` 會寫進 `manifest.json`／`release.json`，UI 的使用者讀得到：不要放不該公開的內容。
`--by` 是自己填的，方便辨識；要查證是誰做的，看 Step Functions 的 execution 與 CloudTrail。
這些參數裡有終端機解不出來的位元組（終端機的編碼不是 UTF-8）或只有半個 emoji 時，`portalctl` 不會送出去，而是直接說明並結束（exit code 2）。

## 每週會發生什麼

每週一 09:00（台北）排程啟動一次 execution：研究（上限 25 分鐘，`MAX_RUNTIME_SECONDS`）→ 驗證 → 發布或等待核准 → 一封通知。

通知的主旨都以 `[LLMOps portal] ` 開頭：

| 主旨 | 意思 | 要做什麼 |
| --- | --- | --- |
| `published <releaseId> (N files)` | 新的資料版本已上線；內文先列實際的差異（原值 → 新值），再列理由與引文 | 看一眼內容；有問題就 rollback（信裡附了只對這一版有效的指令） |
| `approval needed: <proposalId>` | 提案通過驗證，但需要人核准（原因寫在內文最前面） | `portalctl show <id>` → `approve` 或 `discard` |
| `no change (unchanged)` | 研究完成，沒有需要變更的內容 | 不用處理 |
| `no change (already_published)` | 這份提案的內容已經在上線的資料裡：另一次執行送了同一份提案，或有人核准了已經核准過的提案（內文會寫是誰、什麼時候） | 不用處理 |
| `research finished without a proposal (…)` | 研究結束但沒有可用的提案；內文有 agent 停止的原因 | 偶發可以忽略；連續出現看「問題排除」 |
| `update NOT published: …` | 提案被拒絕，資料維持原狀 | 看原因；多半重跑一次即可 |
| `FAILED: …` | 流程本身出錯。內文會說失敗的步驟，以及資料是否可能已經變更 | 看「問題排除」 |
| `FAILED: the failure report could not be written` | 流程出錯，而且整理失敗原因的那一步也失敗了（通常是 execution 的 input 裡有程式處理不了的內容）。這一封是 state machine 自己寄的：一句英文說明，加上那次 execution 的 ARN | 到 Step Functions 打開那個 execution 看是哪一步、什麼錯誤；`portalctl status` 確認上線的版本 |
| `rolled back to <releaseId>`／`automatic publishing paused`／`automatic publishing resumed`／`proposal discarded: <proposalId>` | 有人執行了對應的動作 | 確認是預期的操作 |
| `ALARM: …`（CloudWatch 的格式，不帶上面的開頭） | 有一次 execution 沒有成功。平常它跟在上面某一封詳細通知後面 | 只收到這一封、沒有詳細通知時：到 Step Functions 看那一次 execution，並用 `portalctl status` 確認上線的版本。錯誤是 `PortalNotificationFailed` 的話，代表動作已經做完、只是通知寄不出去：用 `portalctl status` 與 `portalctl pending` 看結果，再查 SNS topic |

**重試不會重複發布，也不會把結果說錯。** Step Functions 重試 Publish 這一步時（Lambda 的回覆遺失），同一次 execution 拿到的是第一次的結果：
已經發布的仍然通知「published」並附上當時的差異與 rollback 指令。

**一週內沒有收到任何通知，就是排程或通知出了問題**：`portalctl executions` 看最近的執行。

## 每天的「網站已更新」通知（ui-verify，選用）

`infra/ui-verify.yaml` 部署之後，除了上面每次 execution 的通知，每天還會多一封確認**網站實際顯示今天的資料**的信
（程式：`pipeline/portal_pipeline/ui_verify.py`，與 pipeline 同一個 Lambda bundle）：

| 主旨 | 什麼時候 | 要做什麼 |
| --- | --- | --- |
| `Daily update live: <日期> (<releaseId>)` | 一天一次：某次 Amplify 部署之後，網站 `data/radar.json` 的版本是今天，而且 `data/manifest.json` 記錄的 release 等於資料位置上線中的 release | 不用處理；內文列出今天新增與 P0 的行動項目、今天各次研究的結果 |
| `Daily update NOT complete: <日期>` | `DeadlineSchedule`（預設 11:30）時今天還沒有寄出上面那一封 | 內文列出缺什麼：Radar 還是舊日期（今天的 digest 沒發布：看 `portalctl pending`／`executions`）、release 不一致（Amplify 沒有重新 build）、網站讀不到 |
| `UI build FAILED (job <id>)` | 每次 Amplify build 失敗 | Amplify console 看那個 job 的 log；網站維持上一次成功的版本 |

「一天一次」靠 `pipeline/notified/ui-<日期>.json`（只能建立一次）。部署參數：`AmplifyAppId`、`AmplifyBranch`、`SiteUrl`（使用者打開的網址，不是資料位置）、
`NotificationTopicArn`、`StateMachineArn`（選用，內文附今天的研究結果）、`SiteAuthSecretArn`（網站有 basic auth 時，Secrets Manager 裡的 `user:password`）。
需要 portal 的 `tools/build_site.mjs` 在 `data/manifest.json` 寫入 `release` 與 `radarAsOf`（llmops-portal 2026-10-11 之後的版本）。

## 看狀態

```bash
portalctl status          # 上線版本、來源、自動發布是否暫停、待核准的提案、最近的發布
portalctl history         # 發布紀錄（manifest.json 保留最近 60 次；--limit N 只看前 N 筆）
portalctl show            # 上線中的 release 實際改了什麼、依據哪些引文
portalctl show <releaseId>
portalctl executions      # 最近的 execution 與結果
portalctl verify --url <DataBaseUrl> --origin <UI 的網址>     # 資料完整性 + 瀏覽器實際拿到的內容
```

`verify` 只給 `--url`（不帶 `--data`，也沒有設 `PORTAL_DATA_URI`）時不需要任何 AWS 權限，可以交給沒有帳號的人檢查「UI 讀不到資料」。
它的輸出不會印出 token。沒有任何東西可以檢查時（兩個都沒給）它會直接說，不會回報「全部通過」。

Exit code：0 成功 · 1 `verify` 有檢查沒過 · 2 參數錯誤 · 3 環境或輸入問題 · 4 沒有發布（被拒絕、待核准、或 pull 有衝突）· 5 流程執行失敗。

## 手動啟動一次更新

```bash
portalctl run --wait                                          # 等同每週排程：{"kind": "weekly"}
portalctl run --payload-json '{"kind":"focus","focus":["lifecycle"]}' --wait
portalctl run --payload-json '{"kind":"verify","claims":["claude-sonnet-4-5 在 Microsoft Foundry 的退役日是 2026-11-30"]}' --mode review --wait
```

- `--mode review`：這一次的結果即使通過所有檢查也先等待核准（只能調嚴，不能把 `review` 的部署調成 `auto`）。
- `--payload-json` 是 agent 接受的 request（`kind`、`focus`、`claims`、`urls`、`budget` …，見 README 第 6 節）。
- 不用 CLI 也可以：Step Functions console → state machine `<stack>-update` → Start execution，input 填 `{}`（每週研究）
  或 `{"request": {"kind": "focus", "focus": ["lifecycle"]}, "by": "eunice"}`。input 必須是 JSON 物件。
- `--wait` 等不到結果（例如沒有 `DescribeExecution` 的權限）時，execution 其實已經啟動了：不要重跑，用 `portalctl executions` 或等通知。

同一時間可以有多個 execution；發布時會各自對當下上線的資料重新驗證，後到的若和先到的改到同一個值，會被拒絕並說明原因。

## 核准或捨棄

```bash
portalctl pending                       # 列出待核准的提案與原因
portalctl show prop-20261005T010000Z-3f9a1c2b    # 先列實際會造成的差異，再列每一項的理由、引文與來源網址；還有 digest 全文、未能確認的事項
portalctl show prop-… --out p.json      # 存成檔案；用瀏覽器打開 scripts/review_proposal.html 再選這個檔案，可以逐項對照
portalctl approve prop-…                # 對當下上線的資料再驗證一次，通過就發布
portalctl discard prop-… --reason "日期要再確認"
```

- 核准的是**妳看到的那一份內容**：`approve` 會帶著提案的 sha256，內容對不上就不會發布。通知信裡的核准指令也帶著它。
- **每一則引文是頁面上完整的一列或一句**，底下另外列出它所在的標題（「位置」）與那張表的表頭（「表頭」）：核對時看的是「這一列的第幾欄是什麼」，
  不必回頭開網頁。等人的原因如果是「程式拿不準這個值是不是在講這個模型」（`uncertainSubject`）或「這個值沒有引文可以逐字對照」（`uncheckedValue`），
  內文會指出是哪一項：那就是這份提案最該看的地方。
- 從提案帶出來的文字（引文、理由、頁面標題）太長時會折行，接續的行以 `┆` 開頭：以 `┆` 開頭的行永遠是資料，不是工具自己印的指令。
- **`show` 顯示的是「現在核准會改到什麼」**：它把提案重新套用在當下上線的資料上再列出差異，每個值都完整印出（不截斷）。
  提案暫存之後資料如果變動過，看到的就是變動之後的結果；套用不上去時會說明原因，並改列暫存當時記下的差異。
- 核准不是跳過檢查：提案暫存期間資料如果被改過（另一次發布、手動 push），核准時會被拒絕。**被拒絕的提案仍然留在待核准清單裡**（`pending` 會顯示拒絕的原因）；
  用 `portalctl discard <id>` 清掉，再重跑研究。
- 對一份已經有結論的提案再下指令，得到的是那個結論：「已經由誰在什麼時候核准並發布（release …）」或「已經由誰捨棄」。被捨棄的提案要再提出，
  重新發布那一次研究的結果即可（`portalctl publish …`）；已經上線的提案不能捨棄，要撤回內容請用 rollback。
  rollback 之後，那份提案的內容已經不在線上：這時它如果又被送出來等核准，就可以捨棄了。
- 通知信裡也附了不需要這個 repo 的核准指令（`aws stepfunctions start-execution … --input '{"action":"approve",…}'`）。
- 不經過研究、直接送一份已經存下來的結果：`portalctl publish s3://<DataBucketName>/research/runs/run-….json`（經由 state machine；它只收到檔名、
  發布的是自己 `research/runs/` 底下的那一份，所以**別的位置的物件會被拒絕**——例如妳修改過、上傳到別處的複本。研究結果不在資料位置旁邊的 `research/` 時，
  用 `--research <位置>` 或環境變數 `PIPELINE_RESEARCH_URI` 指出來；環境變數只用在環境變數指定的資料位置上），或 `portalctl publish <本機檔案或 s3 物件> --direct`（直接讀那個檔案、寫入資料位置；預設先送去待核准，
  加 `--mode auto` 才會在通過所有關卡時立刻發布）。

## Rollback

```bash
portalctl rollback --reason "10/05 的 lifecycle 日期有誤"       # 回到前一個 release
portalctl rollback --to <releaseId> --reason "…"              # 回到指定的 release（portalctl history 查 id）
portalctl resume                                              # 確認沒問題後恢復自動發布
```

- 幾秒內生效：只是把指標指回舊的 release。已經開著的頁面在切回分頁時（或 5 分鐘內）會提示資料換了版本，重新載入後就是舊資料。
- **rollback 之後自動發布會暫停**（所有提案都等待核准），因為下一次研究很可能再提出同一個變更。暫停和切換指標是同一次寫入，中間沒有空窗。
  找出原因（來源頁面寫錯？agent 讀錯？）之後再 `resume`；需要的話先 `discard` 同樣的提案。
- **rollback 不是開關。** rollback 本身也是一次發布（history 會多一筆 `rollback`）；上線中的版本已經是 rollback 的結果時，不帶 `--to` 的 rollback 會被拒絕，
  因為「回到前一版」這時指的是剛被撤下的那一版。要換到別的版本就用 `--to` 明確指定。
- 通知信裡的 rollback 指令只對信裡提到的那一版有效：那一版已經不在線上時，它什麼都不做並說明原因。
- **目標必須是上線過的版本**（`portalctl history` 列出的）。bucket 裡可能有完整、卻從未上線過的 release（發布時被另一次發布搶先留下的；它的提案通常還在待核准清單裡），
  rollback 到它等於發布一份沒人核准的提案，所以會被拒絕。確定就是要那一版（例如它比紀錄保留的 60 筆更早）：先 `portalctl show <releaseId>` 確認內容，
  再 `portalctl rollback --to <releaseId> --unlisted --direct`。

只想暫停自動發布、不改資料：`portalctl pause --reason "…"`；研究照常執行，提案都等待核准。
暫停旗標放在 `manifest.json` 裡（`autoPublish`），`portalctl status` 會顯示是誰、什麼時候、為什麼暫停的。

## 手動發布（測試結果、平台清單、人工修改）

```bash
# 測試結果或平台清單：只處理這些資料夾，研究資料不動；不需要先 pull
portalctl push --portal ../llmops-portal --only runs,data/internal --dry-run
portalctl push --portal ../llmops-portal --only runs,data/internal --note "10/05 compat run"

# 人工修改研究資料：先 pull（把上線中的資料合併進 checkout），改完再 push
portalctl pull --portal ../llmops-portal
#   …編輯 data/*.json…
portalctl push --portal ../llmops-portal --note "修正 gpt-6-sol 的 context window"
```

- `push` 會先顯示會變更哪些檔案並詢問；在 CI 裡加 `--yes`。
- **push 是逐檔合併，不是整包取代。** 基準是 checkout 當初 pull 下來的那個 release（記在 `.portal-release.json`）：妳沒改的檔案維持上線中的版本，
  只有妳改過的採用 checkout 的；上線在這之間也改過的檔案是衝突，會停下來並列出檔案——不會悄悄蓋掉 agent 的更新。先 `pull`（會逐筆合併，見下）再 push；
  `--force` 是整個檔案以 checkout 為準，上線在那個檔案的其他更新也會被蓋掉。
- checkout 沒有來源紀錄時（從來沒有 pull 過）：只新增與取代，不刪除（要刪除上線中有、checkout 沒有的檔案要加 `--delete`），
  而且和上線的版本不同的研究資料檔會被當成衝突——這是為了不讓舊的 checkout 蓋掉 agent 的更新。`--only runs,…` 這種只發布工具產出的用法不受影響。
- `pull` 也是合併：**不會覆蓋妳在 checkout 裡改過的內容**。只有上線改過的檔案直接寫入；**兩邊都改過的 `data/` 底下的 JSON 逐筆合併**，例如
  `≈ data/catalog.json（合併：上線版本 +2 筆、本機 +1 筆）`：物件逐個 key、紀錄清單依紀錄的 key 對齊（`id`、`service`、`feature`、`scope`、`alias`、
  `endpoint`，軟體版本用 `version`——和 agent 的 operation 指定紀錄的方式相同）。只有一邊新增／修改／刪除的採用那一邊；兩邊新增的紀錄都留下，
  排在原有紀錄之後（上線的在前）；最上層的 `asOf`／`generatedAt` 兩邊都改時取較晚的時間。schema、`runs/`、`config/` 仍然整個檔案比較。
  **兩邊把同一個值改得不一樣**（一邊刪除、另一邊修改同一筆紀錄；沒有 key 的清單兩邊都改）才是衝突（exit code 4），會列出 JSON pointer 與兩邊的值，
  例如 `/events/azure-claude-sonnet-4-5-eol/date`，本機的版本原封不動。處理方式：把列出的位置在 checkout 裡改成上線的值再 pull 一次
  （要保留妳的值，pull 完成後再改回來、push）；或加 `--overwrite`，讓有衝突的檔案整個換成上線的版本。
  合併完成後 checkout 以剛 pull 的 release 為基準，合併進來的本機修改仍是「妳的修改」：下一次 push 才會發布。
  **有衝突時 pull 什麼都不寫**——其他檔案不更新，來源紀錄也不動（只做一半的話，下一次 push 會把沒更新到的檔案當成妳的修改）。先決定衝突的檔案再 pull 一次。
  `pull --dry-run` 預覽的結果會停下來時，exit code 同樣是 4。
  上線已經移除、而妳沒改過的檔案會從 checkout 刪掉（這樣 checkout 才等於它記錄的那個 release）；checkout 沒有來源紀錄時不會刪除任何檔案。
  **妳刻意刪掉的檔案不會被寫回來**（會列出「保留妳刪除的 N 個檔案」）：下一次 push 會把它們從上線的資料移除；要取回就加 `--overwrite`。
- **被略過的東西不算刪除。** checkout 裡不是 UI 會讀的檔案、以及 symlink（檔案或資料夾）不會被讀取，上線中對應的內容維持原狀——
  例如同事用 `--include` 發布過的 `config/notes.md`，不會因為妳的一次普通 push 而消失。要把它從上線的資料拿掉，從 checkout 刪除那個檔案再 push。
  被略過的只是那個路徑本身：旁邊多了一個 `runs/perf (copy)/` 這種資料夾，不會影響 `runs/perf/` 底下的新增與刪除。
- 少了 UI 必要的檔案（schema、`data/sources.json`、`catalog.json`、`lifecycle.json`、`litellm.json`）的發布一律被拒絕，`--force`／`--allow-invalid` 也不行。
- 發布前會驗證 schema 與跨檔規則，只拒絕「這次會帶進來的新問題」。確定要發布有問題的檔案才加 `--allow-invalid`（讀到它的頁面可能無法顯示）。
  `--force` 不會略過驗證，`--allow-invalid` 也不會略過衝突：兩個開關各管一件事。
- checkout 裡只有 UI 會讀的檔案會被發布（schema、`data/*.json`、digest、模型檔、`data/internal/platform-inventory*.json`、`runs/compat|perf/*.json`、
  `config/litellm.example.yaml`）。其他檔案會列在「略過」裡；真的要發布就用 `--include <路徑>`。
- `.portal-release.json` 是本機狀態（`bootstrap` 與 `pull` 會寫；`push` 之後 checkout 和新的 release 完全一致時也會更新），建議加進 portal repo 的 `.gitignore`。
- 改 schema：先 `portalctl pull` → 在 portal repo 改 `schema/types.ts` → `npm run schema` → `portalctl push`。只改 schema 時可以不 pull：
  `portalctl push --only schema`。新 schema 會用來驗證全部資料；UI 要先能處理新格式（見 architecture 第 2 節）。

## 調整設定

| 想改的事 | 怎麼改 |
| --- | --- |
| 排程時間、時區、暫停排程 | pipeline stack 的 `ScheduleExpression`／`ScheduleTimezone`／`ScheduleState` |
| 排程送出的研究內容 | `ScheduleRequest`（JSON，例如 `{"kind":"weekly","budget":{"searches":20}}`） |
| auto ↔ review | `PublishMode` |
| 自動發布的變更數上限、是否允許刪除 | `AutoMaxOps`、`AutoAllowRemove` |
| 通知收件人 | `NotificationEmail`，或直接在 SNS topic（output `NotificationTopicArn`）加訂閱 |
| 誰可以核准、rollback | 把 output `OperatorPolicyArn` 掛到對方的 user／role |
| 誰能從瀏覽器讀資料 | data plane stack 的 `DataAccessToken`、`WebAclArn`（`AllowedOrigins` 建議維持 `*`） |
| agent 的模型、預算、搜尋來源 | `agentcore/agentcore.json` 的 `envVars` → `agentcore deploy` |
| 發布邏輯（`pipeline/`） | 改程式 → 跑測試 → `build_lambdas.py --upload` → 用新的 `CodeKey` 更新 pipeline stack |

改 stack 參數的指令形式（沒有列出的參數沿用原值）：

```bash
aws cloudformation deploy --stack-name llmops-portal-pipeline --template-file infra/pipeline.yaml \
    --capabilities CAPABILITY_IAM --parameter-overrides PublishMode=auto
```

換 token：`DataAccessToken=<新的值>` 更新 data plane stack → 把新的 `DataBaseUrl` 設到 UI（Amplify 環境變數或 `config.json`）→ 重新部署 UI。
兩個步驟之間 UI 會讀不到資料，所以先準備好再連續做。

## 問題排除

| 症狀 | 可能原因 | 檢查與處理 |
| --- | --- | --- |
| UI 每一頁都是「載入失敗」，頁尾寫「無法讀取資料：…HTTP 403」 | token 不對；WAF 擋掉這個 IP；distribution 還在部署 | `portalctl verify --url <UI 設定的 dataBaseUrl>`；比對 stack 的 `DataBaseUrl` |
| 頁尾寫「連不到…（網路中斷，或資料位置不允許這個網站讀取：CORS／存取限制）」 | UI 的網址不在 `AllowedOrigins`；token 不對（CloudFront 的 403 可能不帶 CORS header，瀏覽器就只說連不到）；網路問題 | `verify --url … --origin <UI 網址>`；`AllowedOrigins` 要含 scheme、不含結尾的 `/` |
| 頁尾寫「不是有效的資料版本指標」 | `dataBaseUrl` 路徑不對，拿到的是網站的 fallback 頁面 | 確認網址結尾是 `/`，而且指到 `manifest.json` 所在的位置 |
| 頁尾寫「讀不到網站的 config.json」 | UI 自己的 `config.json` 回了錯誤（不是 404） | 看 Amplify 的部署與 rewrite 規則；`config.json` 不存在是正常的（404），回 5xx 才會這樣 |
| 某些頁面顯示「這個頁面無法顯示」 | 那些頁面讀到的檔案格式不符（通常是 `--allow-invalid` 發布的檔案），或 UI 比資料舊 | `portalctl verify` 會列出資料問題；rollback 或修正後 push |
| UI 沒有顯示新資料 | 頁面還開著舊版本（按右下角的重新載入）；發布其實是 pending 或 rejected | `portalctl status`；通知信 |
| 頁尾是「資料隨網站一起打包」 | UI 沒有設定資料位置 | Amplify 的 `VITE_DATA_BASE_URL`（要是真正的環境變數）或 `public/config.json`；改完要重新 build |
| `approval needed`，原因是「自動發布已暫停」 | 之前有 rollback 或 `pause` | 確認原因後 `portalctl resume`；這一份用 `approve` |
| `approval needed`，原因是連結、`derived`、來源沒有讀完、提案比資料舊、像指令的文字… | 提案通過了驗證，但其中有程式無法自己確認的部分（完整的清單在 architecture 第 4 節） | `portalctl show <id>` 對照那一項的引文與來源，再 `approve` 或 `discard` |
| `approval needed`，原因是「程式無法確定……講的是不是這個模型」或「沒有引文可以逐字對照」 | 引文那一列／那一句裡不只一個名稱、有程式不認得的名稱、沒有提到這筆紀錄的平台，或日期有兩種讀法；或那個值（公告 id、短寫的日期）本來就沒有東西可以對照 | 同上：內文指出的那一項，對照引文（含「位置」與「表頭」）確認。digest 裡的日期寫成 `10-20` 這種短寫時很常見——寫完整的日期就不會 |
| `update NOT published`：提案裡的值超過 30,000 個 | 不是 agent 寫得出來的大小（手動編輯或損壞的 run 紀錄） | 重跑研究；要發布的是手動修改，用 `pull`／`push` |
| `approval needed`，原因是「會改寫 portal 上原有的文字」「會修改既有資料的狀態、分類或識別欄位」「會改變可信度標籤」「會改變清單裡紀錄的順序」「會把原有的文字清空」「會拿掉…引用來源」 | `auto` 模式只自己發布新增的內容與日期、數字、版本的變更；這些是對既有紀錄的其他修改，設計上就要人看 | 同上。這類原因很常出現時不是故障：那一類的更新本來就走核准 |
| `FAILED: BadRequest`，原因是「mode 只能是 auto 或 review」，步驟是「啟動研究」 | execution 的 input 裡 `mode` 寫錯（例如 `manual`） | 改成 `review`（或拿掉）重新啟動；研究還沒有開始，沒有花費 |
| `PortalNotificationFailed`（只有 alarm，沒有詳細通知） | SNS 寄不出去（topic 被刪、權限、訊息被拒） | 動作已經做完：`portalctl status`、`portalctl pending` 看結果；修好 topic 之後不需要重跑 |
| `portalctl publish s3://…` 說「只能發布這個部署自己的研究紀錄」 | 那個物件不在這個部署的 `research/runs/` 底下；或資料位置是在指令列指定的，環境變數 `PIPELINE_RESEARCH_URI` 因此沒有套用（訊息會寫出來） | 要發布的是別處的檔案（例如修改過的複本）就加 `--direct`；研究結果放在別的位置就用 `--research <位置>`（或在只用環境變數時設 `PIPELINE_RESEARCH_URI`） |
| `portalctl rollback --to …` 說「不在發布紀錄裡」 | 那個 release 從未上線過，或比紀錄保留的範圍更早 | 見上面 Rollback 一節的 `--unlisted` |
| `push` 說「這次發布之後會缺少 UI 必要的檔案」 | checkout 裡那個檔案不存在，或是 symlink | 補上檔案（`portalctl pull --overwrite` 可以取回上線中的版本） |
| `configure_agent.py --check` 說兩個位置「不是同一個 stack 的」或「權限只給這兩個位置」 | `PORTAL_DATA_URI`／`OUTPUT_URI` 不是 data plane stack 的 output（不同 bucket，或不是 `portal/` 與 `research/`） | `configure_agent.py --stack <data plane stack>` 重新寫入 |
| `portalctl run`／`executions` 說它「是由 state machine 執行的」而拒絕 | 指令列有 `--data`、資料位置是本機資料夾，或加了 `--direct` | 拿掉 `--data`（兩者都用環境變數），或明確加上 `--state-machine <ARN>` |
| `update NOT published`：原值不符 | 研究期間資料被改過（另一次發布或手動 push） | 重跑一次 |
| `update NOT published`：依 SAMPLE 資料產生 | runtime 沒有設 `PORTAL_DATA_URI` | `configure_agent.py --stack …` 後 `agentcore deploy` |
| `update NOT published`：沒有記錄原值（before） | runtime 跑的是舊版的 agent | 重新部署 agent（README 步驟 4） |
| `FAILED: ResearchTimeout` | 研究超過 `ResearchTimeoutSeconds`；或 runtime 沒有把結果寫到 `research/` | `agentcore logs`；`configure_agent.py --check --stack …`（`OUTPUT_URI` 與 role 的 policy） |
| `FAILED`，步驟是「啟動研究」 | runtime ARN 不對、Lambda 沒有 `InvokeAgentRuntime` 權限、runtime 忙碌 | execution history 的 `error.Cause`；`agentcore status` |
| `FAILED: ConfigError` | runtime 的 `OUTPUT_URI` 和 pipeline 等結果的位置（`s3://<DataBucketName>/research/`）不一致；或有人手動改了 Lambda 的環境變數 | 訊息會寫出兩邊的位置：`configure_agent.py --stack …` 後 `agentcore deploy`。設定有疑問時流程寧可停下來，也不會當成 `auto`。如果是 Publish 這一步才停（研究已經跑完）：結果還在 `research/runs/`，把設定改回來後 `portalctl publish s3://<DataBucketName>/research/runs/<run>.json` 就能發布，不必重跑 |
| `FAILED`，原因含 AccessDenied | role 的權限和實際的 bucket／prefix 對不上 | 確認 pipeline stack 的 `DataBucketName` 是 data plane 的 bucket；看那個 Lambda 的 log |
| `FAILED`，原因含 `Runtime.ImportModuleError` | Lambda bundle 和執行環境不合（架構或 Python 版本） | 重新執行 `build_lambdas.py --upload`（預設打包 arm64／Python 3.13，和 template 一致）並更新 `CodeKey` |
| 連續 `research finished without a proposal` | 來源頁面改版讀不到；搜尋 Gateway 失效；預算太低 | `agentcore logs`；`(cd app/llmops_research && uv run --extra dev python -m pytest tests/test_live.py --live -v -rP)` |
| 沒有收到任何通知 | SNS 訂閱沒有確認；排程是 DISABLED | SNS console 的訂閱狀態；`portalctl executions`；stack 的 `ScheduleState` |
| `portalctl` 說「需要確認：加上 --yes」 | 不是在互動式終端機執行 | 先 `--dry-run` 看內容，再加 `--yes` |
| `portalctl` 的動作沒有送到 pipeline（印出的是「直接操作」） | 指令列有 `--data`，或沒有設 `PIPELINE_STATE_MACHINE_ARN` | 用環境變數設定兩者，或加 `--state-machine <ARN>` |
| `verify`：上線中的 release 不完整或被改過 | 有人直接改了 bucket 裡的檔案 | `rollback --to <完好的 release>`；用 S3 versioning 與 CloudTrail 找出是誰 |

execution 的細節：Step Functions console → state machine → 那一次 execution，每一步的輸入輸出都是同一份 state
（`input`、`run`、`research`、`publish`、`notification`、`error`）。Lambda 的 log 在 `/aws/lambda/<stack>-start-research` 等四個 log group。

## bucket 裡有什麼

| prefix | 內容 | 誰寫 | 保存 |
| --- | --- | --- | --- |
| `portal/manifest.json` | 指標（含自動發布的暫停旗標、最近 60 次發布的摘要，與最近 30 天每個台北日期結束時上線的 release：`versions`） | Publish function、`portalctl` | 舊版本 180 天（versioning） |
| `portal/releases/<id>/` | 不可變的資料版本：每一個 release 都是它當時內容的紀錄（`release.json` 記著來源、差異與引文） | 同上 | 永久，永不刪除（rollback 需要；`versions` 只是指向其中 30 天的名單） |
| `research/runs/`、`research/proposals/` | 每次研究的完整紀錄（提案、引文、抓過的頁面、token 用量） | agent | `ResultRetentionDays`（預設 365 天） |
| `pipeline/pending/`、`decided/`、`rejected/` | 待核准、已決定、被拒絕的提案 | Publish function、`portalctl` | 永久（檔案很小） |
| `artifacts/` | Lambda bundle | `build_lambdas.py --upload` | 永久；舊的可以手動刪 |
