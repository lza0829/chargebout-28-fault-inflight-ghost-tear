# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。

## 有在充时改故障 / 维护

本仓全局只贯彻一种口径：**故障强制中断在充**。

- 桩位仍挂着充电中/结算中会话时，允许改故障或维护；同一笔事务内把这些会话按中断口径收口
  （状态 → 已中断、写中断回执与会话事件、释放占用、枪线联动故障）。
- 强制中断不自动结算：金额停在当前预估，之后也不能再结算/记脉冲。
- 改故障与关账并发时只有一方能收口，另一方被拒（4xx），不会出现双终态；
  若桩态已是故障却仍有在充，再发一次改故障会自愈收口。
- 每次桩态变更落一条 `pile_status_events`（含 `桩态N` 回执号）；被中断的每个会话落一条
  `session_interrupts`（`中断{桩态事件}-{会话}`）。
- 回执可重放：改桩态带可选 `请求号`，同请求号重放返回原回执、不重复中断/写事件；
  也可用 `GET /api/桩态回执?请求号=...` 取回。
- 影响面同刷：桩位页、桩态事件、会话册与详情、总览（桩位故障桶 / 充电中桶 /
  `故障桩在充会话` / `口径异常数`）。

相关接口：`POST /api/桩位/{id}/状态`、`GET /api/桩位/{id}/状态事件`、
`GET /api/桩态回执`、`GET /api/口径体检`。

夹具：`backend/tests/test_pile_fault_linkage.py`（有在充改故障、故障与关账对撞、
故障后记脉冲被拒、拒改占用不变、重放、体检识别半截）。

```bash
cd backend
PYTHONPATH=. python -m pytest tests -q
```

