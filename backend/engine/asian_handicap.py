"""亚盘五态结算（从老板历史资产迁移，直接复用候选）

来源：football_arbitrage_monitor/settlement/asian_handicap.py
     + domain/handicap.py（normalize/split）
状态：FULL_WIN / FULL_LOSS / PUSH / HALF_WIN / HALF_LOSS
规则：adjusted_margin = 所选方净胜球 + 盘口；>0 赢、=0 走水、<0 输
      四分一盘拆两个等额半盘组件，组件结果聚合为五态；
      WIN 返 stake×odds、PUSH 返 stake、LOSS 返 0。
纯 Decimal/Fraction 精确运算，无 DB 依赖。
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

ZERO = Decimal("0")
TWO = Decimal("2")
QUARTER = Decimal("0.25")
HALF = Decimal("0.5")


# ----------------------------- 精确工具 -----------------------------
def _exact(fraction: Fraction) -> Decimal:
    """Fraction → 精确 Decimal（历史 _decimal_from_fraction_exact 同义）。"""
    return Decimal(fraction.numerator) / Decimal(fraction.denominator)


def _require_decimal(value, name: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{name} must be a valid decimal") from exc
    raise TypeError(f"{name} must be Decimal or str")


# ----------------------------- 盘口规范化/拆分 -----------------------------
def normalize_handicap(value) -> Decimal:
    """数值或分裂线字符串（0/0.5、-0.5/-1 等）→ 四分一网格 Decimal。"""
    if isinstance(value, Decimal):
        return _validate_quarter_grid(value)
    if not isinstance(value, str):
        raise TypeError(f"handicap must be Decimal or str, got {type(value).__name__}")
    text = "".join(value.split())
    if not text:
        raise ValueError("handicap must not be empty")
    if "/" not in text:
        return _validate_quarter_grid(_parse_decimal(text))
    if text.count("/") != 1:
        raise ValueError("split handicap must contain exactly two components")
    return _parse_split_handicap(text)


def split_handicap(handicap: Decimal) -> tuple[Decimal, ...]:
    """四分一盘 → 相邻两个半盘；整数/半球盘 → 单组件。"""
    normalized = _validate_quarter_grid(_require_decimal(handicap, "handicap"))
    magnitude = abs(Fraction(normalized))
    if magnitude % Fraction(HALF) == 0:
        return (normalized,)
    sign = -1 if normalized < ZERO else 1
    return (
        _canonical(_exact(sign * (magnitude - Fraction(QUARTER)))),
        _canonical(_exact(sign * (magnitude + Fraction(QUARTER)))),
    )


def _parse_split_handicap(text: str) -> Decimal:
    global_sign = Fraction(1)
    unsigned_text = text
    if text[0] in "+-":
        remainder = text[1:]
        if "+" not in remainder and "-" not in remainder:
            global_sign = Fraction(-1) if text[0] == "-" else Fraction(1)
            unsigned_text = remainder
    left_text, right_text = unsigned_text.split("/")
    if not left_text or not right_text:
        raise ValueError("split handicap components must not be empty")
    left = _exact(global_sign * Fraction(_parse_decimal(left_text)))
    right = _exact(global_sign * Fraction(_parse_decimal(right_text)))
    left = _validate_half_grid(left, "left split component")
    right = _validate_half_grid(right, "right split component")
    if (left < ZERO < right) or (right < ZERO < left):
        raise ValueError("split handicap components must have the same sign")
    if abs(Fraction(left) - Fraction(right)) != Fraction(HALF):
        raise ValueError("split handicap components must be adjacent half lines")
    return _validate_quarter_grid(_exact((Fraction(left) + Fraction(right)) / 2))


def _parse_decimal(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise ValueError(f"invalid handicap value: {text!r}") from error
    if not value.is_finite():
        raise ValueError("handicap must be finite")
    return value


def _validate_half_grid(value: Decimal, name: str) -> Decimal:
    if Fraction(value) % Fraction(HALF) != 0:
        raise ValueError(f"{name} must be an integer- or half-ball line")
    return _canonical(value)


def _validate_quarter_grid(value: Decimal) -> Decimal:
    finite_value = _require_decimal(value, "handicap")
    if Fraction(finite_value) % Fraction(QUARTER) != 0:
        raise ValueError("handicap must be on the quarter-ball grid")
    return _canonical(finite_value)


def _canonical(value: Decimal) -> Decimal:
    if value == ZERO:
        return ZERO
    sign, digits, exponent = value.as_tuple()
    trimmed = list(digits)
    while trimmed and trimmed[-1] == 0:
        trimmed.pop()
        exponent += 1
    return Decimal((sign, tuple(trimmed), exponent))


# ----------------------------- 五态结算 -----------------------------
class ComponentOutcome:
    WIN = "WIN"
    PUSH = "PUSH"
    LOSS = "LOSS"


class SettlementStatus:
    FULL_WIN = "FULL_WIN"
    FULL_LOSS = "FULL_LOSS"
    PUSH = "PUSH"
    HALF_WIN = "HALF_WIN"
    HALF_LOSS = "HALF_LOSS"


@dataclass(frozen=True, slots=True)
class ComponentSettlement:
    handicap: Decimal
    stake: Decimal
    outcome: str
    gross_return: Decimal


@dataclass(frozen=True, slots=True)
class AsianHandicapSettlement:
    status: str
    selection_goal_difference: int
    handicap: Decimal
    stake: Decimal
    decimal_odds: Decimal
    gross_return: Decimal
    components: tuple[ComponentSettlement, ...]


def settle_asian_handicap(*, selection_goal_difference: int,
                          handicap, stake, decimal_odds) -> AsianHandicapSettlement:
    """五态结算：拆分 → 组件判定 → 聚合 → 现金。总返还=组件之和（一致性断言）。"""
    if not isinstance(selection_goal_difference, int) or isinstance(selection_goal_difference, bool):
        raise TypeError("selection_goal_difference must be int")
    normalized = normalize_handicap(_require_decimal(handicap, "handicap"))
    stake_d = _require_decimal(stake, "stake")
    odds_d = _require_decimal(decimal_odds, "decimal_odds")

    components = split_handicap(normalized)
    component_stake = _exact(Fraction(stake_d) / len(components))
    component_settlements = []
    for component_handicap in components:
        outcome = _resolve_component_outcome(selection_goal_difference, component_handicap)
        gross = {
            ComponentOutcome.WIN: _exact(Fraction(component_stake) * Fraction(odds_d)),
            ComponentOutcome.PUSH: component_stake,
            ComponentOutcome.LOSS: ZERO,
        }[outcome]
        component_settlements.append(
            ComponentSettlement(component_handicap, component_stake, outcome, gross)
        )
    total = sum((c.gross_return for c in component_settlements), ZERO)
    status = _aggregate_status(tuple(c.outcome for c in component_settlements))
    return AsianHandicapSettlement(
        status=status,
        selection_goal_difference=selection_goal_difference,
        handicap=normalized,
        stake=stake_d,
        decimal_odds=odds_d,
        gross_return=total,
        components=tuple(component_settlements),
    )


def _resolve_component_outcome(selection_goal_difference: int, handicap: Decimal) -> str:
    adjusted = _exact(Fraction(selection_goal_difference) + Fraction(handicap))
    if adjusted > ZERO:
        return ComponentOutcome.WIN
    if adjusted == ZERO:
        return ComponentOutcome.PUSH
    return ComponentOutcome.LOSS


def _aggregate_status(outcomes: tuple[str, ...]) -> str:
    win = outcomes.count(ComponentOutcome.WIN)
    loss = outcomes.count(ComponentOutcome.LOSS)
    push = outcomes.count(ComponentOutcome.PUSH)
    n = len(outcomes)
    if win == n:
        return SettlementStatus.FULL_WIN
    if loss == n:
        return SettlementStatus.FULL_LOSS
    if push == n:
        return SettlementStatus.PUSH
    if win == 1 and push == 1 and n == 2:
        return SettlementStatus.HALF_WIN
    if loss == 1 and push == 1 and n == 2:
        return SettlementStatus.HALF_LOSS
    raise ValueError(f"unsupported component outcome combination: {outcomes!r}")


# ----------------------------- 状态空间（引擎用） -----------------------------
def ah_states(line) -> list[str]:
    """主队让球盘 line 的净胜球等价状态（gd 为整数）。

    组件 c WIN iff gd > -c。按整数/半整边界切分 gd 域，
    再按 (主队结算, 客队结算) 签名合并等价态，避免多余走水暗示。
    返回形如 "gd<=0"、"gd=1"、"gd>=2" 的状态标签。
    """
    from decimal import ROUND_DOWN
    normalized = normalize_handicap(_require_decimal(line, "line"))
    comps = split_handicap(normalized)
    pts = set()
    for c in comps:
        b = -c
        if b == b.to_integral_value():
            pts.add(int(b))      # 整数边界：gd<b / =b / >b 三分
        else:
            pts.add(int(b))      # 半整边界：gd<=floor / >=ceil 两分
    pts = sorted(pts)

    def settle_pair(gd: int) -> tuple[str, str]:
        home = settle_asian_handicap(selection_goal_difference=gd, handicap=normalized,
                                     stake="1", decimal_odds="2").status
        away = settle_asian_handicap(selection_goal_difference=-gd, handicap=-normalized,
                                     stake="1", decimal_odds="2").status
        return (home, away)

    # 候选区间：(-∞,p0]、p0、…、pn、[pn+1,+∞)
    candidates: list[tuple[str, int]] = []
    candidates.append((f"gd<={pts[0] - 1}", pts[0] - 1))
    for i, p in enumerate(pts):
        candidates.append((f"gd={p}", p))
        if i + 1 < len(pts):
            if pts[i + 1] > p + 1:
                candidates.append((f"gd={p + 1}..{pts[i + 1] - 1}", p + 1))
        else:
            candidates.append((f"gd>={p + 1}", p + 1))

    merged: dict[tuple[str, str], tuple[str, int]] = {}
    for label, gd in candidates:
        sig = settle_pair(gd)
        if sig not in merged:
            merged[sig] = (label, gd)
    return [label for label, _ in merged.values()]


def gd_from_state(state: str) -> int:
    """状态标签 → 代表净胜球整数。"""
    text = state.replace("gd", "").replace(" ", "")
    if ".." in text:
        return int(text.split("..")[0])
    if "<=" in text:
        return int(text.split("<=")[1])
    if ">=" in text:
        return int(text.split(">=")[1])
    if "=" in text:
        return int(text.split("=")[1])
    raise ValueError(f"unparseable AH state: {state!r}")


def ah_settle_state(state: str, bet: dict, stake, line_d) -> Decimal:
    """引擎用：state（净胜球等价态）+ bet → 该状态下返还（含本金）。

    bet 需含 selection_code（HOME/AWAY）；主队盘口 = line_d，客队盘口 = -line_d；
    客队侧净胜球 = -gd。
    """
    gd = gd_from_state(state)
    code = bet["selection_code"]
    h = line_d if code == "HOME" else -line_d
    gd_sel = gd if code == "HOME" else -gd
    r = settle_asian_handicap(selection_goal_difference=gd_sel, handicap=h,
                              stake=stake, decimal_odds=bet["odds_decimal"])
    return r.gross_return
