# Research agent（AgentCore Runtime）

部署在 **Amazon Bedrock AgentCore Runtime** 的研究 agent：上公開網站查資料、驗證內容，產出「每一項都附證據、已通過 portal schema 驗證、
可以直接套用」的更新**提案**。它只讀 portal 的資料、只寫研究結果；要不要發布由 pipeline 決定（見 [architecture](architecture.md)）。

## 1. 一次 run 怎麼運作

```
 pipeline（StartResearch）／ agentcore invoke ／ scripts/invoke_agent.py      payload: {"kind":"weekly"} …
                                             ▼
          ┌──────────────── AgentCore Runtime（每個 session 一個 microVM）────────────────┐
          │ main.py  BedrockAgentCoreApp：POST /invocations（SSE 事件串流）· GET /ping    │
          │   └─ Strands Agent + Claude（Bedrock，global inference profile）              │
          │        ├─ lookup_portal / describe_schema ─► 上線中的 release（PORTAL_DATA_URI）│
          │        ├─ web_search ───────► AgentCore Gateway（web-search connector）       │
          │        ├─ fetch_page / find_in_page / read_page / list_links ─► SafeFetcher   │
          │        │                         （選用：AgentCore Browser 處理 JS 渲染頁）  │
          │        ├─ record_evidence ──► 逐字引用驗證（引文必須真的在抓回來的頁面上）    │
          │        └─ submit_proposal ──► Validator（schema·跨檔·證據·信任層級·保護欄位）│
          │                                  ├ 不通過 → 錯誤退回給模型，同一個 run 內修正  │
          │                                  └ 通過   → proposal JSON                     │
          └───────────────────────────────────────┬──────────────────────────────────────┘
                                                  ▼
       回傳給呼叫端（SSE 最後一個事件 type=result）＋ 存到 OUTPUT_URI（research/runs/<runId>.json）
                                                  ▼
                    pipeline 的 Publish 步驟：對上線中的資料重新驗證 → 發布／等待核准／拒絕
```

每日的 role（`role` 欄位）在 `OUTPUT_URI` 底下另外寫一筆交接紀錄 `handoff/<台北日期>/<HHMMSS>-<role>-<runId>.json`（摘要、`unverified`、
提案的 `leads`、`checked` 裡以 `not reached:` 開頭的項目），下一個 role 開始時把它們當成資料放進任務說明；讀寫失敗只記 log。
`portal_health()` 工具（`research/portal/health.py`）不連網，對載入的 portal 資料做整體檢查（catalog／deep dive／watchlist／
digest／lifecycle），是 `portal-reviewer` 的起點。詳見 [daily-agents-design](daily-agents-design.md) 4.1。

`PORTAL_DATA_URI` 指到資料位置（`s3://<bucket>/portal/`）時，agent 讀 `manifest.json` 指到的 release（`releases/<id>/schema` 與
`releases/<id>/data`），提案裡會記下它是依哪一個 release 產生的。沒有設定時讀的是內建的 SAMPLE 快照，這種提案 pipeline 一律拒絕發布。

提案的 id 是 `prop-<UTC 時間>-<8 碼>`，後面 8 碼由提案的內容算出來：同一秒完成的兩次研究不會撞名，對同一份資料提出的同一個變更認得出來是同一份。

## 2. 它的輸出經過哪些檢查

驗證不是靠模型自律，而是 `submit_proposal` 這道程式碼關卡：不通過的提案退回給模型在同一個 run 裡修正，通過的才會存檔。
這些規則經過四輪獨立檢查；仍然存在的限制在 [known-issues](known-issues.md)。

**核心規則：一項事實，一則引文。** 每個 operation **實際新增或改動**的日期、數字、版本，都要能在它引用的**同一則**引文裡找到，而且那一則引文要同時滿足：

1. **逐字出自 agent 實際抓回的頁面**（`record_evidence` 會比對；忽略排版與表格分隔線），並記下頁面的 SHA-256 與抓取時間。
   **引文是頁面上完整的單位**：模型貼上的片段會被擴成它碰到的整列表格、整句話或整個標題（很長的段落取碰到的那幾句），存下來、之後拿來判斷的是擴充後的文字，
   另外記下它所在的標題（`under`）與表格的表頭（`header`）。所以「只引一列的後半段，讓它看起來像另一個模型的值」這種引法不成立。同一段文字在頁面上出現不只一次時，
   `record_evidence(..., line=N)` 指定是哪一行（`find_in_page` 會列出行號）。
