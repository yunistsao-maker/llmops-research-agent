# UI：改成執行時從資料位置讀資料

portal 的 UI（`llmops-portal/web`，Vite + React）原本在 build 時把 `data/`、`runs/`、`config/` 打包進網站，所以資料更新就要重新 build、
重新部署。這裡的改裝把「資料從哪裡來」移到執行時決定：UI 啟動時讀一個小小的 `config.json`，知道資料位置之後先讀指標
（`manifest.json`），再從它指到的 release 讀所有資料。之後 pipeline 每發布一個 release，UI 不需要任何動作就會讀到。

改裝只動資料載入這一層，頁面程式不變。沒有設定資料位置時，網站照舊讀打包的資料；看得到的差別只有頁尾那一行
（原本的「每週排程更新、經平台團隊審核」換成「資料隨網站一起打包」），以及某一頁畫不出來時顯示錯誤訊息而不是整個空白。

下面的指令都在這個專案（`llmops-research-agent`）的根目錄執行，portal repo 假設放在旁邊的 `../llmops-portal`。

## 1. 套用

```bash
python scripts/patch_ui.py ../llmops-portal/web --check     # 只看會改什麼，不寫入
python scripts/patch_ui.py ../llmops-portal/web             # 套用（可以重複執行）
(cd ../llmops-portal/web && npm run build)                  # type check + build；這時仍然使用打包的資料
```

| 動作 | 檔案 | 內容 |
| --- | --- | --- |
| 新增 | `src/dataSource.ts` | 資料來源：bundled（打包）或 remote（資料位置的指標 → release）；每個 request 都有逾時 |
| 新增 | `src/DataStatus.tsx` | 頁尾：資料版本、發布時間、來源（agent 自動更新／誰核准／手動／rollback）、變更紀錄連結；有另一個版本上線時在右下角提示重新載入；讀不到資料時自動再試 |
| 新增 | `src/ErrorBoundary.tsx` | 畫面畫不出來時顯示錯誤訊息，導覽列與頁尾照常（原本是整個網站變成空白） |
| 新增 | `public/config.json` | `{}`；執行時設定（已經存在就不動） |
| 修改 | `src/data.ts` | `getJson` 與 `manifest` 改走 `dataSource`（其他 loader 不變） |
| 修改 | `src/pages/Gateway.tsx` | 設定範例（`config/litellm.example.yaml`）改走 `dataSource` |
| 修改 | `scripts/sync-data.mjs` | 設定了資料位置時不再把資料複製進 `public/` |
| 修改（選用） | `src/App.tsx`、`src/main.tsx` | 頁尾換成資料版本列；頁面內容與整個 App 各包一層 ErrorBoundary |
| 修改（選用） | `src/pages/Radar.tsx`、`src/styles.css`、`README.md` | 首頁等資料索引回來再判斷有沒有週報；樣式；說明 |

- **每一項修改只換掉它認得的那一段程式。** 那一段妳改過、認不得時：必要的修改（上表沒有標「選用」的三列：`src/data.ts`、`src/pages/Gateway.tsx`、
  `scripts/sync-data.mjs`）會讓整個動作停下來、什麼都不寫，並印出要手動改的內容；選用的修改會略過並說明怎麼手動加。
- **`--revert` 是把每一項修改反過來做一次**，不是拿舊檔案蓋回去，所以套用之後妳在同一個檔案別處做的修改都會留著。
  只要有一項撤不回來（套用後那一段又被改過），就什麼都不寫，並告訴妳是哪一項、原本的內容是什麼。
- **請把 `web/.patch_ui/` 一起 commit**（很小）。裡面的 `state.json` 記錄每一項修改換掉了什麼，換一台機器才撤得回來；
  沒有這份紀錄時，只有「純新增」的修改能自動撤回，其他的會請妳用 `git checkout` 還原。`orig/` 是套用前的檔案，給人對照用。
- 其他地方如果還有**自己讀資料**的程式，script 會列出來並停下：呼叫 `fetch`（包含 `window.fetch(…)`、把 `fetch` 交給別的名字、括號寫在下一行）、
  `XMLHttpRequest`／`EventSource`、把資料檔 `import` 進程式（那一份會被打包進網站，永遠不會更新）、把 `data/…json` 這種路徑交給 `loaders`／`fetchJson`／`fetchText`
  以外的東西，或是 `href`／`src` 直接指向 `data/`、`runs/`、`config/`。這些請改用 `loaders`、`fetchJson` 或 `fetchText`，否則在資料位置模式下讀到的不是資料位置的內容。
  真的和 portal 資料無關的（例如呼叫別的服務），在那一行加上註解 `// patch_ui: ok`。
  這是對原始碼的文字比對，認得的就是上面這些寫法；**「沒有任何頁面繞過資料位置」是由第 4 節的 `ui_smoke.py` 確認的**（它看瀏覽器實際發出的 request）。
