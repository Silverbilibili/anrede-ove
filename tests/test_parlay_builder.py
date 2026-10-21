"""V4.1 串关引擎单元测试：组合数量 / 赔率计算 / 金额 / 边界

验证：
  - C(N,K) 组合生成正确
  - 混合过关总组合数正确
  - 赔率计算 = pi(各腿赔率)
  - 同场多选被排除
  - 失效投注项被排除
  - 系统串法组合数正确
"""
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.parlay_builder import (
    SelectionItem, ParlayCombo, BetSlip,
    generate_n_parlay_combos, generate_mixed_parlay_combos,
    generate_system_parlay_combos, build_bet_slip,
    _combo_count, available_parlay_types, PARLAY_TYPES, SYSTEM_PARLAYS,
    validate_selection_freshness, refresh_slip_validity, slip_to_dict,
)

D = Decimal


def _sel(mid: str, code: str, odds: str, source: str = "TC",
         mtype: str = "1X2") -> SelectionItem:
    return SelectionItem(
        selection_id=f"SEL_{mid}_{code}",
        match_id=mid, match_name=f"Match {mid}",
        competition="Test", kickoff_time="2026-10-10T00:00:00Z",
        market_type=mtype, market_line=None,
        selection_code=code, selection_name=code,
        odds=D(odds), source=source, quote_version=1,
        source_timestamp="2026-10-10T00:00:00Z",
    )


# ============ 组合数量测试 ============

def test_combo_count_formula():
    """验证 C(N,K) 公式正确。"""
    assert _combo_count(4, 2) == 6   # C(4,2)=6
    assert _combo_count(4, 3) == 4   # C(4,3)=4
    assert _combo_count(4, 4) == 1   # C(4,4)=1
    assert _combo_count(5, 2) == 10  # C(5,2)=10
    assert _combo_count(5, 3) == 10  # C(5,3)=10
    assert _combo_count(3, 3) == 1   # C(3,3)=1
    assert _combo_count(3, 4) == 0   # k>n -> 0
    print("[PASS] C(N,K) 公式正确")


def test_generate_2parlay_3selections():
    """3 个有效选项 → 2串1 → C(3,2)=3 组合。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M2", "AWAY", "2.0"), _sel("M3", "DRAW", "3.0")]
    combos = generate_n_parlay_combos(sels, k=2)
    assert len(combos) == 3, f"Expected 3, got {len(combos)}"
    # 验证组合内容：每组合 2 腿，全部不同 match_id
    for c in combos:
        assert c.k == 2
        mids = [s.match_id for s in c.selections]
        assert len(set(mids)) == 2
    print("[PASS] 3选2 → 3个2串1")


def test_generate_3parlay_4selections():
    """4 个有效选项 → 3串1 → C(4,3)=4 组合。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M2", "AWAY", "2.0"),
            _sel("M3", "DRAW", "3.0"), _sel("M4", "HOME", "1.8")]
    combos = generate_n_parlay_combos(sels, k=3)
    assert len(combos) == 4
    for c in combos:
        assert c.k == 3
        mids = [s.match_id for s in c.selections]
        assert len(set(mids)) == 3
    print("[PASS] 4选3 → 4个3串1")


def test_same_match_excluded():
    """同场多个选项不应进入同一组合。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M1", "AWAY", "3.0"),
            _sel("M2", "HOME", "2.0")]
    combos = generate_n_parlay_combos(sels, k=2)
    # M1选了两个方向，但同一组合中M1只能出现一次
    # C(3,2)=3 但M1的2个选择互相冲突 -> 实际只有 M1_HOME+M2 和 M1_AWAY+M2 = 2
    assert len(combos) == 2, f"Expected 2 (same match excluded), got {len(combos)}"
    print("[PASS] 同场多选被排除")


def test_invalid_selection_excluded():
    """失效投注项被排除在组合外。"""
    valid = _sel("M1", "HOME", "1.5")
    invalid = _sel("M2", "AWAY", "2.0")
    invalid.is_valid = False
    sels = [valid, invalid, _sel("M3", "DRAW", "3.0")]
    combos = generate_n_parlay_combos(sels, k=2)
    # 包含 M2 的组合标为无效
    for c in combos:
        has_invalid = any(s.match_id == "M2" for s in c.selections)
        if has_invalid:
            assert not c.is_valid
    print("[PASS] 失效投注项标记无效")


def test_mixed_parlay_types():
    """混合过关：3个选项，同时选2串1和3串1 → 3+1=4组合。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M2", "AWAY", "2.0"), _sel("M3", "DRAW", "3.0")]
    combos = generate_mixed_parlay_combos(sels, ["PARLAY2", "PARLAY3"])
    assert len(combos) == 4  # C(3,2)=3 + C(3,3)=1
    parlay2_cnt = sum(1 for c in combos if c.parlay_type == "PARLAY2")
    parlay3_cnt = sum(1 for c in combos if c.parlay_type == "PARLAY3")
    assert parlay2_cnt == 3
    assert parlay3_cnt == 1
    print("[PASS] 混合过关 2串1+3串1 = 4组合")


def test_system_3_4():
    """系统3串4：3场 → 3个2串1 + 1个3串1 = 4组合。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M2", "AWAY", "2.0"), _sel("M3", "DRAW", "3.0")]
    combos = generate_system_parlay_combos(sels, "SYS_3_4")
    assert len(combos) == 4
    assert sum(1 for c in combos if c.parlay_type == "PARLAY2") == 3
    assert sum(1 for c in combos if c.parlay_type == "PARLAY3") == 1
    print("[PASS] 系统3串4 = 3×2串1 + 1×3串1")


def test_system_4_11():
    """系统4串11：4场 → 6×2串1 + 4×3串1 + 1×4串1 = 11组合。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M2", "AWAY", "2.0"),
            _sel("M3", "DRAW", "3.0"), _sel("M4", "HOME", "1.8")]
    combos = generate_system_parlay_combos(sels, "SYS_4_11")
    assert len(combos) == 11
    assert sum(1 for c in combos if c.parlay_type == "PARLAY2") == 6
    assert sum(1 for c in combos if c.parlay_type == "PARLAY3") == 4
    assert sum(1 for c in combos if c.parlay_type == "PARLAY4") == 1
    print("[PASS] 系统4串11 = 6×2串1 + 4×3串1 + 1×4串1 = 11组合")