2. **講的是這筆紀錄的模型**，不是同一頁的另一個模型（下表「對象」）。
3. **所在頁面的來源層級夠高**：不低於這項事實的 `verification` 標籤；沒有自己標籤的位置有固定的下限（下表「層級下限」）。

一則只提到模型名稱的官方引文，加上一則帶著數值的論壇貼文，湊不成一項官方事實：三個條件要落在同一則引文上。

**程式拿不準的時候不猜。** 第 2 點的判斷有三種結果：是、不是、**不確定**。「不是」退回給模型；「不確定」的提案照樣成立，但會帶上旗標，
發布端看到旗標就不自動發布，改成等人核准（下表「對象」）。同樣的做法也用在程式沒有東西可以逐字對照的值上（`flags.uncheckedValue`）。

| 檢查 | 做法 |
| --- | --- |
| 來源層級 | official > independent > community，由**實際抓到的網址**（重新導向之後、路徑正規化之後）依內建的規則決定，不看模型怎麼說。規則指的是廠商自己寫的頁面：同一個網域上任何人都能發文的地方（GitHub issue／discussion、Microsoft Q&A、Tech Community 討論區、AWS Marketplace 的上架頁、PyPI、論壇）是 community。**規則以主機為準**：一條規則指的是那個主機（與它的 `www.`）；子網域要規則明講才算（`*.anthropic.com`），而且就算明講，名字像公開發文區的子網域（`community.`、`forum.`、`discuss.`、`answers.`、`marketplace.`、`wiki.` …）也不算。路徑規則不分大小寫；路徑裡有 `//`、`;參數`、或把一般字元編碼過（`%6d`）的網址可以抓來讀，但一律算 community。路徑裡有 `.`／`..` 區段（含編碼過的寫法）、反斜線或編碼過的 `/` 的網址不抓：那樣的網址寫的和伺服器實際收到的不是同一個路徑。portal 引用過的頁面沿用它登記的類型，但只限**那一頁**，不是整個網站。公司自己認定的官方來源用 `EXTRA_OFFICIAL_DOMAINS` 加（第 4 節） |
| 對象 | 模型名稱整個比對（`Claude Sonnet 5` 不是 `Claude Sonnet 5.5`，`gpt-4o` 不是 `gpt-4o-mini`，`Claude 3 Haiku` 不是 `Claude 3.5 Haiku`）。判斷以引文的每個單位（一列、一句）為準，結果有三種。**是**：表格的一列裡，值在這個模型自己的名稱後面、而且前面沒有別的名稱；一句話裡，這個模型是值之前唯一被點名的；用頓號或 and 並列的名稱共用後面的值（`A and B retire on D`）；引文本身沒有名稱時，看同一段上一個只講這個模型、而且沒有自己數值的單位，再看所在的標題（由內而外；更新紀錄的日期或版本可以寫在標題上），最後是頁面標題。**不是**（退回）：值屬於另一個模型；`migrate from X to Y before D`、「改用 Y」這類句子裡的 Y 是替代品，不是對象。**不確定**（接受，但帶 `flags.uncertainSubject`，發布時等人）：值前面有兩個不同的名稱而看不出誰是誰；名稱後面接著程式不認得的字（`GPT-4o Audio Preview` 不當成 `gpt-4o`）；值前面有一個像是模型名稱、但 portal 不認得的字；紀錄屬於某個平台（Bedrock／Foundry／Vertex…）而引文、標題與頁面的主機都沒有提到那個平台；價格引文的標題或同一列寫著 batch／priority 這類另一種計價；日期有兩種讀法（`11/10/2026`）。網址的 query 與 fragment 不算對象的依據，標題就是網址本身的頁面（JSON、純文字）也不算。新紀錄的各個名稱欄位（`modelName`、`providerModelId` …）必須是同一個模型。紀錄沒有可用的名稱（lifecycle、catalog、deep dive）直接退回 |
| LiteLLM 的欄位 | `data/litellm.json` 與 catalog 模型的 `litellm` 區段，對象是 **LiteLLM 這個產品**：引文、它的上下文或頁面的主機要點名 LiteLLM，否則不算數（AWS 頁面上的 `Boto3 version 1.40.0`、`Requires Python 3.9.2` 都不是 LiteLLM 的版本）；同一句裡別的軟體和它後面的版本號歸那個軟體；`litellm-proxy-extras` 這類名稱更長的套件算「不確定」。新的安全公告，id 不是公開的公告編號（CVE、GHSA、PYSEC、OSV …）時等人；版本欄位寫的不是一個版本（例如 `*-stable`）時也等人（兩者都是 `flags.uncheckedValue`） |
| 層級下限 | 沒有自己 `verification` 的位置：`data/litellm.json` 的建議版本、最低基準、支援的版本線、release、要避開的版本要官方頁面；安全公告要公告頁面或官方頁面；catalog 模型層級的事實要官方頁面，benchmark 至少獨立來源；deep dive 的 onboarding、API 相容性、可用性矩陣、breaking changes、與前代比較、安全與合規要官方頁面，其餘至少獨立來源 |
| 標籤 | 新紀錄的 `verification` 不能高於它引用的來源；調高既有的標籤，等於重新主張它底下每一個日期與數字，全部要在夠高層級的引文裡找得到。`conflicts[]` 的日期、`replacement` 的模型同樣要出自講這個模型的引文 |
| 版本與數字 | 版本照套件索引與 release note 的寫法辨認（`v1.102.1`、`1.103.0rc1`、`v1.83.10-stable`、`main-v1.104.0-stable`、`1.102.x`），**整串**比對，包含後面每一個標記：`1.40.0foo` 不是 `1.40`，`…-stable.patch.1` 不是 `…-stable.patch.9`。讀版本之前先拿掉模型名稱、id、金額與日期（`Claude Sonnet 5.5` 不是 v5.5，`$1.50` 不是 1.50）。`data/litellm.json` 的版本欄位另外要符合 UI 的 Gateway 頁面解析的格式。價格可以由「每千 token」換算成「每百萬」，僅限價格欄位，而且只有和「每千 token」寫在一起的那個數字（同一個子句，或那一欄的表頭）才換算；其他單位（每十萬 token）不換算。模型名稱、id、日期裡的數字不算「引文裡的數字」，註腳記號（`$3¹`）、刪除線裡的舊值、時間（`00:00`）與 HTTP 狀態碼也不算 |
| 文字裡的日期與金額 | 備註、摘要這類文字裡的日期與金額要在引文裡，或是這筆資料原本就有的值（當天的日期除外）。讀之前先還原全形字、隱形字元與各種連字號；認得的寫法：`2026-11-30`、`2026/11/30`、`30.11.2026`、`30-Nov-2026`、`November 30, 2026`、`30. November 2026`、`2026年11月30日`、`2026년 11월 30일`、民國 `115年11月30日`；金額 `$3.75`、`NT$120`、`3.75 美元`、`120 元`、`EUR 3` 等。沒有年份的日子：寫成文字的（`3 月 31 日`、`March 31`）要有出處，否則退回；純數字的短寫（`10-20`）沒有出處時接受，但發布時等人（`flags.uncheckedValue`）——日期請寫完整的 `YYYY-MM-DD` |
| 推算的值 | 標成 `derived` 的事實不需要引文，但 rationale 要寫出算式與輸入，而且發布時一律等人核准；`derived` 只豁免它自己那一項事實 |
| 衝突處理 | 官方頁面彼此不一致時 `date` 取最早（強制），其餘記進 `conflicts[]`（`conflicting` / `superseded`）。改了日期卻沒有把舊日期留成 `superseded` 只是警告，提案仍然成立 |
| 引用來源 | 引用的來源必須是這個 operation 的引文所在的頁面；一筆資料原有的引用來源不能被清空，也不能重複引用同一個來源 |
| 清單 | 新增的紀錄不能用清單裡已經有的 key（`id`、`service`、`feature`、`alias`、`scope`、`endpoint`，release 的 `version`）：頁面會改顯示新的那一筆，等於改寫了舊的。把新紀錄插在既有紀錄的前面會收到警告，發布時等人（很多頁面顯示的是第一筆）；單純調換順序退回。引用來源（`sources`）只能從尾端增加：調換順序、改或刪除引用的備註都等人 |
| digest | 至少要有一則項目，而且每一則都要引用來源。項目裡的日期、金額與「幾月幾日」要出自它引用、而且這次**真的讀過**的頁面的引文——並且那則引文講的是**這句話說的那個模型**（digest 自己的句子用同一套對象規則來讀：「X 將於 D 下架（Y 同日）」說的是 X，旁邊提到 Y 不會讓 Y 的日期變成 X 的）——或者是 portal 上那個模型的紀錄原本就有的值（改前或改後）。headline 與 `actionItems` 只能重複各項目已經成立的日期與金額（同一個模型），或 portal 上那個模型的紀錄；`actionItems[].due` 是團隊自己訂的期限，不檢查；`relatedModelIds` 必須是 catalog 裡的模型 id。`unverified` 裡的日期要在這次讀過的頁面、顯示過的搜尋結果、或它提到的模型的紀錄裡。程式判斷不了的情況不退回，而是讓整份提案等人，各有旗標：引用的頁面這次一頁都沒讀（`digestNotReread`）、整份 digest 沒有項目或 `unverified` 的日期查無出處（`digestUnsourced`）、引文沒有點名句子裡的模型或句子提到不只一個模型（`uncertainSubject`）、短寫的日期沒有出處（`uncheckedValue`）。每一則項目與行動項目可以帶三個**標記**：`topic`（它講的主題，同一個主題每天寫成同一個，portal 的 Radar 把同一個主題的項目合成一張卡：`<area>:<subject>[:<aspect>]`，小寫，subject 用 catalog 的模型 id、litellm、CVE 編號…，例如 `pricing:claude-sonnet-5-5`；格式不對退回）、`check: true`（「查過、沒有變化」的涵蓋紀錄，不是新聞；一樣要引用來源）、`merges`（這一則更新、併入的**先前**項目：期間結束日比它早的 digest 裡**同一區**項目的標題，行動項目則是先前行動項目的 `action` 原文，最多 10 個；以 Radar 的正規化（全形半形、大小寫、標點與空白、「【… 補充】」標記）比對後必須完全相同，否則退回，發布端重新套用時也會再檢查一次）。標記不是句子：`merges` 裡的日期不再對照引文（它們是 portal 上已經有、上線時檢查過的標題），`topic` 提到的模型也不算這一則項目提到的模型 |
| Schema + 跨檔 | 套用後的資料必須仍通過 portal 的 JSON Schema（draft-07、`additionalProperties:false`）與跨檔檢查；只比對「新增」的問題。連結欄位必須是單純的 `https://` 網址；日期欄位必須是單純的 `YYYY-MM-DD`（結尾多一個換行也不行）；數值不能是 NaN／Infinity 或超過瀏覽器能精確表示的整數 |
| 一個 operation 一件事 | 單一個 `replace` 改寫超過 12 個原有的值會被退回，要求拆成一個欄位一個 operation。`replace`／`remove` 的對象必須是現在就存在的資料，不能接在同一份提案前面的 operation 後面。**路徑中途不能用數字位置**（`/events/2/date`）：要用 id（`/events/<id>/date`），位置只有在最後一段才可以用（`/notes/0`、`/notes/-`） |
| 提案的大小 | 一份提案最多 80 個 operation、600 KB、30,000 個值（文字、數字、陣列、物件全部算在內；整個 portal 的資料大約兩萬個）。超過的不逐項檢查，直接退回並請模型只改研究涉及的紀錄 |
| 原值一定留下 | 每個 `replace`／`remove` 都記下它取代的值（`before`，JSON null 也算）；`add` 沒有原值，帶了就是錯的。發布時用它確認提案是依現在的資料提出的 |
| 時間戳記由程式蓋 | `asOf`、`generatedAt`、`schema` 不能用 operation 寫，來源的 `accessed` 與 digest 的 `generatedAt` 也由程式蓋，而且只會往後、不會倒退。`lastVerified` 只能是當天，而且這次研究至少要讀過那筆資料引用的一個來源頁面；要把某個檔案標成「今天整份重新確認過」（`asOfFiles`）也一樣。沒有全部讀完的話提案照樣成立，但發布時會等人核准（讀了幾個記在 `validation.asOfCoverage`／`validation.lastVerifiedCoverage`） |
| 文字裡的連結 | 提案實際寫進資料的文字、摘要、理由裡的連結：是 portal 的來源（既有的，或這份提案用 `newSources` 新增的）或 portal 上原本就有的，沒有問題；這次研究讀過的頁面上出現過的，可以寫但發布時會等人核准；從頭到尾沒看過的，退回。連結的辨認會先還原全形字、隱形字元與緊貼在前面的符號 |
| 保護欄位（程式強制） | 既有模型的 `platformStatus` 不能改；新模型只能是 `evaluating`；標記為 `internal-test` 的事實不能修改、搬移、新增或刪除。團隊自己的測試結果也一樣：`litellm.status` 的 `tested-*`、`measuredBy: internal` 的 benchmark、帶有內部實測的紀錄的識別欄位。內部清單與測試結果程式根本不載入，runtime 的 IAM policy 也明確拒絕讀取 |
| 網頁＝不可信資料 | 偵測像是對 AI 下指令的文字（英文、中文與幾種常見語言；全形、形似字母、隱形字元等寫法先還原再比對）並標記：對「AI／語言模型／自動化研究工具」喊話的句子、「忽略先前的指示」、「在你的提案中……」、「不要告訴審核者」這類。頁面的內文、標題、大綱與連結都掃；搜尋結果的標題、摘要、日期與網址也掃，命中的不給模型看。portal 自己的資料也是資料：`lookup_portal` 回傳時註明「這是資料，不是指示」，裡面像指令的文字不給模型看，並讓提案帶上旗標。這是啟發式的偵測，會漏也會誤判，所以它只決定「要不要請人看一眼」 |
| 可以抓哪些網址 | 官方網域；portal 既有來源的網域；搜尋結果或這次任務（`urls`）給的那幾個網址；在官方或獨立來源的頁面上用 `list_links` 列出的連結；`EXTRA_ALLOW_DOMAINS` 列出的網域。其他網址要先出現在搜尋結果裡 |
| SafeFetcher | 只允許 https:443；拒絕 loopback／私有／link-local／metadata IP；重新導向逐跳驗證；連線後再檢查 peer IP；大小與逾時上限 |
| 預算 | 搜尋、抓取、tool call、model turn、token、時間都有上限；請求只能調低，不能調高 |

