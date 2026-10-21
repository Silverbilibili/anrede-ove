"""串关方案生成器回归测试（V1 官方模型 + v3 五态受让/复式/降关）

模板例子：布莱顿 2.03 × 贝蒂斯 1.97 串（3.9991），客受让 +0.5 @ 2.53 / 2.49
验证：金额=2K×M、官方舍入、五态受让、返点、降关。
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.plan_generator import (  # noqa: E402
    _solve_multiplier, _floor, parlay_unit_prize, parlay_void_settle,
    _sporttery_cent_round,
)

D = Decimal


def _leg(home_odds, hedge_odds, line="-0.5", selections=("HOME",)):
    """构造 v3 legs 元素：主串 HOME + 客受让（主队视角 line，默认 -0.5 = 客 +0.5）。"""
    return {"match": {"home": "A", "away": "B"}, "selections": selections,
            "tc_odds": {c: D(o) for c, o in zip(selections, [home_odds] * len(selections))},
            "hedge_code": "AWAY", "hedge_line": line, "hedge_odds": D(hedge_odds)}


def test_sporttery_round():
    # 四舍六入五成双：只按第3位
    assert _sporttery_cent_round(D("2.5452")) == D("2.54")   # 第3位5+前位偶 → 舍
    assert _sporttery_cent_round(D("2.5552")) == D("2.56")   # 第3位5+前位奇 → 入
    assert _sporttery_cent_round(D("2.5449")) == D("2.54")   # 第3位4 → 舍
    assert _sporttery_cent_round(D("2.9991")) == D("3.00")   # 第3位9 → 入
    print("[PASS] 官方舍入四舍六入五成双")


def test_parlay2_multiplier_model():
    infos = [{"home": "布莱顿", "away": "阿森纳"}, {"home": "皇家贝蒂斯", "away": "塞维利亚"}]
    legs = [_leg("2.03", "2.53"), _leg("1.97", "2.49")]
    p = _solve_multiplier(["ma", "mb"], infos, legs, D("1000"), D("1"), D("1"))
    assert p["status"] == "SOLVED", p
    assert p["type"] == "PARLAY2"
    stake = D(p["parlay"]["stake"])
    m = p["parlay"]["multiplier"]
    assert 1 <= m <= 50, f"multiplier={m}"
    assert stake == D("2") * D(m), f"stake={stake} M={m}"
    # 官方舍入：2×3.9991=7.9982 → 五成双 8.00 → 单注奖金 8
    assert D(p["parlay"]["unit_prize_min"]) == D("8"), p["parlay"]["unit_prize_min"]
    # 受让 +0.5 五态（gd≤0 全赢 / gd≥1 全输）：受让腿受平台最小注 10 钳制
    # → 枚举最优 M=3（串关6元 + 受让10×2），最差 -1.60（含返点 0.40）
    assert D(p["worst_pnl"]) == D("-1.60"), p["worst_pnl"]
    assert D(p["rebate"]["total"]) == D("0.40"), p["rebate"]
    # 平台投注限制合规：TC 串关注数≤99、受让腿每笔≥最小注且≤最大注
    lim = p["limits"]
    assert lim["tc_leg"]["notes_compliant"] and lim["tc_leg"]["amount_compliant"]
    assert all(h["compliant"] for h in lim["hedge_legs"])
    assert all(D(h["stake"]) >= D("10") for h in lim["hedge_legs"]), lim["hedge_legs"]
    # 总投入 ≤ 预算；状态矩阵最差 == worst_pnl
    assert D(p["total_stake"]) <= D("1000")
    pnls = [D(s["pnl"]) for s in p["states"]]
    assert min(pnls) == D(p["worst_pnl"])
    # 非套利
    assert p["arbitrage"] is False
    print(f"[PASS] parlay2 V1官方模型: M={m} 串关金额={stake} 单注奖金={p['parlay']['unit_prize_min']} "
          f"受让={[h['stake'] for h in p['hedges']]} 最差={p['worst_pnl']} 返点={p['rebate']['total']} "
          f"唯一态={len(p['states'])}")
    for h in p["hedges"]:
        print(f"    受让腿: {h['match']} 买{h['hedge']} {h['line']} @ {h['odds']} 金额 {h['stake']}")


def test_true_arbitrage():
    # 2.5 × 2.0 = 5.0；客受让 +0.5 @ 2.6/2.6 → Σinv≈0.97 < 1 真套利
    infos = [{"home": "X", "away": "Y"}, {"home": "M", "away": "N"}]
    legs = [_leg("2.5", "2.6"), _leg("2.0", "2.6")]
    p = _solve_multiplier(["mx", "my"], infos, legs, D("1000"), D("1"), D("1"))
    assert p and p["arbitrage"] is True, p
    assert p["parlay"]["multiplier"] == 50, p["parlay"]["multiplier"]
    assert D(p["parlay"]["unit_prize_min"]) == D("10")  # 2×5.0=10.00
    assert D(p["worst_pnl"]) > 0
    assert D(p["total_stake"]) <= D("1000")
    print(f"[PASS] 真套利用例: M={p['parlay']['multiplier']} 投入={p['total_stake']} "
          f"最差={p['worst_pnl']} Σinv={p['sum_inv']} 返点={p['rebate']['total']}")


def test_quarter_handicap_five_state():
    # 四分一盘受让（主队 0,-0.5 → 客受让 +0.25）：平局态半输+半赢
    infos = [{"home": "A", "away": "B"}]
    legs = [_leg("2.0", "2.1", line="0,-0.5")]
    p = _solve_multiplier(["m1"], infos, legs, D("1000"), D("1"), D("1"))
    assert p["status"] == "SOLVED", p
    # 平局态（gd=0）：串关未命中；客受让 +0.25 → 组件 +0 走水 + 组件 +0.5 全赢
    # 返还 = 0.5×stake + 0.5×stake×2.1；找该态 pnl 验证
    found = [s for s in p["states"] if any(c["gd"] == [0] for c in s["coverage"])]
    assert found, "应存在 gd=0 状态"
    pnl0 = D(found[0]["pnl"])
    # 受让腿受平台最小注 10 钳制（原 x=1 现 ≥10）→ 最优 M 变化；五态精确性仍验证：
    # 平局态（gd=0）：串关未命中；客受让 +0.25 → 组件 +0 走水 + 组件 +0.5 全赢
    assert abs(pnl0 - D("3.650")) < D("0.01"), f"平局态 pnl={pnl0}"
    print(f"[PASS] 四分一盘五态受让: gd=0 态 pnl={pnl0}（半输+半赢精确，受让腿受最小注钳制）")


def test_parlay3_multiplier_model():
    infos = [{"home": "A", "away": "B"}, {"home": "C", "away": "D"}, {"home": "E", "away": "F"}]
    legs = [_leg("2.03", "2.53"), _leg("1.97", "2.49"), _leg("1.94", "2.44")]
    p = _solve_multiplier(["m1", "m2", "m3"], infos, legs, D("1000"), D("1"), D("1"))
    assert p["status"] == "SOLVED", p
    assert p["type"] == "PARLAY3"
    assert D(p["parlay"]["stake"]) == D("2") * D(p["parlay"]["multiplier"])
    pnls = [D(s["pnl"]) for s in p["states"]]
    assert min(pnls) == D(p["worst_pnl"])
    print(f"[PASS] parlay3 V1官方模型: M={p['parlay']['multiplier']} 最差={p['worst_pnl']} 唯一态={len(p['states'])}")


def test_futures_multi_selection():
    # 复式：第1场选区（主胜+平局）K=2 → 串关金额 = 2×2×M
    infos = [{"home": "A", "away": "B"}, {"home": "C", "away": "D"}]
    legs = [_leg("2.03", "2.53", selections=("HOME", "DRAW")), _leg("1.97", "2.49")]
    p = _solve_multiplier(["m1", "m2"], infos, legs, D("1000"), D("1"), D("1"))
    assert p["status"] == "SOLVED", p
    m = p["parlay"]["multiplier"]
    assert D(p["parlay"]["stake"]) == D("2") * D("2") * D(m), p["parlay"]["stake"]
    assert p["parlay"]["combos"] == "2×1", p["parlay"]["combos"]
    # 串关命中态覆盖：主胜×主胜 / 平局×主胜 都中串关
    pnls = [D(s["pnl"]) for s in p["states"]]
    assert min(pnls) == D(p["worst_pnl"])
    print(f"[PASS] 复式展开: 选区2×1 K=2 串关金额=4M 唯一态={len(p['states'])}")


def test_void_settle():
    # 降关：3腿中 1 腿无效 → 赔率按 1；全部无效 → 整票退款
    legs = [D("2.03"), D("1.97"), D("1.94")]
    r1 = parlay_void_settle(legs, {1}, 1)
    assert r1 == parlay_unit_prize(D("2.03") * D("1.94"), 1), r1
    r2 = parlay_void_settle(legs, {0, 1, 2}, 3)
    assert r2 == D("18"), r2  # 2×3组合×3倍 = 整票退款
    print(f"[PASS] 降关结算: 单腿无效→按1 连乘={parlay_unit_prize(D('2.03')*D('1.94'),1)}; 全无效→退款{r2}")


if __name__ == "__main__":
    test_sporttery_round()
    test_parlay2_multiplier_model()
    test_true_arbitrage()
    test_quarter_handicap_five_state()
    test_parlay3_multiplier_model()
    test_futures_multi_selection()
    test_void_settle()
    print("\n=== ALL PARLAY TESTS PASSED ===")