# ============ 赔率与金额计算测试 ============

def test_combo_odds_calculation():
    """组合赔率 = 各腿赔率之积。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M2", "AWAY", "2.0")]
    combos = generate_n_parlay_combos(sels, k=2)
    assert len(combos) == 1
    c = combos[0]
    assert c.combo_odds == D("1.5") * D("2.0") == D("3.0")
    assert c.theoretical_return == c.combo_odds * c.unit_stake
    print("[PASS] 组合赔率 = 1.5 × 2.0 = 3.0")


def test_bet_slip_total():
    """投注单总金额 = 单注金额 × 总组合数。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M2", "AWAY", "2.0"), _sel("M3", "DRAW", "3.0")]
    slip = build_bet_slip(sels, parlay_types=["PARLAY2"], unit_stake=D("2"))
    assert slip.total_combos == 3  # C(3,2)=3
    assert slip.total_stake == D("6")  # 3 × 2
    # 最高赔率组合: M2(2.0) × M3(3.0) = 6.0, 返还 = 6.0 × 2 = 12.0
    assert slip.max_return == D("2.0") * D("3.0") * D("2"), f"Expected 12.0, got {slip.max_return}"
    print(f"[PASS] bet slip: {slip.total_combos} units x {slip.unit_stake} = {slip.total_stake}")


def test_bet_slip_dedup():
    """重复选择自动去重。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M1", "HOME", "1.5"), _sel("M2", "AWAY", "2.0")]
    slip = build_bet_slip(sels, parlay_types=["PARLAY2"])
    assert len(slip.selections) == 2  # M1 HOME去重
    assert slip.total_combos == 1     # C(2,2)=1
    print("[PASS] 重复选择去重")


def test_available_parlay_types():
    """可用过关方式推荐正确。"""
    types = available_parlay_types(4)
    # PARLAY2, PARLAY3, PARLAY4 都应可用
    t2 = next(t for t in types if t["type"] == "PARLAY2")
    assert t2["available"] and t2["combo_count"] == 6
    t3 = next(t for t in types if t["type"] == "PARLAY3")
    assert t3["available"] and t3["combo_count"] == 4
    t4 = next(t for t in types if t["type"] == "PARLAY4")
    assert t4["available"] and t4["combo_count"] == 1
    t5 = next(t for t in types if t["type"] == "PARLAY5")
    assert not t5["available"]
    sys_types = [t for t in types if t["type"] in ("SYS_3_4", "SYS_4_11", "SYS_5_26")]
    # SYS_3_4 needs exactly 3 selections -> NOT available for 4
    t34 = next((t for t in sys_types if t["type"] == "SYS_3_4"), None)
    assert t34 is not None, f"SYS_3_4 not found in types: {[t['type'] for t in types]}"
    assert t34["available"] is False, f"SYS_3_4 should NOT be available for 4 selections (needs exactly 3): {t34}"
    assert t34["combo_count"] == 0, f"SYS_3_4 combo_count expected 0, got {t34.get('combo_count')}"
    # SYS_4_11 needs exactly 4 selections -> AVAILABLE
    t411 = next((t for t in sys_types if t["type"] == "SYS_4_11"), None)
    assert t411 is not None, "SYS_4_11 not found in types"
    assert t411["available"] is True, f"SYS_4_11 should be available for 4 selections: {t411}"
    assert t411["combo_count"] == 11, f"SYS_4_11 combo_count expected 11, got {t411.get('combo_count')}"
    # SYS_5_26 needs exactly 5 -> NOT available for 4
    t526 = next((t for t in sys_types if t["type"] == "SYS_5_26"), None)
    if t526:
        assert not t526["available"], "SYS_5_26 should NOT be available for 4 selections"
    print("[PASS] 可用过关方式推荐正确")


def test_empty_selections():
    """空选择列表 -> 0组合。"""
    combos = generate_n_parlay_combos([], k=2)
    assert len(combos) == 0
    slip = build_bet_slip([])
    assert slip.total_combos == 0
    assert slip.total_stake == D("0")
    print("[PASS] 空选择列表处理正确")


def test_slip_to_dict():
    """序列化正确。"""
    sels = [_sel("M1", "HOME", "1.5"), _sel("M2", "AWAY", "2.0")]
    slip = build_bet_slip(sels, parlay_types=["PARLAY2"])
    d = slip_to_dict(slip)
    assert d["total_combos"] == 1
    assert d["data_summary"]["total_selections"] == 2
    assert d["data_summary"]["valid_selections"] == 2
    assert len(d["combos"]) == 1
    assert d["combos"][0]["k"] == 2
    print("[PASS] slip_to_dict 序列化正确")


if __name__ == "__main__":
    tests = [
        test_combo_count_formula,
        test_generate_2parlay_3selections,
        test_generate_3parlay_4selections,
        test_same_match_excluded,
        test_invalid_selection_excluded,
        test_mixed_parlay_types,
        test_system_3_4,
        test_system_4_11,
        test_combo_odds_calculation,
        test_bet_slip_total,
        test_bet_slip_dedup,
        test_available_parlay_types,
        test_empty_selections,
        test_slip_to_dict,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} tests passed")