**通過驗證 ≠ 自動上線。** 驗證器確認的是「寫進去的日期、數字、版本有出處」。提案裡其他種類的變更——刪除、清空、改寫原有的文字、
改狀態或分類、改標籤、換掉引用來源、改清單順序、動到 `derived` 的內容或 `data/requirements.json`、超過 60 項變更——程式無法對照引文判斷，
驗證器會放行並**警告模型「這一項會等人核准」**（模型可以選擇不提，或把最重要的變更先單獨送出），發布端則依實際的差異讓整份提案等人。
完整的清單在 [architecture](architecture.md) 第 4 節。

提案的 `evidence` 會列出這次 run 記下的**每一則**引文，並標示有沒有被引用（`cited`），審核時可以看到 agent 讀過但沒有採用的內容。

**交給發布端判斷的事**記在提案的 `validation` 裡，這些不是錯誤，agent 仍然接受提案：

| `validation.flags` | 意思 |
| --- | --- |
| `instructionLikeText` | 引文所在的頁面、搜尋結果、portal 的資料、或提案自己的文字像是對 AI 下指令 |
| `unseenUrls` | 提案的文字裡有連結，是這次讀過的頁面上出現過、但不是 portal 的來源 |
| `uncertainSubject` | 某個值的引文講的是不是這個模型（這個平台、這種計價），程式拿不準 |
| `uncheckedValue` | 某個值沒有可以逐字對照的引文：不是公開編號的公告 id、不是版本的版本欄位、沒有出處的短寫日期 |
| `digestNotReread` | digest 的某一則項目，引用的頁面這次一頁都沒讀（只是重複 portal 既有的內容） |
| `digestUnsourced` | digest 沒有任何項目，或 `unverified` 裡的日期查無出處 |