- 原本頁尾寫「每週排程更新、經平台團隊審核」。自動發布之後這句話不成立，所以換成每一版實際的來源，例如
  「research agent 自動更新：通過程式驗證，未經人工逐項審核」或「research agent 提案，eunice 核准後發布」。

## 2. 設定資料位置

`dataBaseUrl` 是 data plane stack 的 output `DataBaseUrl`（結尾有 `/`）。兩種設定方式，`config.json` 有寫就以它為準：

| 方式 | 怎麼做 | 適合 |
| --- | --- | --- |
| build 時的環境變數 | 在 Amplify console 的 Environment variables 加 `VITE_DATA_BASE_URL`，重新部署 | Amplify 接 Git repo 自動 build；網址不會進 repo |
| 執行時的 `config.json` | `public/config.json` 寫 `{"dataBaseUrl": "https://…/"}` 後 build；或直接改 build 產物裡的 `config.json` | 手動上傳 build 產物；同一份產物要指到不同的資料位置（dev／prod） |

設定之後 `npm run build` 會印出 `remote data: nothing is bundled into the site`，產物裡不再有 `data/`、`runs/`、`config/`。
要退回打包模式，把設定拿掉重新 build 即可。`BUNDLE_DATA=1` 可以在設定了資料位置的情況下仍然打包一份（平常不需要）。

兩個容易踩到的地方：

- `VITE_DATA_BASE_URL` 要是**真正的環境變數**（Amplify 的 Environment variables 就是）。寫在 `web/.env*` 檔裡的話，網站會去讀資料位置，
  但 build 仍然把資料打包進去；build 時會印出 WARNING 提醒。
- 用 `config.json` 設定時，`web/public/config.json` 必須進版本控制（不要被 `.gitignore` 擋掉），Amplify 從 repo build 才帶得到它。
  本機預覽請用第 3 節的環境變數，不要把 `http://127.0.0.1…` 寫進這個檔案再不小心 commit 出去。

**網址含有 token，等同一條分享連結。** UI 的 JavaScript 讀得到它，所以會出現在使用者的瀏覽器裡；它的作用是讓不知道網址的人讀不到，
不是身分驗證。UI 自己的訊息（頁尾、錯誤畫面）最多只會出現資料位置的主機名稱，不會出現路徑與 token。UI 需要登入時的做法見 [architecture](../docs/architecture.md) 第 6 節。

### Amplify 上的三件事

1. **CORS**：UI 與資料位置不同網域，資料位置要允許 UI 的 origin。data plane stack 的 `AllowedOrigins` 預設是 `*`，建議先維持：
   資料位置本來就靠 token 網址保護，`*` 沒有多開放什麼。要收斂時填 UI 的網址（含 `https://`、不含結尾的 `/`，多個用逗號分隔）。
   收斂成多個網址時要知道一件事：同一個瀏覽器先後從兩個「同站」的 UI 網址（例如同一個 Amplify app 的兩個 branch）讀同一個檔案時，
   第二個可能拿到瀏覽器快取裡、標著第一個網址的回應而被 CORS 擋下（release 檔案快取一年）。有多個 UI 網址時請維持 `*`。
2. **Content-Security-Policy**：如果妳在 Amplify 設了 CSP 的 custom header，`connect-src` 要加上資料位置的網域。
3. **（選用）讓資料走 UI 的網域**：在 Amplify 的 Rewrites and redirects 加一條 200 rewrite，把 `/portal-data/<*>` 轉到
   `<DataBaseUrl><*>`，UI 的 `dataBaseUrl` 寫 `/portal-data/`。這樣沒有 CORS，token 也不會出現在瀏覽器裡。
   **這條規則要排在 SPA 的 catch-all 規則（`/<*>` → `/index.html`）之前**，否則永遠輪不到它。

   ```json
   [{ "source": "/portal-data/<*>", "status": "200", "target": "https://<distribution>.cloudfront.net/<token>/<*>", "condition": null }]
   ```

   這條路我只確認了設定語法（Amplify 文件的 reverse proxy rewrite，只支援 HTTPS），沒有實測它的快取行為。採用前請執行
   `portalctl verify --url https://<UI 網址>/portal-data/`，它會檢查 `manifest.json` 有沒有被快取、每個檔案的內容是否正確。

