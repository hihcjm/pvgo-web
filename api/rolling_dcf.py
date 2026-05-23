"""
Rolling DCF Valuation Model — Damodaran Methodology (v3)
=========================================================
Three-tier lifecycle engine: Mature / Hyper-Growth / Cyclical
Produces year-end target prices for 2026, 2027, 2028.

Key design rules per lifecycle:

  MATURE (5-year horizon post-base):
    - Growth decays linearly -> risk_free_rate
    - Margin: hold 2028 consensus, mild convergence to industry avg
    - Reinvestment: DeltaRev / sales_to_capital

  HYPER-GROWTH (10-year horizon):
    - Growth decays linearly -> risk_free_rate; Year-10 revenue capped at max_tam_revenue
    - Margin: converge linearly to target_positive_margin by Year 10
    - Reinvestment: strictly DeltaRev / sales_to_capital
    - EV adjusted by probability_of_survival

  CYCLICAL (10-year horizon):
    - Year 4-10 margin converges to 10-year historical average (mid-cycle)
    - Reinvestment cap: min(DeltaRev/S2C, Revenue * max_capex_to_sales)
    - Ignores 2028 consensus growth/margin for long-run extrapolation

Terminal Value (all types): FCF_{Final+1} / (WACC - rf)   [Guard 4]
WACC spread over rf enforced >= 100 bps                    [Guard 4]
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd


# ---------------------------------------------------------------------------
# 1. TYPE ALIASES & CONSTANTS
# ---------------------------------------------------------------------------

FirmType = Literal["mature", "hyper_growth", "cyclical"]

CONSENSUS_YEARS: tuple[int, ...] = (2026, 2027, 2028)
ROLLING_BASES:   tuple[int, ...] = (2026, 2027, 2028)
MATURE_HORIZON:  int = 5
GROWTH_HORIZON:  int = 10


# ---------------------------------------------------------------------------
# 2. DATA STRUCTURES
# ---------------------------------------------------------------------------

@dataclass
class Financials:
    """Latest-year actuals (FY2025 snapshot)."""
    revenue:              float          # $B
    ebit_margin:          float          # fraction  (negative allowed)
    revenue_growth:       float          # YoY fraction
    cash:                 float          # $B  cash + ST investments
    debt:                 float          # $B  total debt
    shares:               float          # B diluted shares
    # For cyclical classification: historical EBIT margins (list of fractions)
    hist_ebit_margins:    list[float] = field(default_factory=list)


@dataclass
class ConsensusYear:
    year:        int
    revenue:     float   # $B
    ebit_margin: float   # fraction  (negative allowed)


@dataclass
class DCFParams:
    # ── rate assumptions ─────────────────────────────────────────────────────
    risk_free_rate:          float = 0.044   # US 10-Y T-note (= terminal g)
    erp:                     float = 0.055   # Equity Risk Premium
    beta:                    float = 1.0
    tax_rate:                float = 0.21

    # ── Mature / generic ─────────────────────────────────────────────────────
    target_industry_margin:  float = 0.15    # Guard 2 / Mature margin target
    sales_to_capital:        float = 1.5     # Guard 3: $rev per $reinvestment

    # ── Hyper-Growth specific ─────────────────────────────────────────────────
    target_positive_margin:  float = 0.15    # margin to converge to by Y10
    max_tam_revenue:         float = float("inf")  # absolute revenue cap ($B)
    probability_of_survival: float = 1.0     # 0–1; EV multiplier

    # ── Cyclical specific ─────────────────────────────────────────────────────
    max_capex_to_sales:      float = 0.12    # reinvestment ceiling as % of revenue
    # mid_cycle_margin computed from hist_ebit_margins; can be pre-set as override
    mid_cycle_margin_override: Optional[float] = None

    # ── computed ──────────────────────────────────────────────────────────────
    wacc: float = field(init=False, repr=True, default=0.0)

    def __post_init__(self) -> None:
        raw = self.risk_free_rate + self.beta * self.erp
        # Guard 4: WACC must strictly exceed rf; enforce minimum spread = 100 bps
        self.wacc = max(raw, self.risk_free_rate + 0.01)

    @property
    def terminal_growth(self) -> float:
        """Guard 4: terminal g is always locked to risk_free_rate."""
        return self.risk_free_rate


# ---------------------------------------------------------------------------
# 3. FIRM CLASSIFIER ENGINE
# ---------------------------------------------------------------------------

def classify_firm(
    f: Financials,
    *,
    cyclical_flag: bool = False,
    cyclical_variance_threshold: float = 0.04,
) -> tuple[FirmType, int]:
    """
    Assign one of three lifecycle types and the appropriate projection horizon.

    Classification rules (in priority order):
      1. "hyper_growth": EBIT margin < 0  OR  rev growth > 20%
         (unprofitable / extreme growth overrides cyclical detection)
      2. "cyclical"    : explicit flag  OR  high historical EBIT-margin variance
      3. "mature"      : EBIT margin > 0  AND  rev growth <= 10%
         (growth 10-20% with positive margin also falls here as default)

    Parameters
    ----------
    f                           : Financials snapshot (FY2025)
    cyclical_flag               : set True when the caller knows the sector is cyclical
    cyclical_variance_threshold : stdev of hist_ebit_margins above which -> cyclical
    """
    # Hyper-growth / unprofitable check FIRST (overrides cyclical)
    if f.ebit_margin < 0 or f.revenue_growth > 0.20:
        return "hyper_growth", GROWTH_HORIZON

    # Cyclical check (only for profitable, moderate-growth firms)
    if cyclical_flag:
        return "cyclical", GROWTH_HORIZON
    if len(f.hist_ebit_margins) >= 3:
        margin_std = statistics.stdev(f.hist_ebit_margins)
        if margin_std >= cyclical_variance_threshold:
            return "cyclical", GROWTH_HORIZON

    # Default: mature
    return "mature", MATURE_HORIZON


# ---------------------------------------------------------------------------
# 4. INTERNAL HELPERS
# ---------------------------------------------------------------------------

def _linspace(start: float, end: float, step: int, total_steps: int) -> float:
    """Linear interpolation: step=1 -> start, step=total_steps -> end."""
    if total_steps <= 1:
        return end
    alpha = (step - 1) / (total_steps - 1)
    return start * (1 - alpha) + end * alpha


def _discount(value: float, wacc: float, n_years: int) -> float:
    return value / (1 + wacc) ** n_years


def _terminal_value(
    fcf_y_next: float,
    wacc:       float,
    terminal_g: float,
    base_year:  int,
    tv_year:    int,
) -> float:
    """
    Guard 4: TV = FCF_{tv_year} / (WACC - rf)
    Discounted back to base_year.
    Raises ValueError if spread <= 0.
    """
    spread = wacc - terminal_g
    if spread <= 0:
        raise ValueError(
            f"Guard 4 violated: WACC({wacc:.4f}) <= terminal_g({terminal_g:.4f}). "
            "Increase beta or ERP."
        )
    tv_undiscounted = fcf_y_next / spread
    n = tv_year - base_year
    return _discount(tv_undiscounted, wacc, n)


# ---------------------------------------------------------------------------
# 5. CONSENSUS -> FCF BRIDGE  (2026-2028)
# ---------------------------------------------------------------------------

def consensus_to_fcf(
    consensus: list[ConsensusYear],
    actuals:   Financials,
    params:    DCFParams,
) -> pd.DataFrame:
    """
    Convert consensus years to FCF rows.
    Reinvestment = DeltaRev / sales_to_capital  (Guard 3, all types).
    """
    rows: list[dict] = []
    prev_rev = actuals.revenue

    for c in sorted(consensus, key=lambda x: x.year):
        ebit  = c.revenue * c.ebit_margin
        nopat = ebit * (1 - params.tax_rate)
        reinv = (c.revenue - prev_rev) / params.sales_to_capital
        fcf   = nopat - reinv
        g     = (c.revenue / prev_rev - 1) if prev_rev else float("nan")

        rows.append({
            "year":         c.year,
            "revenue":      round(c.revenue, 4),
            "rev_growth":   round(g * 100, 2) if math.isfinite(g) else None,
            "ebit_margin":  round(c.ebit_margin * 100, 2),
            "ebit":         round(ebit, 4),
            "nopat":        round(nopat, 4),
            "reinvestment": round(reinv, 4),
            "fcf":          round(fcf, 4),
            "source":       "consensus",
        })
        prev_rev = c.revenue

    return pd.DataFrame(rows).set_index("year")


# ---------------------------------------------------------------------------
# 6. EXTRAPOLATION ENGINE  (lifecycle-specific)
# ---------------------------------------------------------------------------

def project_financials(
    consensus:     list[ConsensusYear],
    actuals:       Financials,
    params:        DCFParams,
    firm_type:     FirmType,
    horizon:       int,
) -> pd.DataFrame:
    """
    Generate FCF rows beyond the last consensus year.

    base_year = last consensus year (e.g. 2028)
    Extrapolation covers [base_year+1 .. base_year+horizon].
    An additional Y+11 row is appended for the terminal value numerator.
    """
    sorted_con  = sorted(consensus, key=lambda c: c.year)
    last        = sorted_con[-1]
    second_last = sorted_con[-2] if len(sorted_con) >= 2 else None

    # Seed growth from last two consensus years
    if second_last and second_last.revenue > 0:
        seed_growth = last.revenue / second_last.revenue - 1
    else:
        seed_growth = params.risk_free_rate + 0.02

    seed_margin = last.ebit_margin
    from_year   = last.year + 1
    to_year     = last.year + horizon
    # +1 extra year for TV FCF numerator
    total_years = horizon + 1

    # Mid-cycle margin for cyclical
    if firm_type == "cyclical":
        if params.mid_cycle_margin_override is not None:
            mid_cycle = params.mid_cycle_margin_override
        elif actuals.hist_ebit_margins:
            mid_cycle = statistics.mean(actuals.hist_ebit_margins)
        else:
            mid_cycle = params.target_industry_margin
    else:
        mid_cycle = params.target_industry_margin  # unused for non-cyclical

    rows:     list[dict] = []
    prev_rev: float      = last.revenue

    for step in range(1, total_years + 1):
        yr = last.year + step

        # ── A. MATURE ───────────────────────────────────────────────────────
        if firm_type == "mature":
            g = _linspace(seed_growth, params.risk_free_rate, step, horizon)
            g = max(g, params.risk_free_rate)            # Guard 1: floor at rf

            # Maintain 2028 margin; slight drift toward industry avg in last 2 steps
            if step <= horizon - 2:
                m = seed_margin
            else:
                blend = (step - (horizon - 2)) / 2      # 0->1 over last 2 steps
                m = seed_margin * (1 - blend) + params.target_industry_margin * blend

        # ── B. HYPER-GROWTH ────────────────────────────────────────────────
        elif firm_type == "hyper_growth":
            g = _linspace(seed_growth, params.risk_free_rate, step, horizon)
            g = max(g, params.risk_free_rate)            # Guard 1: floor at rf

            m = _linspace(seed_margin, params.target_positive_margin, step, horizon)

        # ── C. CYCLICAL ─────────────────────────────────────────────────────
        else:
            # Growth: Years 1-3 decay seed_growth -> mid-cycle; Years 4+ hold mid-cycle
            mid_cycle_growth = params.risk_free_rate + 0.02
            if step <= 3:
                g = _linspace(seed_growth, mid_cycle_growth, step, 3)
            else:
                g = mid_cycle_growth
            g = max(g, params.risk_free_rate)

            # Margin: Years 1-3 hold seed margin; Years 4+ converge to mid_cycle avg
            if step <= 3:
                m = seed_margin
            else:
                cyc_step  = step - 3
                cyc_total = horizon - 3
                m = _linspace(seed_margin, mid_cycle, cyc_step, max(cyc_total, 1))

        # ── Revenue ─────────────────────────────────────────────────────────
        rev = prev_rev * (1 + g)

        # Hyper-growth: TAM cap
        if firm_type == "hyper_growth" and rev > params.max_tam_revenue:
            rev = params.max_tam_revenue
            g   = (rev / prev_rev - 1) if prev_rev else 0.0

        ebit  = rev * m
        nopat = ebit * (1 - params.tax_rate)

        # ── Reinvestment ────────────────────────────────────────────────────
        reinv_base = (rev - prev_rev) / params.sales_to_capital
        if firm_type == "cyclical":
            reinv_cap = rev * params.max_capex_to_sales
            reinv     = min(reinv_base, reinv_cap)
        else:
            reinv = reinv_base                           # Guard 3

        fcf = nopat - reinv

        rows.append({
            "year":         yr,
            "revenue":      round(rev, 4),
            "rev_growth":   round(g * 100, 2),
            "ebit_margin":  round(m * 100, 2),
            "ebit":         round(ebit, 4),
            "nopat":        round(nopat, 4),
            "reinvestment": round(reinv, 4),
            "fcf":          round(fcf, 4),
            "source":       "extrapolated",
        })
        prev_rev = rev

    return pd.DataFrame(rows).set_index("year")


# ---------------------------------------------------------------------------
# 7. ROLLING DCF CALCULATOR
# ---------------------------------------------------------------------------

def _pv_stream(fcf_series: pd.Series, wacc: float, base_year: int) -> float:
    """Sum of PVs for each FCF year discounted to base_year."""
    total = 0.0
    for yr, fcf in fcf_series.items():
        n = int(yr) - base_year
        if n <= 0:
            continue
        total += float(fcf) / (1 + wacc) ** n
    return total


def calculate_rolling_targets(
    actuals:       Financials,
    consensus:     list[ConsensusYear],
    params:        DCFParams | None = None,
    *,
    cyclical_flag: bool = False,
) -> pd.DataFrame:
    """
    Compute year-end target prices for 2026, 2027, 2028.

    For each base_year T in {2026, 2027, 2028}:
      Base Cash  = Cash_{T-1} + FCF_T
      EV         = PV(FCF_{T+1 .. T+horizon}) + PV(TV_{T+horizon+1})
      Equity     = EV * survival_prob + Base Cash - Debt
      TP         = Equity / Shares

    Terminal Value: FCF_{T+horizon+1} / (WACC - rf)   discounted to T  [Guard 4]
    """
    if params is None:
        params = DCFParams()

    firm_type, horizon = classify_firm(actuals, cyclical_flag=cyclical_flag)
    wacc               = params.wacc
    terminal_g         = params.terminal_growth   # Guard 4: == rf

    # Survival probability only applied for hyper_growth
    survival = params.probability_of_survival if firm_type == "hyper_growth" else 1.0

    # Build consensus FCF table (2026-2028)
    con_df = consensus_to_fcf(consensus, actuals, params)

    # Build extrapolated table (2029 .. 2028+horizon+1)
    ext_df = project_financials(
        consensus  = consensus,
        actuals    = actuals,
        params     = params,
        firm_type  = firm_type,
        horizon    = horizon,
    )

    all_fcf: pd.Series = pd.concat([con_df["fcf"], ext_df["fcf"]])

    # Rolling cash accumulation: Cash_T = Cash_{T-1} + FCF_T
    rolling_cash: dict[int, float] = {}
    cash_prev = actuals.cash
    for yr in sorted(c.year for c in consensus):
        fcf_t = float(all_fcf.loc[yr])
        rolling_cash[yr] = cash_prev + fcf_t
        cash_prev = rolling_cash[yr]

    results: list[dict] = []

    for base_year in ROLLING_BASES:
        proj_start  = base_year + 1
        proj_end    = base_year + horizon
        tv_fcf_year = proj_end + 1                 # FCF_Y+horizon+1 for TV numerator

        window_fcf = all_fcf[
            (all_fcf.index >= proj_start) & (all_fcf.index <= proj_end)
        ]

        pv_fcfs = _pv_stream(window_fcf, wacc, base_year)

        # TV FCF: look up tv_fcf_year; fall back to last available row
        if tv_fcf_year in all_fcf.index:
            fcf_y_tv = float(all_fcf.loc[tv_fcf_year])
        else:
            fcf_y_tv = float(all_fcf.iloc[-1])

        pv_tv = _terminal_value(fcf_y_tv, wacc, terminal_g, base_year, tv_fcf_year)

        ev           = (pv_fcfs + pv_tv) * survival
        base_cash    = rolling_cash.get(base_year, actuals.cash)
        equity_value = ev + base_cash - actuals.debt
        target_price = equity_value / actuals.shares if actuals.shares > 0 else float("nan")

        results.append({
            "valuation_date":   base_year,
            "firm_type":        firm_type,
            "horizon":          horizon,
            "wacc_pct":         round(wacc * 100, 2),
            "terminal_g_pct":   round(terminal_g * 100, 2),
            "survival_prob":    round(survival, 2),
            "proj_window":      f"{proj_start}-{proj_end}",
            "tv_fcf_year":      tv_fcf_year,
            "fcf_y_tv":         round(fcf_y_tv, 4),
            "pv_fcfs_B":        round(pv_fcfs, 3),
            "pv_tv_B":          round(pv_tv, 3),
            "ev_B":             round(ev, 3),
            "base_cash_B":      round(base_cash, 3),
            "debt_B":           round(actuals.debt, 3),
            "equity_B":         round(equity_value, 3),
            "shares_B":         round(actuals.shares, 4),
            "target_price":     round(target_price, 2),
        })

    return pd.DataFrame(results).set_index("valuation_date")


# ---------------------------------------------------------------------------
# 8. DIAGNOSTIC: FULL SCHEDULE
# ---------------------------------------------------------------------------

def build_full_schedule(
    actuals:       Financials,
    consensus:     list[ConsensusYear],
    params:        DCFParams,
    *,
    cyclical_flag: bool = False,
) -> pd.DataFrame:
    """Return the concatenated consensus + extrapolated FCF schedule."""
    firm_type, horizon = classify_firm(actuals, cyclical_flag=cyclical_flag)
    con_df = consensus_to_fcf(consensus, actuals, params)
    ext_df = project_financials(
        consensus  = consensus,
        actuals    = actuals,
        params     = params,
        firm_type  = firm_type,
        horizon    = horizon,
    )
    return pd.concat([con_df, ext_df])


# ---------------------------------------------------------------------------
# 9. MOCK EXECUTION  — three firms, one per lifecycle type
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    pd.set_option("display.float_format", "{:.3f}".format)
    pd.set_option("display.max_columns", 20)
    pd.set_option("display.width", 140)

    # ── FIRM A: MATURE  (AAPL-proxy) ────────────────────────────────────────
    mature_actuals = Financials(
        revenue        = 416.16,
        ebit_margin    = 0.318,
        revenue_growth = 0.064,
        cash           = 53.77,
        debt           = 97.34,
        shares         = 15.12,
        hist_ebit_margins = [0.29, 0.31, 0.31, 0.30, 0.318],
    )
    mature_consensus = [
        ConsensusYear(year=2026, revenue=486.77, ebit_margin=0.330),
        ConsensusYear(year=2027, revenue=528.50, ebit_margin=0.338),
        ConsensusYear(year=2028, revenue=573.00, ebit_margin=0.342),
    ]
    mature_params = DCFParams(
        risk_free_rate=0.044, erp=0.055, beta=1.24,
        tax_rate=0.155,
        target_industry_margin=0.28,
        sales_to_capital=2.0,
        probability_of_survival=1.0,
    )

    # ── FIRM B: HYPER-GROWTH  (IonQ-style) ──────────────────────────────────
    hyper_actuals = Financials(
        revenue        = 0.043,
        ebit_margin    = -4.0,           # -400%  stress
        revenue_growth = 2.0,            # 200%   stress
        cash           = 0.38,
        debt           = 0.05,
        shares         = 0.67,
        hist_ebit_margins = [-3.5, -2.0, -4.0],
    )
    hyper_consensus = [
        ConsensusYear(year=2026, revenue=0.105, ebit_margin=-2.5),
        ConsensusYear(year=2027, revenue=0.230, ebit_margin=-1.2),
        ConsensusYear(year=2028, revenue=0.420, ebit_margin=-0.4),
    ]
    hyper_params = DCFParams(
        risk_free_rate=0.044, erp=0.055, beta=2.10,
        tax_rate=0.0,
        target_positive_margin=0.15,
        target_industry_margin=0.15,
        sales_to_capital=0.8,
        max_tam_revenue=10.0,            # $10B TAM cap
        probability_of_survival=0.70,   # 30% bankruptcy risk
    )

    # ── FIRM C: CYCLICAL  (Steel-proxy) ─────────────────────────────────────
    cyclical_actuals = Financials(
        revenue        = 22.0,
        ebit_margin    = 0.18,           # current peak margin
        revenue_growth = 0.08,
        cash           = 1.5,
        debt           = 6.0,
        shares         = 1.2,
        # wide historical spread -> triggers cyclical classification
        hist_ebit_margins = [0.04, 0.12, 0.20, 0.18, 0.06, 0.14, 0.22, 0.09, 0.18, 0.10],
    )
    cyclical_consensus = [
        ConsensusYear(year=2026, revenue=23.5, ebit_margin=0.16),
        ConsensusYear(year=2027, revenue=24.8, ebit_margin=0.13),
        ConsensusYear(year=2028, revenue=25.5, ebit_margin=0.11),
    ]
    cyclical_params = DCFParams(
        risk_free_rate=0.044, erp=0.055, beta=1.40,
        tax_rate=0.21,
        target_industry_margin=0.13,    # industry avg (unused; mid_cycle from hist)
        sales_to_capital=1.2,
        max_capex_to_sales=0.10,        # 10% reinvestment ceiling
    )

    scenarios = [
        ("MATURE   [AAPL-proxy: 6% growth, 32% margin]",   mature_actuals,   mature_consensus,   mature_params,   False),
        ("HYPER-GROWTH [IonQ: 200% growth, -400% margin]", hyper_actuals,    hyper_consensus,    hyper_params,    False),
        ("CYCLICAL [Steel-proxy: wide margin variance]",    cyclical_actuals, cyclical_consensus, cyclical_params, True),
    ]

    for label, actuals, consensus, params, cyc_flag in scenarios:
        firm_type, horizon = classify_firm(actuals, cyclical_flag=cyc_flag)

        print("=" * 78)
        print(f"  {label}")
        print("=" * 78)
        print(f"  Firm type      : {firm_type.upper()}   |  Horizon: {horizon} yrs")
        print(f"  WACC           : {params.wacc*100:.2f}%"
              f"  (rf={params.risk_free_rate*100:.1f}%"
              f"  b={params.beta}"
              f"  ERP={params.erp*100:.1f}%)")
        print(f"  Terminal g     : {params.terminal_growth*100:.2f}%  [Guard 4: = rf]")
        print(f"  Spread         : {(params.wacc - params.risk_free_rate)*100:.2f}%  [>0 enforced]")
        if firm_type == "hyper_growth":
            print(f"  Survival prob  : {params.probability_of_survival:.0%}")
            print(f"  TAM cap        : ${params.max_tam_revenue:.1f}B")
        if firm_type == "cyclical":
            if actuals.hist_ebit_margins:
                mc = statistics.mean(actuals.hist_ebit_margins)
                print(f"  Mid-cycle marg : {mc*100:.1f}%  (10-yr hist avg)")
            print(f"  Max CapEx/Sales: {params.max_capex_to_sales*100:.0f}%")
        print()

        sched = build_full_schedule(actuals, consensus, params, cyclical_flag=cyc_flag)
        cols  = ["revenue", "rev_growth", "ebit_margin", "nopat", "reinvestment", "fcf", "source"]
        print(sched[cols].to_string())
        print()

        targets = calculate_rolling_targets(
            actuals, consensus, params, cyclical_flag=cyc_flag
        )
        print("  Rolling Target Prices:")
        for yr, row in targets.iterrows():
            print(
                f"    {yr}E -> ${row['target_price']:>8.2f}"
                f"  | EV ${row['ev_B']:.3f}B"
                f"  | PV(FCF) ${row['pv_fcfs_B']:.3f}B"
                f"  | PV(TV) ${row['pv_tv_B']:.3f}B"
                f"  | Cash ${row['base_cash_B']:.3f}B"
                f"  | Survival {row['survival_prob']:.0%}"
            )
        print()