另外還有 `asOfCoverage`／`lastVerifiedCoverage`（標成「今天確認過」的檔案或紀錄，引用的來源讀了幾個）。
發布端看到**任何一種旗標**——包括它不認得的種類——就不會自動發布，改成等人核准，通知與 `portalctl show` 會列出每一項的原因。
文字裡的連結、實際的差異則由發布端自己重新判斷一次，不採用 agent 的說法（見 [architecture](architecture.md) 第 4 節）。

## 3. 呼叫方式

平常由 pipeline 呼叫（`portalctl run`、每週排程）。要直接呼叫 agent、只看結果不發布：

```bash
# 只查一個來源，不存檔
uv run --with-requirements scripts/requirements.txt python scripts/invoke_agent.py --payload-json \
  '{"kind":"prompt","instruction":"核對 Foundry 上 claude-sonnet-4-5 的退役日","urls":["https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirement-schedule"],"dryRun":true}'

# 驗證一組說法，把結果存成檔案（用瀏覽器打開 scripts/review_proposal.html 再選這個檔案來看）
uv run --with-requirements scripts/requirements.txt python scripts/invoke_agent.py \
  --payload-json '{"kind":"verify","claims":["claude-sonnet-4-5 在 Microsoft Foundry 的退役日是 2026-11-30"]}' --out verify.json
```

