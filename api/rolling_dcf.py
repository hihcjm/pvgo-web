"""
7-Stage Lifecycle Rolling DCF Valuation Model
==============================================
Damodaran Framework — Production-Ready, Modular

Stages (strict sequential filter):
  1. Cyclical     — 10Y EBIT-margin StdDev > 8%
  2. Declining    — 3Y avg revenue growth < 0%
  3. Pre-Revenue  — Revenue negligible (<= threshold)
  4. Start-up     — EBIT < 0  (post-revenue, not cyclical/declining)
  5. High-Growth  — Rev Growth > 15%
  6. Mature-Growth— 8% < Rev Growth <= 15%
  7. Mature-Stable— 0% <= Rev Growth <= 8%

Key design rules per stage:
  WACC Decay  : compounded discrete discount factors (no flat WACC assumption)
  Reinvestment: DeltaRev / Sales-to-Capital  (Cyclical: capped at Rev * 0.15)
  Terminal Value: FCF_final / (Terminal_WACC - RFR)
  Survival Prob : applied to EV only (not cash)
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd


# ---------------------------------------------------------------------------
# 1. CONSTANTS & TYPE ALIASES
# ---------------------------------------------------------------------------

Stage = Literal[
    "Cyclical",
    "Declining",
    "Pre-Revenue",
    "Start-up",
    "High-Growth",
    "Mature-Growth",
    "Mature-Stable",
]

ROLLING_BASES: tuple[int, ...] = (2026, 2027, 2028)
PRE_REVENUE_THRESHOLD: float   = 0.05   # $B (or T-won equivalent)


# ---------------------------------------------------------------------------
# 2. DATA STRUCTURES
# ---------------------------------------------------------------------------

@dataclass
class Financials:
    """Latest-year actuals snapshot."""
    revenue:              float          # $B or T-won
    ebit_margin:          float          # fraction (negative OK)
    revenue_growth:       float          # TTM YoY fraction
    cash:                 float          # $B or T-won
    debt:                 float          # $B or T-won
    shares:               float          # B-shares or T-shares
    hist_ebit_margins:    list[float]    = field(default_factory=list)  # last <=10Y
    hist_rev_growth:      list[float]    = field(default_factory=list)  # last <=5Y


@dataclass
class ConsensusYear:
    year:        int
    revenue:     float   # $B or T-won
    ebit_margin: float   # fraction


@dataclass
class StageConfig:
    """
    Per-stage projection parameters.
    WACC: if wacc_start == wacc_end -> flat. Otherwise decays linearly over horizon.
    """
    stage:                   Stage
    horizon:                 int            # projection years beyond last consensus
    wacc_start:              float          # WACC at Year 1 of extrapolation
    wacc_end:                float          # WACC at final year (= terminal WACC)
    target_margin:           float          # EBIT margin convergence target
    survival_prob:           float          # multiplied against EV
    sales_to_capital:        float          # reinvestment: DeltaRev / S2C
    max_reinv_pct_rev:       float          # reinvestment ceiling (pct of revenue)
    margin_converge_year:    int            # year by which margin fully converges
    growth_decays_to_rf:     bool           # True -> growth linearly -> rf
    margin_fixed:            bool           # True -> hold consensus margin (no convergence)
    growth_fixed:            bool           # True -> force growth = rf from Year 1


# ---------------------------------------------------------------------------
# 3. MODULE 1 — LIFECYCLE CLASSIFIER
# ---------------------------------------------------------------------------

class LifecycleClassifier:
    """
    Assigns one of 7 Damodaran lifecycle stages via strict sequential filters.

    Filter order (first match wins):
      F1: Cyclical      — 10Y EBIT-margin StdDev > 8%
      F2: Declining     — 3Y avg rev growth < 0%
      F3: Pre-Revenue   — revenue <= PRE_REVENUE_THRESHOLD
      F4: Start-up      — EBIT margin < 0
      F5: High-Growth   — rev growth > 15%
      F6: Mature-Growth — 8% < rev growth <= 15%
      F7: Mature-Stable — default
    """

    CYCLICAL_STD_THRESHOLD: float = 0.08   # 8%
    DECLINING_GROWTH_WINDOW: int  = 3      # years

    def classify(self, f: Financials) -> Stage:
        # F3 — Pre-Revenue  (must precede cyclical/startup checks)
        if f.revenue <= PRE_REVENUE_THRESHOLD:
            return "Pre-Revenue"

        # F4 — Start-up (negative margin, has revenue)
        if f.ebit_margin < 0:
            return "Start-up"

        # F1 — Cyclical: only evaluated for profitable, revenue-positive firms.
        # Uses population stdev (pstdev) over ALL available hist margins.
        # Requirement: at least 3 data points and CURRENT margin also positive.
        if f.ebit_margin > 0 and len(f.hist_ebit_margins) >= 3:
            # pstdev to match Damodaran's full-population normalisation
            pstd = statistics.pstdev(f.hist_ebit_margins)
            if pstd > self.CYCLICAL_STD_THRESHOLD:
                return "Cyclical"

        # F2 — Declining: profitable firm with contracting revenue
        recent_growth = f.hist_rev_growth[-self.DECLINING_GROWTH_WINDOW:]
        if len(recent_growth) >= 2:
            avg_g = sum(recent_growth) / len(recent_growth)
            if avg_g < 0.0:
                return "Declining"
        elif f.revenue_growth < -0.02:
            return "Declining"

        # F5/F6/F7 — Growth tiers
        g = f.revenue_growth
        if g > 0.15:
            return "High-Growth"
        if g > 0.08:
            return "Mature-Growth"
        return "Mature-Stable"

    def get_config(
        self,
        stage:              Stage,
        rf:                 float,
        industry_margin:    float,
        consensus_margin:   float,
        hist_avg_margin:    float,
        sales_to_capital:   float,
    ) -> StageConfig:
        """Build StageConfig for the given stage."""

        # Shared defaults
        s2c = sales_to_capital

        if stage == "Pre-Revenue":
            return StageConfig(
                stage=stage, horizon=10,
                wacc_start=0.25,  wacc_end=0.08,
                target_margin=0.30,
                survival_prob=0.20,
                sales_to_capital=s2c, max_reinv_pct_rev=0.50,
                margin_converge_year=10,
                growth_decays_to_rf=True, margin_fixed=False, growth_fixed=False,
            )

        if stage == "Start-up":
            return StageConfig(
                stage=stage, horizon=10,
                wacc_start=0.18,  wacc_end=0.08,
                target_margin=max(0.20, industry_margin),
                survival_prob=0.60,
                sales_to_capital=s2c, max_reinv_pct_rev=0.40,
                margin_converge_year=10,
                growth_decays_to_rf=True, margin_fixed=False, growth_fixed=False,
            )

        if stage == "High-Growth":
            return StageConfig(
                stage=stage, horizon=10,
                wacc_start=0.12,  wacc_end=0.08,
                target_margin=industry_margin,
                survival_prob=1.0,
                sales_to_capital=s2c, max_reinv_pct_rev=0.35,
                margin_converge_year=10,
                growth_decays_to_rf=True, margin_fixed=False, growth_fixed=False,
            )

        if stage == "Mature-Growth":
            return StageConfig(
                stage=stage, horizon=10,
                wacc_start=0.08,  wacc_end=0.08,
                target_margin=consensus_margin,
                survival_prob=1.0,
                sales_to_capital=s2c, max_reinv_pct_rev=0.25,
                margin_converge_year=10,
                growth_decays_to_rf=True, margin_fixed=True, growth_fixed=False,
            )

        if stage == "Mature-Stable":
            return StageConfig(
                stage=stage, horizon=5,
                wacc_start=0.08,  wacc_end=0.08,
                target_margin=consensus_margin,
                survival_prob=1.0,
                sales_to_capital=s2c, max_reinv_pct_rev=0.20,
                margin_converge_year=5,
                growth_decays_to_rf=False, margin_fixed=True, growth_fixed=True,
            )

        if stage == "Declining":
            return StageConfig(
                stage=stage, horizon=5,
                wacc_start=0.09,  wacc_end=0.09,
                target_margin=max(industry_margin * 0.5, 0.02),
                survival_prob=1.0,
                sales_to_capital=max(s2c * 0.7, 0.3), max_reinv_pct_rev=0.10,
                margin_converge_year=5,
                growth_decays_to_rf=False, margin_fixed=False, growth_fixed=False,
            )

        # Cyclical
        return StageConfig(
            stage=stage, horizon=10,
            wacc_start=0.09,  wacc_end=0.09,
            target_margin=hist_avg_margin,
            survival_prob=1.0,
            sales_to_capital=s2c, max_reinv_pct_rev=0.15,
            margin_converge_year=1,   # INSTANTLY normalize
            growth_decays_to_rf=True, margin_fixed=False, growth_fixed=False,
        )


# ---------------------------------------------------------------------------
# 4. MODULE 2 — FINANCIAL PROJECTOR
# ---------------------------------------------------------------------------

def _linspace(start: float, end: float, step: int, total: int) -> float:
    """Linear interpolation: step=1->start, step=total->end."""
    if total <= 1:
        return end
    alpha = (step - 1) / (total - 1)
    return start * (1 - alpha) + end * alpha


def _wacc_at_step(cfg: StageConfig, step: int) -> float:
    """
    WACC for this extrapolation step.
    step=1 -> wacc_start, step=horizon -> wacc_end.
    """
    return _linspace(cfg.wacc_start, cfg.wacc_end, step, cfg.horizon)


class FinancialProjector:
    """
    Converts consensus years -> FCF, then extrapolates beyond last consensus
    applying stage-specific safety guards.
    """

    def consensus_to_fcf(
        self,
        consensus: list[ConsensusYear],
        actuals:   Financials,
        cfg:       StageConfig,
        rf:        float,
        tax_rate:  float,
    ) -> pd.DataFrame:
        """
        Convert 2026-2028 consensus to FCF rows.
        Reinvestment = DeltaRev / S2C (capped at Rev * max_reinv_pct_rev).
        """
        rows: list[dict] = []
        prev_rev = actuals.revenue

        for c in sorted(consensus, key=lambda x: x.year):
            ebit  = c.revenue * c.ebit_margin
            nopat = ebit * (1 - tax_rate)
            reinv = min(
                (c.revenue - prev_rev) / cfg.sales_to_capital,
                c.revenue * cfg.max_reinv_pct_rev,
            )
            fcf = nopat - reinv
            g   = (c.revenue / prev_rev - 1) if prev_rev and prev_rev > 0 else 0.0

            rows.append({
                "year":         c.year,
                "revenue":      round(c.revenue, 4),
                "rev_growth":   round(g * 100, 2),
                "ebit_margin":  round(c.ebit_margin * 100, 2),
                "ebit":         round(ebit, 4),
                "nopat":        round(nopat, 4),
                "reinvestment": round(reinv, 4),
                "fcf":          round(fcf, 4),
                "wacc":         round(cfg.wacc_start * 100, 2),
                "source":       "consensus",
            })
            prev_rev = c.revenue

        return pd.DataFrame(rows).set_index("year")

    def extrapolate(
        self,
        consensus: list[ConsensusYear],
        actuals:   Financials,
        cfg:       StageConfig,
        rf:        float,
        tax_rate:  float,
    ) -> pd.DataFrame:
        """
        Extrapolate [last_consensus+1 .. last_consensus+horizon+1] FCF rows.
        +1 extra row = FCF_final+1 used as Terminal Value numerator.
        Applies all stage-specific guards.
        """
        sorted_con  = sorted(consensus, key=lambda c: c.year)
        last        = sorted_con[-1]
        second_last = sorted_con[-2] if len(sorted_con) >= 2 else None

        # Seed growth from consensus
        if second_last and second_last.revenue > 0:
            seed_g = last.revenue / second_last.revenue - 1
        else:
            seed_g = actuals.revenue_growth

        seed_m = last.ebit_margin

        # Declining: ensure growth starts negative or near zero
        if cfg.stage == "Declining":
            seed_g = min(seed_g, -0.01)

        rows:     list[dict] = []
        prev_rev: float      = last.revenue
        n_extra = cfg.horizon + 1  # +1 for TV numerator row

        for step in range(1, n_extra + 1):
            yr = last.year + step

            # ── Growth rate ────────────────────────────────────────
            if cfg.growth_fixed:
                g = rf                               # Mature-Stable: flat at rf
            elif cfg.stage == "Declining":
                # Decay from seed (negative) toward 0 over horizon
                g = _linspace(seed_g, 0.0, step, cfg.horizon)
                g = min(g, 0.0)                      # never positive for Declining
            elif cfg.growth_decays_to_rf:
                g = _linspace(seed_g, rf, step, cfg.horizon)
                g = max(g, rf)                       # floor at rf
            else:
                g = seed_g

            # ── EBIT Margin ────────────────────────────────────────
            if cfg.margin_fixed:
                m = seed_m
            elif cfg.stage == "Cyclical":
                m = cfg.target_margin               # INSTANT normalization
            else:
                converge_step  = min(step, cfg.margin_converge_year)
                m = _linspace(seed_m, cfg.target_margin, converge_step, cfg.margin_converge_year)

            # ── Revenue ───────────────────────────────────────────
            rev = prev_rev * (1 + g)

            ebit  = rev * m
            nopat = ebit * (1 - tax_rate)

            # ── Reinvestment (stage-specific cap) ─────────────────
            reinv = min(
                (rev - prev_rev) / cfg.sales_to_capital,
                rev * cfg.max_reinv_pct_rev,
            )
            # Declining: reinvestment must be non-negative (no capex when shrinking)
            if cfg.stage == "Declining":
                reinv = max(reinv, 0.0)

            fcf = nopat - reinv

            # ── WACC for this step ────────────────────────────────
            wacc_t = _wacc_at_step(cfg, min(step, cfg.horizon))

            rows.append({
                "year":         yr,
                "revenue":      round(rev, 4),
                "rev_growth":   round(g * 100, 2),
                "ebit_margin":  round(m * 100, 2),
                "ebit":         round(ebit, 4),
                "nopat":        round(nopat, 4),
                "reinvestment": round(reinv, 4),
                "fcf":          round(fcf, 4),
                "wacc":         round(wacc_t * 100, 2),
                "source":       "extrapolated",
            })
            prev_rev = rev

        return pd.DataFrame(rows).set_index("year")


# ---------------------------------------------------------------------------
# 5. MODULE 3 — ROLLING DCF ENGINE
# ---------------------------------------------------------------------------

class RollingDCFEngine:
    """
    Computes Year-End 2026 / 2027 / 2028 Target Prices.

    For each base_year T:
      cumulative_pv_factor(T, yr) = product of (1+wacc_t) for t in [T+1..yr]
      pv_fcf = sum( FCF_yr / cumulative_factor(T, yr) )
      tv_year = last projection year + 1
      tv_wacc = WACC at final extrapolation step (= wacc_end)
      TV = FCF_tv_year / (tv_wacc - rf)
      pv_tv = TV / cumulative_factor(T, tv_year)
      EV = (pv_fcf + pv_tv) * survival_prob
      equity = EV + base_cash - debt
      target_price = equity / shares
    """

    def _cumulative_discount(
        self,
        wacc_schedule: dict[int, float],
        base_year:     int,
        target_year:   int,
    ) -> float:
        """
        Product of (1+wacc_t) for each year from base_year+1 to target_year.
        wacc_schedule: {year: wacc_fraction}
        """
        factor = 1.0
        for yr in range(base_year + 1, target_year + 1):
            w = wacc_schedule.get(yr, list(wacc_schedule.values())[-1])
            factor *= (1 + w)
        return factor

    def calculate(
        self,
        actuals:        Financials,
        consensus:      list[ConsensusYear],
        cfg:            StageConfig,
        rf:             float,
        tax_rate:       float,
        rolling_bases:  tuple[int, ...] = ROLLING_BASES,
    ) -> pd.DataFrame:
        proj = FinancialProjector()

        # Build consensus FCF table
        con_df = proj.consensus_to_fcf(consensus, actuals, cfg, rf, tax_rate)

        # Build extrapolated FCF table
        ext_df = proj.extrapolate(consensus, actuals, cfg, rf, tax_rate)

        # Merge
        all_df: pd.DataFrame = pd.concat([con_df, ext_df])

        # Build WACC schedule: {year: wacc_fraction}
        # Consensus years use wacc_start; extrapolated use per-step WACC
        wacc_sched: dict[int, float] = {}
        for yr in all_df.index:
            wacc_sched[int(yr)] = all_df.loc[yr, "wacc"] / 100.0

        # Rolling cash accumulation: Cash_T = Cash_{T-1} + FCF_T
        rolling_cash: dict[int, float] = {}
        cash_prev = actuals.cash
        for c in sorted(consensus, key=lambda x: x.year):
            fcf_t = float(all_df.loc[c.year, "fcf"])
            rolling_cash[c.year] = cash_prev + fcf_t
            cash_prev = rolling_cash[c.year]

        results: list[dict] = []

        for base_year in rolling_bases:
            proj_start  = base_year + 1
            proj_end    = sorted(consensus, key=lambda x: x.year)[-1].year + cfg.horizon
            tv_year     = proj_end + 1          # FCF_final+1 for TV numerator

            # FCF window: proj_start .. proj_end
            window_idx = [yr for yr in all_df.index if proj_start <= int(yr) <= proj_end]

            # PV of FCFs using compounded discrete WACCs
            pv_fcfs = 0.0
            for yr in window_idx:
                fcf_yr  = float(all_df.loc[yr, "fcf"])
                cum_fac = self._cumulative_discount(wacc_sched, base_year, int(yr))
                pv_fcfs += fcf_yr / cum_fac

            # Terminal Value
            if tv_year in all_df.index:
                fcf_tv = float(all_df.loc[tv_year, "fcf"])
            else:
                fcf_tv = float(all_df.iloc[-1]["fcf"])

            tv_wacc = cfg.wacc_end
            spread  = tv_wacc - rf
            if spread <= 0:
                spread = 0.01   # safety floor: 100bps
            tv_undiscounted = fcf_tv / spread
            cum_tv = self._cumulative_discount(wacc_sched, base_year, tv_year)
            pv_tv  = tv_undiscounted / cum_tv

            ev           = (pv_fcfs + pv_tv) * cfg.survival_prob
            base_cash    = rolling_cash.get(base_year, actuals.cash)
            equity_value = ev + base_cash - actuals.debt
            target_price = equity_value / actuals.shares if actuals.shares > 0 else float("nan")

            results.append({
                "valuation_date":  base_year,
                "stage":           cfg.stage,
                "horizon":         cfg.horizon,
                "wacc_start_pct":  round(cfg.wacc_start * 100, 2),
                "wacc_end_pct":    round(cfg.wacc_end   * 100, 2),
                "terminal_g_pct":  round(rf * 100, 2),
                "survival_prob":   round(cfg.survival_prob, 2),
                "proj_window":     f"{proj_start}-{proj_end}",
                "tv_year":         tv_year,
                "fcf_tv":          round(fcf_tv, 4),
                "tv_spread_pct":   round(spread * 100, 2),
                "pv_fcfs":         round(pv_fcfs, 4),
                "pv_tv":           round(pv_tv, 4),
                "ev":              round(ev, 4),
                "base_cash":       round(base_cash, 4),
                "debt":            round(actuals.debt, 4),
                "equity":          round(equity_value, 4),
                "shares":          round(actuals.shares, 4),
                "target_price":    round(target_price, 2),
            })

        return pd.DataFrame(results).set_index("valuation_date")


# ---------------------------------------------------------------------------
# 6. PUBLIC CONVENIENCE API  (used by app.py bridges)
# ---------------------------------------------------------------------------

def classify_and_configure(
    actuals:           Financials,
    rf:                float,
    industry_margin:   float,
    sales_to_capital:  float,
    consensus:         list[ConsensusYear],
) -> tuple[Stage, StageConfig]:
    """One-call helper: classify -> build StageConfig."""
    clf  = LifecycleClassifier()
    stage = clf.classify(actuals)

    last_con_margin = sorted(consensus, key=lambda c: c.year)[-1].ebit_margin if consensus else actuals.ebit_margin
    hist_avg_margin = (statistics.mean(actuals.hist_ebit_margins)
                       if len(actuals.hist_ebit_margins) >= 2 else actuals.ebit_margin)

    cfg = clf.get_config(
        stage            = stage,
        rf               = rf,
        industry_margin  = industry_margin,
        consensus_margin = last_con_margin,
        hist_avg_margin  = hist_avg_margin,
        sales_to_capital = sales_to_capital,
    )
    return stage, cfg


def build_full_schedule(
    actuals:   Financials,
    consensus: list[ConsensusYear],
    cfg:       StageConfig,
    rf:        float,
    tax_rate:  float,
) -> pd.DataFrame:
    proj   = FinancialProjector()
    con_df = proj.consensus_to_fcf(consensus, actuals, cfg, rf, tax_rate)
    ext_df = proj.extrapolate(consensus, actuals, cfg, rf, tax_rate)
    return pd.concat([con_df, ext_df])


def calculate_rolling_targets(
    actuals:   Financials,
    consensus: list[ConsensusYear],
    cfg:       StageConfig,
    rf:        float,
    tax_rate:  float,
) -> pd.DataFrame:
    engine = RollingDCFEngine()
    return engine.calculate(actuals, consensus, cfg, rf, tax_rate)


# ---------------------------------------------------------------------------
# 7. MOCK EXECUTION — Cyclical (Steel) + Start-up (IonQ-style)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pd.set_option("display.float_format", "{:.4f}".format)
    pd.set_option("display.max_columns", 15)
    pd.set_option("display.width", 160)

    # ── FIRM A: Cyclical (Steel-proxy, pstdev ~10% -> triggers F1) ────────
    cyclical_actuals = Financials(
        revenue        = 22.0,
        ebit_margin    = 0.20,          # current peak — will be instantly normalized
        revenue_growth = 0.08,
        cash           = 1.5,
        debt           = 6.0,
        shares         = 1.2,
        hist_ebit_margins = [0.02, 0.18, 0.28, 0.20, 0.02, 0.22, 0.30, 0.04, 0.20, 0.08],
        hist_rev_growth   = [0.12, -0.08, 0.18, 0.09, 0.05],
    )
    cyclical_consensus = [
        ConsensusYear(year=2026, revenue=23.5, ebit_margin=0.16),
        ConsensusYear(year=2027, revenue=24.8, ebit_margin=0.13),
        ConsensusYear(year=2028, revenue=25.5, ebit_margin=0.11),
    ]
    RF_STEEL = 0.044

    stage_c, cfg_c = classify_and_configure(
        actuals=cyclical_actuals, rf=RF_STEEL,
        industry_margin=0.12, sales_to_capital=1.2,
        consensus=cyclical_consensus,
    )

    # ── FIRM B: Start-up (IonQ-style) ─────────────────────────────────────
    startup_actuals = Financials(
        revenue        = 0.043,
        ebit_margin    = -4.0,
        revenue_growth = 2.0,
        cash           = 0.38,
        debt           = 0.05,
        shares         = 0.67,
        hist_ebit_margins = [-3.5, -2.0, -4.0],
        hist_rev_growth   = [0.80, 1.50, 2.00],
    )
    startup_consensus = [
        ConsensusYear(year=2026, revenue=0.105, ebit_margin=-2.5),
        ConsensusYear(year=2027, revenue=0.230, ebit_margin=-1.2),
        ConsensusYear(year=2028, revenue=0.420, ebit_margin=-0.4),
    ]
    RF_STARTUP = 0.044

    stage_s, cfg_s = classify_and_configure(
        actuals=startup_actuals, rf=RF_STARTUP,
        industry_margin=0.15, sales_to_capital=0.8,
        consensus=startup_consensus,
    )

    for label, actuals, consensus, cfg, rf in [
        ("CYCLICAL  [Steel-proxy: StdDev 6.1% -> Cyclical]",      cyclical_actuals, cyclical_consensus, cfg_c, RF_STEEL),
        ("START-UP  [IonQ-style: -400% margin, 200% growth]",     startup_actuals,  startup_consensus,  cfg_s, RF_STARTUP),
    ]:
        print("=" * 80)
        print(f"  {label}")
        print("=" * 80)
        print(f"  Stage          : {cfg.stage}")
        print(f"  Horizon        : {cfg.horizon} yrs")
        print(f"  WACC           : {cfg.wacc_start*100:.1f}% -> {cfg.wacc_end*100:.1f}%  (decay per year)")
        print(f"  Terminal g     : {rf*100:.2f}%  (= rf)")
        print(f"  Survival Prob  : {cfg.survival_prob:.0%}")
        print(f"  Target Margin  : {cfg.target_margin*100:.1f}%")
        print(f"  S/Capital      : {cfg.sales_to_capital}")
        print(f"  Max Reinv/Rev  : {cfg.max_reinv_pct_rev*100:.0f}%")
        print()

        sched = build_full_schedule(actuals, consensus, cfg, rf, tax_rate=0.21)
        cols  = ["revenue", "rev_growth", "ebit_margin", "nopat", "reinvestment", "fcf", "wacc", "source"]
        print(sched[cols].to_string())
        print()

        targets = calculate_rolling_targets(actuals, consensus, cfg, rf, tax_rate=0.21)
        print("  Rolling Target Prices:")
        for yr, row in targets.iterrows():
            print(
                f"    {yr}E -> ${row['target_price']:>9.2f}"
                f"  | EV ${row['ev']:.4f}"
                f"  | PV(FCF) ${row['pv_fcfs']:.4f}"
                f"  | PV(TV) ${row['pv_tv']:.4f}"
                f"  | Cash ${row['base_cash']:.4f}"
                f"  | Surv {row['survival_prob']:.0%}"
                f"  | WACC {row['wacc_start_pct']:.1f}%->{row['wacc_end_pct']:.1f}%"
            )
        print()
