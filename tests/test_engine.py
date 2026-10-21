"""P0 引擎回归测试。

直接调 solve_arbitrage，不依赖 DB / HTTP。
覆盖：
1. 有套利场景（1X2 三向对冲）
2. 无套利场景（INFEASIBLE）
3. 返点进 PnL
4. 最终覆盖核验（三态全 covered）
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

# 让 `backend` 可导入
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.engine.engine import solve_arbitrage  # noqa: E402
from backend.engine.asian_handicap import ah_states, ah_settle_state  # noqa: E402


def _bets(items: list[tuple[str, str, str]]) -> list[dict]:
    """items: list of (quote_id, source, selection_code, odds_str)"""
    out = []
    for qid, source, code, odds in items:
        out.append({
            "id": qid,
            "source": source,
            "selection_code": code,
            "odds_decimal": odds,
        })
    return out


def test_arbitrage_basic():
    """TC 主胜 2.50 + HG 平 3.40 + HG 客 3.80，有套利。"""
    bets = _bets([
        ("Q_TC_1", "TC", "HOME", "2.50"),
        ("Q_HG_2", "HG", "DRAW", "3.40"),
        ("Q_HG_3", "HG", "AWAY", "3.80"),
    ])
    plan = solve_arbitrage(bets, ["HOME", "DRAW", "AWAY"],
                          budget=Decimal("1000"), step=Decimal("1"), min_stake=Decimal("1"))
    assert plan is not None
    assert plan["status"] == "COMPLETED", plan
    assert Decimal(plan["min_pnl"]) > 0, f"Pmin 应 >0: {plan}"
    assert Decimal(plan["total_stake"]) <= 1000
    # 三态都该被覆盖
    assert plan["uncovered_states"] == []
    assert set(plan["covered_states"]) == {"HOME", "DRAW", "AWAY"}
    # HG 返点 0.5% 应体现在 rebate_total
    assert Decimal(plan["rebate_total"]) > 0
    print("[PASS] test_arbitrage_basic")
    print(f"  total_stake={plan['total_stake']}  Pmin={plan['min_pnl']}  "
          f"rebate={plan['rebate_total']}  pnl_by_state={plan['pnl_by_state']}")


def test_no_arbitrage():
    """Liverpool 1.80 + 平 3.20 + 客 4.00，sum_inv=0.5556+0.3125+0.25=1.1181 > 1，无套利。"""
    bets = _bets([
        ("Q_TC_1", "TC", "HOME", "1.80"),
        ("Q_HG_2", "HG", "DRAW", "3.20"),
        ("Q_HG_3", "HG", "AWAY", "4.00"),
    ])
    plan = solve_arbitrage(bets, ["HOME", "DRAW", "AWAY"],
                          budget=Decimal("1000"), step=Decimal("1"), min_stake=Decimal("1"))
    assert plan is not None
    assert plan["status"] == "INFEASIBLE", f"应识别为无套利: {plan}"
    print("[PASS] test_no_arbitrage")
    print(f"  reason={plan['reason']}")


def test_coverage_partial():
    """只下 HOME 一注，HOME covered，DRAW/AWAY uncovered。"""
    bets = _bets([
        ("Q_TC_1", "TC", "HOME", "2.50"),
    ])
    plan = solve_arbitrage(bets, ["HOME", "DRAW", "AWAY"],
                          budget=Decimal("1000"), step=Decimal("1"), min_stake=Decimal("1"))
    # 单注 sum_inv=0.4 < 1 会被判有套利，但覆盖是 partial
    assert plan is not None
    if plan["status"] == "COMPLETED":
        assert "HOME" in plan["covered_states"]
        assert set(plan["uncovered_states"]) == {"DRAW", "AWAY"}
        assert plan["full_coverage"] is False
    print("[PASS] test_coverage_partial")
    print(f"  covered={plan['covered_states']}  uncovered={plan['uncovered_states']}")


def test_rebate_applied():
    """HG 源返点 0.5% 应被计入 PnL。"""
    bets = _bets([
        ("Q_TC_1", "TC", "HOME", "2.50"),
        ("Q_HG_2", "HG", "DRAW", "3.40"),
        ("Q_HG_3", "HG", "AWAY", "3.80"),
    ])
    plan = solve_arbitrage(bets, ["HOME", "DRAW", "AWAY"],
                          budget=Decimal("1000"), step=Decimal("1"), min_stake=Decimal("1"))
    assert plan["status"] == "COMPLETED"
    hg_stakes = sum(Decimal(s["stake"]) for s in plan["stakes"] if s["source"] == "HG")
    expected_rebate = hg_stakes * Decimal("0.005")
    assert abs(Decimal(plan["rebate_total"]) - expected_rebate) < Decimal("0.01"), \
        f"返点不符: got {plan['rebate_total']}, expect ~{expected_rebate}"
    print("[PASS] test_rebate_applied")
    print(f"  HG_stakes={hg_stakes}  rebate={plan['rebate_total']}")


def test_asian_handicap_p1():
    """P1: 让球盘两态对冲。TC 主队 -0.5 @ 2.01 vs HG 客队 +0.5 @ 1.95。
    sum_inv = 1/2.01 + 1/1.95 = 0.4975 + 0.5128 = 1.0103 > 1，应无套利。
    换一组有套利的：TC 主 -0.5 @ 2.10 vs HG 客 +0.5 @ 1.95
    sum_inv = 0.4762 + 0.5128 = 0.9890 < 1，应有套利。
    """
    bets = _bets([
        ("Q_TC_1", "TC", "HOME", "2.10"),
        ("Q_HG_2", "HG", "AWAY", "1.95"),
    ])
    plan = solve_arbitrage(bets, ["HOME", "AWAY"],
                          budget=Decimal("1000"), step=Decimal("1"), min_stake=Decimal("1"))
    assert plan["status"] == "COMPLETED", f"应有套利: {plan}"
    assert Decimal(plan["min_pnl"]) > 0
    assert plan["uncovered_states"] == []
    assert set(plan["covered_states"]) == {"HOME", "AWAY"}
    print("[PASS] test_asian_handicap_p1")
    print(f"  total={plan['total_stake']}  Pmin={plan['min_pnl']}  pnl={plan['pnl_by_state']}")


def test_over_under_p1():
    """P1: 大小球两态对冲。TC 大 2.5 @ 2.10 vs HG 小 2.5 @ 1.85。
    sum_inv = 0.4762 + 0.5405 = 1.0167 > 1，应无套利。
    换一组：TC 大 2.5 @ 2.20 vs HG 小 2.5 @ 1.85
    sum_inv = 0.4545 + 0.5405 = 0.9950 < 1，应有套利。
    """
    bets = _bets([
        ("Q_TC_1", "TC", "OVER", "2.20"),
        ("Q_HG_2", "HG", "UNDER", "1.85"),
    ])
    plan = solve_arbitrage(bets, ["OVER", "UNDER"],
                          budget=Decimal("1000"), step=Decimal("1"), min_stake=Decimal("1"))
    assert plan["status"] == "COMPLETED", f"应有套利: {plan}"
    assert Decimal(plan["min_pnl"]) > 0
    assert set(plan["covered_states"]) == {"OVER", "UNDER"}
    print("[PASS] test_over_under_p1")
    print(f"  total={plan['total_stake']}  Pmin={plan['min_pnl']}  pnl={plan['pnl_by_state']}")


def test_ah_no_arbitrage():
    """P1: 让球盘无套利场景应返回 INFEASIBLE。"""
    bets = _bets([
        ("Q_TC_1", "TC", "HOME", "2.01"),
        ("Q_HG_2", "HG", "AWAY", "1.95"),
    ])
    plan = solve_arbitrage(bets, ["HOME", "AWAY"],
                          budget=Decimal("1000"), step=Decimal("1"), min_stake=Decimal("1"))
    assert plan["status"] == "INFEASIBLE"
    print("[PASS] test_ah_no_arbitrage")
    print(f"  reason={plan['reason']}")


def test_ah_five_state_push():
    """v4: 整数盘 -1 对冲（HOME 1.9 vs AWAY 2.2，Σ=0.98 套利）。
    状态含走水 gd=1：两注都 PUSH 返还本金 → 该态保本（pnl≈0）。
    旧两态模型会把 gd=1 误算为主胜态返还 stake×odds（高估收益）。
    """
    bets = _bets([
        ("Q_TC_1", "TC", "HOME", "1.90"),
        ("Q_HG_2", "HG", "AWAY", "2.20"),
    ])
    states = ah_states("-1")
    assert states == ["gd<=0", "gd=1", "gd>=2"], states
    settle = lambda state, bet, stake: ah_settle_state(state, bet, stake, Decimal("-1"))
    plan = solve_arbitrage(bets, states, budget=Decimal("1000"),
                           step=Decimal("1"), min_stake=Decimal("1"), settle=settle)
    assert plan["status"] == "COMPLETED", plan
    assert set(plan["covered_states"]) == set(states)  # 走水态返还本金也计入覆盖
    # 走水态：两注都 PUSH 返还本金 → 仅剩返点收益（HG 0.5%），是最差态
    push_pnl = Decimal(plan["pnl_by_state"]["gd=1"])
    rebate = Decimal(plan["rebate_total"])
    assert abs(push_pnl - rebate) < Decimal("1"), f"走水态应=返点: {push_pnl} vs {rebate}"
    assert Decimal(plan["min_pnl"]) == push_pnl
    # 输/赢态有正收益
    assert Decimal(plan["pnl_by_state"]["gd<=0"]) > 0
    assert Decimal(plan["pnl_by_state"]["gd>=2"]) > 0
    print("[PASS] test_ah_five_state_push")
    print(f"  total={plan['total_stake']}  Pmin={plan['min_pnl']}  pnl={plan['pnl_by_state']}")


def test_ah_quarter_half():
    """v4: 四分一盘 -0.25（HOME 2.1 vs AWAY 2.1，Σ=0.952 套利）。
    平局态 gd=0：主队半输（返半）客队半赢（返半×odds），净收益为正但最小。
    """
    bets = _bets([
        ("Q_TC_1", "TC", "HOME", "2.10"),
        ("Q_HG_2", "HG", "AWAY", "2.10"),
    ])
    states = ah_states("-0.25")
    assert states == ["gd<=-1", "gd=0", "gd>=1"], states
    settle = lambda state, bet, stake: ah_settle_state(state, bet, stake, Decimal("-0.25"))
    plan = solve_arbitrage(bets, states, budget=Decimal("1000"),
                           step=Decimal("1"), min_stake=Decimal("1"), settle=settle)
    assert plan["status"] == "COMPLETED", plan
    # 平局态半赢半输：净收益 = 25 + 返点（HG 0.5%×500=2.5）
    gd0 = Decimal(plan["pnl_by_state"]["gd=0"])
    rebate = Decimal(plan["rebate_total"])
    assert abs(gd0 - (Decimal("25") + rebate)) < Decimal("1"), f"平局态应≈25+返点: {gd0}"
    assert Decimal(plan["min_pnl"]) > 0
    assert set(plan["covered_states"]) == set(states)
    print("[PASS] test_ah_quarter_half")
    print(f"  total={plan['total_stake']}  Pmin={plan['min_pnl']}  pnl={plan['pnl_by_state']}")


if __name__ == "__main__":
    test_arbitrage_basic()
    test_no_arbitrage()
    test_coverage_partial()
    test_rebate_applied()
    test_asian_handicap_p1()
    test_over_under_p1()
    test_ah_no_arbitrage()
    test_ah_five_state_push()
    test_ah_quarter_half()
    print("\n=== ALL TESTS PASSED ===")
