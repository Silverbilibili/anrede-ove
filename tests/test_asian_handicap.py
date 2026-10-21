"""亚盘五态结算回归测试（迁移自老板历史资产，含一致性断言）"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.engine.asian_handicap import (  # noqa: E402
    normalize_handicap, split_handicap, settle_asian_handicap,
    SettlementStatus,
)

D = Decimal


def test_normalize_split():
    # 数值四分一网格
    assert normalize_handicap("0.25") == D("0.25")
    assert normalize_handicap("-0.5") == D("-0.5")
    assert normalize_handicap("0") == D("0")
    # 分裂线 → 均值为四分一
    assert normalize_handicap("0/0.5") == D("0.25")
    assert normalize_handicap("-0.5/-1") == D("-0.75")
    # 拆分
    assert split_handicap(D("0.25")) == (D("0"), D("0.5"))
    assert split_handicap(D("-0.75")) == (D("-0.5"), D("-1"))
    assert split_handicap(D("-0.5")) == (D("-0.5"),)
    assert split_handicap(D("0")) == (D("0"),)
    # 非法输入
    try:
        normalize_handicap("0.3")
        assert False, "0.3 不在四分一网格应报错"
    except ValueError:
        pass
    print("[PASS] 盘口规范化/拆分（含分裂线 0/0.5 → 0.25）")


def test_full_win_loss_push():
    # 整数盘 -1，主胜2球 → FULL_WIN
    r = settle_asian_handicap(selection_goal_difference=2, handicap="-1",
                              stake="100", decimal_odds="1.9")
    assert r.status == SettlementStatus.FULL_WIN
    assert r.gross_return == D("190")
    # 整数盘 -1，主胜1球 → PUSH 走水
    r = settle_asian_handicap(selection_goal_difference=1, handicap="-1",
                              stake="100", decimal_odds="1.9")
    assert r.status == SettlementStatus.PUSH
    assert r.gross_return == D("100")
    # 整数盘 -1，平局 → FULL_LOSS
    r = settle_asian_handicap(selection_goal_difference=0, handicap="-1",
                              stake="100", decimal_odds="1.9")
    assert r.status == SettlementStatus.FULL_LOSS
    assert r.gross_return == D("0")
    print("[PASS] 整数盘三态：赢(190)/走水(100)/输(0)")


def test_quarter_half_win_loss():
    # 受让 +0.25（+0/+0.5），平局 → HALF_WIN：50×2 + 50 = 150
    r = settle_asian_handicap(selection_goal_difference=0, handicap="+0.25",
                              stake="100", decimal_odds="2.0")
    assert r.status == SettlementStatus.HALF_WIN, r.status
    assert r.gross_return == D("150"), r.gross_return
    # 受让 +0.25，主胜1球 → FULL_WIN
    r = settle_asian_handicap(selection_goal_difference=1, handicap="+0.25",
                              stake="100", decimal_odds="2.0")
    assert r.status == SettlementStatus.FULL_WIN
    assert r.gross_return == D("200")
    # 让 -0.25（-0/-0.5），平局 → HALF_LOSS：50(走水) + 0(输) = 50
    r = settle_asian_handicap(selection_goal_difference=0, handicap="-0.25",
                              stake="100", decimal_odds="2.0")
    assert r.status == SettlementStatus.HALF_LOSS, r.status
    assert r.gross_return == D("50"), r.gross_return
    # 受让 +0.25，输1球 → FULL_LOSS
    r = settle_asian_handicap(selection_goal_difference=-1, handicap="+0.25",
                              stake="100", decimal_odds="2.0")
    assert r.status == SettlementStatus.FULL_LOSS
    assert r.gross_return == D("0")
    print("[PASS] 四分一盘五态：半赢150/全赢200/半输50/全输0")


def test_half_line_no_push():
    # -0.5 平局 → FULL_LOSS（半盘无走水）
    r = settle_asian_handicap(selection_goal_difference=0, handicap="-0.5",
                              stake="100", decimal_odds="1.95")
    assert r.status == SettlementStatus.FULL_LOSS
    assert r.gross_return == D("0")
    # +0.5 平局 → FULL_WIN
    r = settle_asian_handicap(selection_goal_difference=0, handicap="+0.5",
                              stake="100", decimal_odds="1.95")
    assert r.status == SettlementStatus.FULL_WIN
    assert r.gross_return == D("195")
    print("[PASS] 半盘无走水：-0.5 平输、+0.5 平赢")


def test_component_consistency():
    """组件返还之和必须等于统一返还（历史一致性断言）。"""
    for gd, h in [(0, "0.25"), (1, "0.25"), (-1, "0.75"), (2, "-1.5"),
                  (1, "-0.5"), (0, "0"), (-2, "+0.5")]:
        r = settle_asian_handicap(selection_goal_difference=gd, handicap=h,
                                  stake="123.45", decimal_odds="2.31")
        total = sum(c.gross_return for c in r.components)
        assert total == r.gross_return, f"{gd}@{h}: {total} != {r.gross_return}"
    print("[PASS] 组件返还一致性（7 组）")


if __name__ == "__main__":
    test_normalize_split()
    test_full_win_loss_push()
    test_quarter_half_win_loss()
    test_half_line_no_push()
    test_component_consistency()
    print("\n=== ALL ASIAN HANDICAP TESTS PASSED ===")