最簡單的是 `agentcore invoke "weekly"`（送出 `{"prompt":"weekly"}`）。

| payload 欄位 | 說明 |
| --- | --- |
| `kind` | `weekly`（預設：整週巡檢＋weekly digest）· `focus`（只查指定範圍）· `verify`（驗證一組說法）· `prompt`（自由指示） |
| `prompt` / `instruction` | 文字指示；單獨一個 `weekly` 會被當成 `kind=weekly` |
| `focus` | `lifecycle` · `litellm` · `pricing` · `new-models` · `regulatory` |
| `claims` | `verify` 用，1–40 項要驗證的說法 |
| `urls` | 最多 30 個 https 網址，當作起點（視為可抓取） |
| `periodStart` / `periodEnd` | digest 期間；預設從最新 digest 的隔天到今天 |
| `includeDigest` | weekly 預設 true；若 portal 已有結束日 ≥ 本次 `periodEnd` 的 digest（digest 的 id 就是 periodEnd，重複的無法套用），agent 會被告知不要再產生 |
| `dryRun` | 只回傳結果，不存檔（`portalctl run` 與排程不接受：沒有存下來的結果可以發布） |
| `background` | 立即回 `accepted`，結果寫到 `OUTPUT_URI`（與 `dryRun` 互斥；pipeline 用的就是這個模式） |
| `budget` | `{"searches":10,"fetches":15,"toolCalls":80}`，只能調低部署上限 |

