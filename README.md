# Football Hedge P0

足球跨平台市场覆盖与对冲研究系统・P0 基础工程（已接入 TC 真实数据）。

## 当前状态



| 模块      | 状态                                                           |
| ------- | ------------------------------------------------------------ |
| TC 体彩采集 | ✅ 真实数据（竞彩官方公开 API，无需登录）                                      |
| HG 皇冠采集 | ❌ 未接入（需要 AdsPower 环境 + DOM selector）                         |
| 真对冲求解器  | ✅ 解析解 `stake_i ∝ 1/odds_i`，Decimal 精确                        |
| 返点进优化   | ✅ HG 0.5% 返点计入 PnL                                           |
| 最终覆盖核验  | ✅ Stake>0 回流重算 covered/uncovered                             |
| 无套利识别   | ✅ sum\_inv ≥ 1 返回 INFEASIBLE                                 |
| 版本绑定    | ✅ snapshot / quote / rule / config / calculation             |
| 状态分类    | ✅ POSITIVE / BREAKEVEN / NON\_PROFIT / INFEASIBLE / NO\_DATA |
| P1 玩法   | ✅ 让球盘 AH / 大小球 OU（两态对冲）                                      |
| Web UI  | ✅ 机会中心 / 详情弹窗 / 收藏 / 跟踪订单 / 水位迭代                             |

## 快速启动



```
cd C:\1\football_hedge_p0\football_hedge_p0
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 采集 TC 真实数据
python -c "from backend.collector.collector import collect; import json,json; print(json.dumps(collect({'name':'TC','enabled':True,'mode':'tc_public_api','url':'https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?clientCode=3001','timeout_seconds':20,'headers':{'User-Agent':'Mozilla/5.0','Referer':'https://www.sporttery.cn/'}}), ensure_ascii=False, indent=2))"

# 跑测试
python tests\test_engine.py

# 启动 Web（需要 fastapi/uvicorn）
uvicorn backend.main:app --host 0.0.0.0 --port 18100 --reload
```

## 项目结构



```
football_hedge_p0/
├── README.md
├── requirements.txt
├── config/
│   └── sources.json          # TC/HG 数据源配置（返点率、URL、selector）
├── data/
│   ├── football.db           # SQLite（WAL 模式）
│   └── raw/                  # 原始快照落盘
├── static/
│   └── index.html            # 实时机会中心 Web UI
├── backend/
│   ├── db.py                 # Schema + migration（自动 ALTER TABLE）
│   ├── main.py               # FastAPI 入口
│   ├── collector/
│   │   └── collector.py      # 采集：TC 公开 API / HTTP JSON / Browser CDP
│   └── engine/
│       └── engine.py         # 权威计算：snapshot → 求解 → 覆盖核验 → 入库
└── tests/
    └── test_engine.py        # 7 个回归用例
```

## V1 P0 模块合规表（15 项必须实现）



| #  | 模块              | 状态                | 实现位置                                                             |
| -- | --------------- | ----------------- | ---------------------------------------------------------------- |
| 1  | 多平台原始数据获取       | ✅ TC 真实 / ⏸ HG 待接 | collector.py                                                     |
| 2  | 来源字段标准化         | ✅                 | collector.normalize\_payload                                     |
| 3  | 比赛匹配            | ⏸ 待 HG            | engine.match\_snapshot                                           |
| 4  | 标准化报价           | ✅                 | quotes 表 + quote\_version                                        |
| 5  | 报价变化与连续重算       | ✅                 | quote\_events 表                                                  |
| 6  | 计算准入与能力分类       | ✅                 | engine.\_admission\_check                                        |
| 7  | 投注原子与投注单生成      | ✅                 | Bet Atom + 单关结构                                                  |
| 8  | 比赛状态生成          | ✅                 | engine.derive\_match\_status（SCHEDULED/PRE\_MATCH/LIVE/FINISHED） |
| 9  | 结算映射            | ✅ P0 边界           | engine.state\_space\_for\_market                                 |
| 10 | 覆盖引擎            | ✅                 | engine.solve\_arbitrage + 最终核验                                   |
| 11 | 经济计算与资金优化       | ✅                 | 解析解 + 返点 + 词典序                                                   |
| 12 | 结果分类            | ✅                 | POSITIVE/BREAKEVEN/NON\_PROFIT/INFEASIBLE                        |
| 13 | 机会评估            | ✅                 | grade=A/B/C/D（按 Pmin）                                            |
| 14 | 前端实时展示          | ✅                 | static/index.html                                                |
| 15 | 端到端 / 回归 / 连续运行 | ✅                 | tests/test\_engine.py + test\_e2e.py                             |

## 测试



```
python tests\test_engine.py   # 7 个单元测试
python tests\test_e2e.py       # 端到端冒烟（采集→计算→入库）
```

## 待办（P1.5 / P2）



* HG 真实 selector（需要 AdsPower 环境）

* 整数盘走水 / 半赢半输结算

* quote\_events 表（水位迭代真实数据）

* 比赛状态机（SCHEDULED→PRE\_MATCH→LIVE→FINISHED）

* 真实赛果结算（P0 后置）

## 安全声明

研究 / 工程基础框架，不承诺盈利，不包含自动下注。