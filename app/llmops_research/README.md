# llmops_research（agent 程式碼）

LLMOps research agent 的 AgentCore Runtime 程式碼。整體架構、部署步驟與驗證狀態請看專案根目錄的 `README.md`，agent 的驗證關卡、payload 與環境變數在
`docs/agent.md`；這裡只放開發時最常用的資訊。

## 開發

```bash
# 以下在 app/llmops_research/ 底下執行
uv run --extra dev python -m pytest tests                      # 離線測試，不需要 AWS
uv run --extra dev python -m pytest tests/test_live.py --live -v -rP   # 對真實網頁（只需要網路）
PORTAL_REPO=/path/to/llmops-portal uv run --extra dev python -m pytest tests/test_portal_parity.py   # 與 portal 自己的驗證器比對
```

- `main.py`：`BedrockAgentCoreApp` 入口。`POST /invocations` 回傳 `text/event-stream`，最後一個事件的 `type` 是 `result`；`GET /ping` 在背景 run 進行中回 `HealthyBusy`。
- `model/load.py`：`MODEL_PROVIDER` = `bedrock`（預設）· `anthropic` · `scripted`（離線替身，`SCRIPT_FILE` 可指定劇本 JSON）。
- `research/agent.py`：payload 驗證、預算、heartbeat、取消；`research/tools.py` 是模型能呼叫的工具；`research/validate/` 是 `submit_proposal` 的驗證關卡。
- `research/web/`：SafeFetcher（SSRF 防護）、HTML→文字（每個表格列一行，方便逐字引用）、網域→信任層級、搜尋後端、AgentCore Browser。
- `research/portal/`：portal 資料載入（`PORTAL_DATA_URI` 指到資料位置時，跟著 `manifest.json` 讀它指到的 release）、JSON patch、portal 驗證器的移植、
  保護欄位（`protected.py`）；`snapshot/` 是內建的 **SAMPLE** 資料（只供離線測試；依它產生的 proposal 會標示「SAMPLE」，pipeline 一律拒絕發布）。
- `research/apply.py`、`research/portal/{checks,data,diff,labels,patch,protected}.py` 與 `research/validate/{injection,urls}.py` 也會被打包進 pipeline 的 Lambda
  （`scripts/build_lambdas.py` 的 `SHARED`）：發布時用同一份程式把 proposal 重新套用、重新驗證、比對差異。改這幾個檔案時，agent 與 pipeline 兩邊的測試都要跑。
- `research/proposal.py` 是 `submit_proposal` 的驗證器：每一個新增或改動的日期、數字、版本，要在**同一則**引文裡找得到，那一則引文還要講的是這個模型
  （`research/validate/subjects.py`）、所在頁面的層級也夠（`research/web/tiers.py`）。它擋不下、但發布前應該有人看一眼的事（像指令的文字、
  沒讀過的網址、digest 沒有重新讀取的項目、`asOf`／`lastVerified` 沒讀完來源）記在 proposal 的 `validation.flags`／`validation.asOfCoverage`／
  `validation.lastVerifiedCoverage`；pipeline 看到任何一種標記就改成等人核准。規則的說明在 `docs/agent.md` 第 2 節，仍然存在的限制在 `docs/known-issues.md`。
- 驗證器與發布端對「什麼會等人」要說同一件事：`research/portal/labels.py` 的判斷（改寫原有的文字、狀態與分類、標籤、清空、引用來源）兩邊共用，
  驗證器對每一種都會警告模型。改其中一邊時，另一邊的測試（`tests/test_grounding_rules.py`、`pipeline/tests/test_publish_gates.py`）也要跟著改。

## 規則

- 網頁內容一律是不可信資料；任何新的抓取、解析或驗證邏輯都要附測試（離線測試用 `tests/e2e_helpers.py` 的假網路；碰到真實頁面行為的，補在 `tests/test_live.py`）。
- 不改 `platformStatus`、內部清單與測試結果（`research/portal/protected.py` 以程式強制，不是只靠 prompt）；agent 只回傳 proposal，不寫 portal。
- 新增模型工具或 proposal 欄位時，同步更新 `research/prompts.py` 的格式說明與 `tests/test_agent_e2e.py`；proposal 的格式變了，
  還要重新產生 pipeline 的測試資料（`pipeline/tests/fixtures/make_fixtures.py`）。

## 本機用 `agentcore dev`

`agentcore dev` 讀的是 `agentcore/.env.local`（不是 `agentcore.json` 的 `envVars`）：

```bash
# 以下在專案根目錄執行
printf 'MODEL_PROVIDER=scripted\n' >> agentcore/.env.local     # 離線；拿掉這行就會用 Bedrock（需要 AWS 憑證）
agentcore dev --logs --skip-deploy --no-traces
agentcore dev -s "weekly"                                      # 另一個終端機
```