**事件串流**（`text/event-stream`，每個事件是 `data: {json}`）：`run_started` → `text` / `tool_call` / `tool_result` → `heartbeat`（每 15 秒）→
最後一個 `result`（`status`：`accepted` · `no_proposal` · `error`；含 `proposal`、`report`、`evidence`、`stats`）。背景模式只有一個 `accepted` 事件。
每個事件另外帶 `event.contentBlockDelta.delta.text`，所以 `agentcore invoke` 直接顯示可讀的進度。

**注意**：同步 run 需要連線一直開著，用戶端斷線會取消這次研究；長任務請用 `background`。每次呼叫都用新的 `runtimeSessionId`（新的 microVM），
同一個 session 預設最多同時 2 個 run。

## 4. 環境變數（`agentcore/agentcore.json` 的 `envVars`）

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `MODEL_PROVIDER` | `bedrock` | `bedrock` · `anthropic`（直連 API，需 `ANTHROPIC_API_KEY` 與 `strands-agents[anthropic]`）· `scripted`（離線測試替身） |
| `MODEL_ID` | `global.anthropic.claude-sonnet-5-5` | Bedrock inference profile id（不要設 temperature／top_p） |
| `AWS_REGION` | `ap-northeast-1` | Bedrock、Gateway、S3 用（在 runtime 裡由平台設定） |
| `PORTAL_DATA_URI` | `bundled` | `s3://bucket/portal/`（資料位置；pipeline 需要這個）· 本機資料夾（資料位置或 portal repo）· `bundled`（內建 SAMPLE）。用 `scripts/configure_agent.py --stack <data plane stack>` 設定 |
| `OUTPUT_URI` | 空 | `s3://bucket/research/` 或資料夾；背景模式必填。同上，由 `configure_agent.py` 設定 |
| `WEB_SEARCH_GATEWAY_NAME` | 空 | Gateway 名稱（通常只需要這個；也可用 `_ID` / `_ARN` / `_ENDPOINT`、`WEB_SEARCH_TARGET`）。未設定或不可用時退回內建的官方頁面清單 |
| `ENABLE_BROWSER` | `false` | 以 AgentCore Browser 處理 JS 渲染頁。打開時還需要兩件事：`additionalPolicies` 加上 `iam/browser-policy.json`（相對於 `app/llmops_research/`），以及安裝 `browser` extra（`playwright`） |
| `EXTRA_ALLOW_DOMAINS` | 空 | 額外允許直接抓取的網域（逗號分隔） |
| `EXTRA_OFFICIAL_DOMAINS` | 空 | 額外視為**官方來源**的位置（逗號分隔），兩種寫法：**主機**（`notices.example.com`，連同它的子網域）——只對內建規則不認得的主機有效，不能用來推翻內建規則對某個主機的判斷；**主機加路徑**（`github.com/acme/notices`）——只涵蓋那個路徑底下，而且優先於內建規則。列在這裡的頁面可以支持 `official` 的事實：只放由該廠商／單位自己發布內容的位置 |
| `MAX_SEARCHES` / `MAX_FETCHES` / `MAX_TOOL_CALLS` | 30 / 40 / 200 | 每個 run 的上限 |
| `MAX_TURNS` / `MAX_TOTAL_TOKENS` / `MAX_RUNTIME_SECONDS` | 120 / 6000000 / 1500 | model 回合、token 總量、時間上限（pipeline 的 `ResearchTimeoutSeconds` 要比最後一個大） |
| `MAX_CONCURRENT_RUNS` | 2 | 每個 session 同時進行的 run |
| `FETCH_TIMEOUT` / `FETCH_MAX_BYTES` | 20 / 3000000 | 單頁抓取逾時（秒）與大小 |
| `PROMPT_CACHE` | `auto` | Bedrock prompt caching；`off` 關閉 |
| `RESEARCH_NOW` / `RESEARCH_TODAY` | 空 | 固定時鐘（測試與重播用） |

