# LLMOps Portal（React 原型，完整版）

Vite + React 18 + TypeScript，零執行期相依（只有 React）。型別直接引用 `../schema/types.ts`；
build 時把 `../data`、`../runs`、`../config` 複製進 `public/`，並產生 `public/data/manifest.json`（週報、deep dive、測試紀錄清單）。

```bash
cd web
npm install
npm run dev        # http://localhost:5173
npm run build      # 產出 dist/（靜態檔，可放 Amplify / S3 + CloudFront）
```

## 頁面（hash 路由）

| 路徑 | 頁面 | 內容 |
|---|---|---|
| `#/`、`#/digest/<日期>` | Release Radar | 週報摘要、統計、行動項目（剩餘天數）、新模型、watchlist、六類變動、未驗證清單；可切換歷史週報 |
| `#/models` | 模型目錄 | 搜尋、依雲端 / 層級篩選、勾選比較 |
| `#/models/<id>` | 模型頁（10 個分頁） | 概覽、Deep dive、三雲與 API、效能與指數、價格與試算、安全與合規、LiteLLM、內部測試、生命週期、Onboarding |
| `#/compare/<id,id,id>` | 模型比較 | 最多 3 個模型並排 |
| `#/eol` | 平台清單與 EOL | 平台路由風險（sync_inventory）、最近退役、全通路生命週期行事曆（依月份、含衝突與原公告日）、各通路規則 |
| `#/tests` | 測試結果 | compat 矩陣（案例 × 路由）、perf 百分位數、goodput、SLO、token 用量 |
| `#/release`、`#/release/<id>` | 發布流程 | 每個模型的 G0–G10 發布紀錄（勾選、狀態、證據、審核人，可匯出 JSON）、關卡定義、需求清單 |
| `#/gateway` | LiteLLM 與安全 | 版本檢查器、建議 / 最低 / 支援窗口、避免清單、安全公告、已知陷阱、設定範例、強化清單 |
| `#/onboarding` | 上手指南 | 四步驟、能力層 alias、所有路由名稱、各模型參數注意事項、資料等級 |
| `#/methods` | 方法與來源 | 指標定義、AI 安全測試目錄、可信度說明、125 筆來源（可搜尋） |

## 資料怎麼進來

| 來源 | 產生方式 | 頁面 |
|---|---|---|
| `data/*.json` | 每週排程研究 → PR 審核 | 全站 |
| `data/internal/platform-inventory.json` | `tools/sync_inventory.py`（沒有時顯示 example） | 平台清單與 EOL、Radar 統計 |
| `runs/compat/*.json` | `tools/compat_suite.py` | 測試結果 |
| `runs/perf/*.json` | `tools/perf_probe.py` | 測試結果 |

`runs/*/example-mock.json` 是對模擬 gateway 跑的示範資料，頁面會標示「模擬資料」；有真實結果後可刪除。

## 原型的限制（正式版要補）

- 發布紀錄存在瀏覽器 localStorage（每人各自一份）；正式版請改存資料庫或 Git，並接簽核流程。
- 沒有登入與權限控管；部署時請放在 SSO / 密碼保護之後。
- 資料是 build 時打包的靜態檔；要即時資料可改成從 S3 或 API 讀取（`src/data.ts` 的 `loaders`）。
- 樣式集中在 `src/styles.css` 的 `:root` 變數，換品牌色只改一處。