驗收：

```bash
uv run --with-requirements scripts/requirements.txt python scripts/portalctl.py verify --url <DataBaseUrl> --origin <UI 的網址，例如 https://main.d111.amplifyapp.com>
```

## 3. 本機預覽（不需要 AWS）

三個終端機。前兩行在終端機 1 與 3 都要先執行（alias 只在定義它的那個終端機有效）；名稱刻意和正式環境用的 `portalctl` 不同，
而且指令列上的 `--data` 會讓每個動作都直接操作這個資料夾，不會送到 state machine。

```bash
cd <這個專案的根目錄>
alias portalctl-local='uv run --with-requirements scripts/requirements.txt python scripts/portalctl.py --data ./_local/portal'
```

```bash
# 終端機 1：用 portal repo 目前的資料建立本機的資料位置（會先列出要發布的檔案再問妳），並啟動它
portalctl-local bootstrap --portal ../llmops-portal
uv run --with-requirements scripts/requirements.txt python scripts/serve_data.py ./_local/portal     # 印出 dataBaseUrl：http://127.0.0.1:8787/

# 終端機 2：UI 指到本機的資料位置（用環境變數，不改任何檔案）
cd ../llmops-portal/web
VITE_DATA_BASE_URL=http://127.0.0.1:8787/ npm run dev         # http://localhost:5173，頁尾顯示資料版本

# 終端機 3：發布、rollback，回到瀏覽器看右下角的提示
portalctl-local pull --portal ../llmops-portal
#   …改一個 data/*.json…
portalctl-local push --portal ../llmops-portal --yes
portalctl-local rollback --reason "試試看"
```

- 頁面在「切回這個分頁」時與每 5 分鐘檢查一次有沒有別的版本，所以 push 之後切到別的分頁再切回來就會看到提示。
- `npm run dev` 在資料位置模式下會把 `web/public/` 裡先前打包的 `data/`、`runs/`、`config/` 刪掉（它們本來就不進版本控制）；
  之後不帶環境變數再執行一次就會重新複製。
- `serve_data.py` 照 `infra/data-plane.yaml` 的行為回應（token、CORS、快取 header、不回應 preflight）。它預設只允許本機的 Vite 網址
  （`localhost`／`127.0.0.1` 的 5173 與 4173）從瀏覽器讀取：這個資料夾裡有內部清單，不該因為開著這支工具，就讓瀏覽器裡任何一個網站都讀得到。
  `--origin <網址>` 是在這份清單之外再加一個來源；`--token <值>` 模擬 `DataAccessToken`（規則和 stack 相同：16–64 個英數字、`_`、`-`）。
  request 的 Host 必須是 `localhost` 或 IP 位址，用別的主機名稱連進來要先以 `--allow-host <名稱>` 列出。它只是開發工具，不是正式環境的元件。

## 4. 部署前的檢查：`ui_smoke.py`

改過 UI、改過 schema、或升級相依套件之後，部署前跑一次：

```bash
uv run --with-requirements scripts/requirements-ui.txt playwright install chromium     # 只需要一次：下載 headless Chromium
(cd ../llmops-portal/web && npm run build)
uv run --with-requirements scripts/requirements-ui.txt python scripts/ui_smoke.py --dist ../llmops-portal/web/dist --portal ../llmops-portal
```

它用 portal repo 的資料建立一個暫存的資料位置，把 build 好的 UI 指過去，然後在 headless Chromium 裡：

1. 逐頁、逐分頁打開（導覽列的每一頁、每個模型的 deep dive 與發布流程、每一期 digest、一組模型比較），確認每一頁都畫得出來、沒有錯誤；
2. 瀏覽途中發布一個新的 release：已經打開的頁面必須維持它原本的版本。接著在**同一個分頁**把每一頁重新打開一次：
   先前讀過的每一個檔案都要改從新的 release 再讀一次。只認檔名、不認版本的快取（例如把讀過的檔案存在 `sessionStorage`）會在這裡被抓到——
   那種頁面頁尾顯示新的版本、內容卻是舊的，而且完全不向資料位置要檔案，所以光看「有沒有讀到別的版本」看不出來；