`agentcore dev`（本機）讀的是 `agentcore/.env.local`，不是 `agentcore.json` 的 `envVars`。

runtime role 的權限來自 `agentcore.json` 的 `additionalPolicies`：data plane stack 的 `RuntimeDataPolicyArn`（讀 `portal/`、寫 `research/`）一定要在裡面，
`agentcore deploy` 是照這個檔案設定 role 的，少了它就會被拿掉。
`uv run --with-requirements scripts/requirements.txt python scripts/configure_agent.py --check --stack <data plane stack>` 可以隨時檢查三項設定。

## 5. 本機開發與部署

以下指令在 `app/llmops_research/` 底下執行：

```bash
uv run --extra dev python -m pytest tests                              # 離線測試（約 20 秒）
uv run --extra dev python -m pytest tests/test_live.py --live -v -rP   # 對真實網頁（只需要網路）
PORTAL_REPO=/path/to/llmops-portal uv run --extra dev python -m pytest tests/test_live.py tests/test_portal_parity.py --live -v -rP
```

用官方 CLI 在本機起 server（離線、不呼叫模型）。以下在**專案根目錄**執行：

```bash
printf 'MODEL_PROVIDER=scripted\n' >> agentcore/.env.local
agentcore dev --logs --skip-deploy --no-traces      # 終端機 1
agentcore dev -s "weekly"                           # 終端機 2：串流顯示進度
```

要在本機用真實模型：把 `.env.local` 的 `MODEL_PROVIDER` 拿掉（預設 `bedrock`），shell 裡備好 AWS 憑證與 region，並可加上
`PORTAL_DATA_URI=/path/to/llmops-portal`（或一個本機的資料位置）讓 agent 讀真實資料。

**部署**（專案根目錄）：

```bash
# 這份專案不含 agentcore/aws-targets.json、agentcore/.cli/deployed-state.json、agentcore/.env.local（每個帳號／每台機器自己的檔案）。
# 已經部署過：沿用原本的那幾個檔案（解壓縮覆蓋在原本的資料夾上，或把它們複製過來）
# 第一次部署：建立 deployment target，填入自己的 account 與 region
cp agentcore/aws-targets.example.json agentcore/aws-targets.json      # 然後編輯 account／region

uv run --with-requirements scripts/requirements.txt python scripts/configure_agent.py --stack <data plane stack 名稱>
agentcore validate && agentcore deploy
```

> `agentcore --version` 應該印出 0.x 的版本號（`@aws/agentcore`）。若出現「Starter Toolkit CLI is no longer supported」或沒有 `validate` 這個指令，
> PATH 上是舊版的 Python CLI：`npm install -g @aws/agentcore`。

## 6. 對真實頁面驗證過的事（2026-10-02）

我對真實頁面跑 agent 時，手上的 portal 資料還是 2026-10-01 版。當時 agent 讀到、且與那份資料不同的有兩項；後來 portal 的 2026-10-02 更新寫入了相同的結論：

1. **Foundry 上的 Claude Sonnet 4.5**：Foundry 退役表列 `claude-sonnet-4-5 | 1 | GA | 2026-11-30 | claude-sonnet-5-5`，舊 portal 是 2026-11-15。
   agent 端到端產生的提案是：`date` 改為 2026-11-30，舊的 2026-11-15 保留為 `superseded`。pipeline 的測試重播的就是這一份提案。
2. **Gemini 2.5 在 Agent Platform**：官方頁面列 `gemini-2.5-pro` / `flash` / `flash-lite` 的 Retirement date 為 October 20, 2026。
   這一項只驗證到「agent 讀得到、逐列可引用」並手動和 portal 對照。

2026-10-06 對這一版重新執行了 `tests/test_live.py --live`（10 passed，2 個需要 Gateway／真實模型的跳過）與 `test_portal_parity.py`（11 passed）。

這證明的是「讀頁面 → 驗證 → 提案」這條管線在真實頁面上讀得到、引得出、套得上；**不代表真實模型的研究品質**（測試用腳本化模型驅動）——那要靠
delivery plan Phase 2 的平行運轉來確認。之後加上的驗證規則（一個 operation 一件事、時間戳記、flags）只用腳本化模型測過：
真實模型遇到這些規則被退回時改不改得過來，同樣要在平行運轉時觀察。

## 7. 已知限制

- **提案品質取決於來源頁面**：官方頁面改版時，`test_live.py` 的第一個測試會先失敗並印出找到的行，這是設計好的 canary；建議定期跑一次。
- **模型 ID 與推理設定**：使用 Bedrock global inference profile；Claude 5.5 不接受自訂 temperature／top_p，也不強制 `tool_choice`，
  agent 用「驗證過的 `submit_proposal` 工具」取代強制工具呼叫。
- **Session 與成本**：每個 run 一個新的 microVM（`invoke_agent.py` 結束時會 `StopRuntimeSession` 釋放它；背景 run 不會被停止）；
  成本由預算上限控制，`result.usage` 會回報 token。
- **時區**：portal 日期用台北日曆日；時間戳用 UTC。
- **Python 3.14 runtime**：本機測試是 3.13；`agentcore.json` 設的是 `PYTHON_3_14`，若部署時有套件問題改成 `PYTHON_3_13`。
- **為什麼用 Runtime 而不是 Harness**：需要自訂的驗證關卡、SafeFetcher 與 patch 邏輯。之後可以加上 AgentCore Observability（已啟用 OTel 依賴）、
  Evaluations（用 `result.report` 當評分素材）與 Policy（限制 Gateway 工具）。

## 8. 不經過 pipeline 的用法（把提案變成 PR）

原本的流程仍然可用：`scripts/apply_proposal.py <proposal.json> --portal ../llmops-portal --open-pr --validate` 會在 portal repo 開分支、
套用、跑 `npm run validate`、開 draft PR。採用 pipeline 之後上線的資料在 S3，repo 的 `data/` 是工作副本，所以這條路只適合
「想用 PR 審核某一份提案」的情況：先 `portalctl pull` 讓 checkout 對齊上線中的資料，再套用提案、審核、merge，最後 `portalctl push`
才會上線。一般情況請用 `PublishMode=review` 加 `portalctl show`／`approve`。`scripts/sync_portal_data.py`（把 repo 的資料平鋪上傳給 agent 讀）已由
`portalctl bootstrap`／`push` 取代；資料位置一旦有 release 指標，它上傳的平面複本就不再被讀取。