3. 確認資料只從資料位置讀，而且資料位置收到的都是單純的 GET。這一項是在資料位置那一端記錄的，因為瀏覽器不會把 preflight 告訴網頁。
   網站本身只該提供 `config.json`：頁面向網站要了任何 JSON／YAML（不管放在哪個資料夾、叫什麼名字）都算失敗——那是打包在網站裡的資料副本。
   真的是網站自己的檔案（例如翻譯檔）用 `--allow-site-file <樣式>` 指明。最後還會打開幾個「id 裡帶著 `../`」的連結
   （`#/digest/..%2F..%2F..%2F<另一個 release>%2F…`）：頁面不能因此去讀別的 release 的檔案；
4. 再發布一個 release，**這次改掉頁面上顯示的一段文字**（從資料檔裡挑一段完整顯示在某一頁、而且在資料裡只出現一次的文字，前面加上記號）：
   開著的頁面要先維持原本的版本與內容，提示有新版本（定時檢查）；重新載入後畫面上要看得到改過的文字。
   **其他每一個資料檔**也各改一段文字，各自到顯示它的那一頁確認（只有第一個檔案更新正確的 UI 會在這裡被抓到）；
5. rollback 之後提示版本換了（切回分頁時的檢查），換回頁面顯示的版本時提示消失；重新載入後回到舊版，**畫面上也要是原本的文字**
   （只接受「比較新的版本」的快取會在這裡被抓到：rollback 之後它還留著被撤下的內容）——同樣是每一個資料檔都看，而且原本的文字要真的在畫面上，
   不是「被撤下的文字不見了」就算數；
6. 資料位置拒絕存取時，頁尾明確顯示讀不到資料；資料位置的路徑與 token 不能出現在畫面的文字、視窗標題、元素的屬性（例如 `title`）或 console 訊息裡；
   恢復之後頁尾自己恢復並提示重新載入。

第 4 步找不到任何一段可以拿來比對的文字時（沒有一頁完整顯示資料檔裡的文字），這項檢查會算失敗並說明原因，不會當成通過：
「內容有沒有跟著更新」沒有檢查到，就不該說全部通過。

全部在本機完成，不碰 AWS，也不修改 `web/` 裡的任何檔案（`config.json` 是在測試用的 web server 上替換的）。測試用的瀏覽器只連得到
這台機器：UI 向其他網站要的資源（例如 Google Fonts）不會連出去，也不算進結果。`--route compare/a,b,c` 可以加上額外要檢查的路徑。
這支檢查認得頁面所靠的 DOM 約定（`header nav button`、`.data-status[data-release]`、`.data-status-reload`…）列在 `ui_smoke.py --help`。

真正的資料位置（CloudFront）有一點本機模擬不出來：token 錯誤時的 403 可能不帶 CORS header，這時頁尾顯示的是「連不到…（CORS／存取限制）」
而不是「HTTP 403」。兩種訊息都代表同一件事。

## 5. 寫新頁面時要記得

- 讀資料用 `data.ts` 的 `loaders`，或 `dataSource.ts` 的 `fetchJson(path)`／`fetchText(path)`；`path` 是 `data/…`、`runs/…`、`config/…` 這種相對路徑。
  設定了資料位置時，`path` 只能是單純的相對路徑（英數字與 `. _ @ + = -` 組成的區段，不以 `.` 開頭）：路徑的一部分常常來自網址列（`#/digest/<id>`），
  帶著 `..`、`%`、`?` 的會被拒絕並顯示「沒有這個資料檔」，不會發出 request——否則一個連結就能讓頁面顯示另一個 release（例如已經 rollback 的那一版）的內容。
  （release 裡的檔名本來就只有這些字元；打包模式沒有 release，路徑照舊原樣使用。）
- 不要在 `fetch` 加自訂 header：資料位置只回應單純的 GET（不回應 CORS preflight）。
- 資料的格式以每個 release 裡的 `schema/llmops.schema.json` 為準。新增欄位時 UI 要能處理「舊資料沒有這個欄位」；
  改完跑 `ui_smoke.py`。
- 一頁的資料形狀不對、畫不出來時，ErrorBoundary 會顯示錯誤訊息並保留導覽列；讀同一份資料的其他頁面可能也會顯示同樣的訊息。
  它只接得到「畫畫面時」的錯誤：按鈕的事件處理或 request 失敗不歸它管，那些要由頁面自己顯示（`useData` 的「載入失敗：…」）。
  不要用 `try/catch` 把錯誤吞掉，讓它顯示出來才會被發現。
- 包住頁面的 ErrorBoundary 用 `resetKey`（目前的路由）清除錯誤，不是用 `key`：換頁不會重新掛載整個頁面，鍵盤焦點與頁內狀態都會留著。
