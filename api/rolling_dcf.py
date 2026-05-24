"""
rolling_dcf.py
==============
Aswath Damodaran 4-Stage Life Cycle DCF Valuation Engine
for Korean Stocks (K-IFRS Consolidated Financials)

핵심 설계 원칙 (v2)
-------------------
  - 모든 스테이지에서 **동일한 FCFF 공식** 사용:
      FCFF_t = NOPAT_t × (1 − RR_t)
      RR_t   = g_t / ROIC_t          ← Damodaran 핵심 항등식
  - 스테이지 분류는 **할인율(effective WACC)** 만 조정:
      Stage 1 (Startup)    : WACC + stage_premium(default +3%)
      Stage 2 (High Growth): WACC (기본)
      Stage 3 (Mature)     : WACC − 0.5%  (리스크 감소 반영)
      Stage 4 (Decline)    : WACC (기본, 청산 리스크 포함)
  - 성장 경로는 공통 _growth_path() 로 일원화 → 단계 경계 불연속 없음
  - 스테이지별 "특수 로직"(파산확률, 청산가치)은 최소한으로만 유지

단위 규약 (Unit Convention)
--------------------------
  - 모든 금액: 조원(T-KRW, 1조 = 1e12 원)
  - shares   : 조주(T-shares) → IV = equity_value(T) / shares(T) = 원
  - 비율      : fraction (0.0 ~ 1.0)
"""

from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 입력 데이터 구조체
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Financials:
    """
    크롤링 파이프라인에서 전달받는 최신 결산 기준 재무 스냅샷.
    모든 금액 단위: 조원(T-KRW).
    """
    # ── 손익계산서 ──────────────────────────────────────────────────────────────
    revenue:            float   # 매출액 (T-KRW)
    ebit:               float   # 영업이익 / EBIT (T-KRW)
    ebit_margin:        float   # EBIT 마진 = ebit / revenue (fraction)
    tax_rate:           float   # 실효세율 (fraction, e.g. 0.22)

    # ── 현금흐름표 ──────────────────────────────────────────────────────────────
    depr_amort:         float   # 감가상각비 + 무형자산상각비 (T-KRW)
    capex:              float   # 자본적지출, 양수값 (T-KRW)
    change_wc:          float   # 운전자본 증감 (T-KRW, 증가=양수=현금유출)

    # ── 재무상태표 ──────────────────────────────────────────────────────────────
    cash_st:            float   # 현금+단기금융상품 (T-KRW)
    debt:               float   # 총차입금 (T-KRW)
    minority_interest:  float   # 비지배지분 (T-KRW, K-IFRS 연결 특성)

    # ── 시장 데이터 ─────────────────────────────────────────────────────────────
    shares:             float   # 발행주식수 (T-shares, 조주 단위)
                                # 예: 5,969,782천주 → 5.969782e9 / 1e12 = 0.005969782


# ═══════════════════════════════════════════════════════════════════════════════
# 2. DCF 엔진 메인 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class DamodaranDCF:
    """
    Damodaran 4-Stage Life Cycle DCF — 단일 FCFF 공식, 단계별 할인율 조정

    모든 스테이지 공통 FCFF 공식
    ----------------------------
      NOPAT_t  = EBIT_t × (1 − tax)
      RR_t     = g_t / ROIC_t                (Damodaran 재투자율 항등식)
      FCFF_t   = NOPAT_t × (1 − RR_t)
      PV_t     = FCFF_t / (1 + eff_wacc)^t   ← eff_wacc는 스테이지별 차등

    스테이지별 effective WACC
    -------------------------
      Stage 1: WACC + stage_premium  (default +0.03 = +3%p)
               스타트업의 높은 불확실성·집중 리스크 반영
      Stage 2: WACC                  (기본 WACC)
      Stage 3: WACC − 0.005          (−0.5%p, 성숙기 리스크 감소)
      Stage 4: WACC                  (기본 WACC, 청산 불확실성 유지)

    성장 경로 (_growth_path)
    ----------------------
      기간 1 (t=1~phase1_years)  : g_base, ROIC_base 유지
      기간 2 (t=phase1+1~total)  : g_base → g_terminal 선형 수렴
                                   ROIC_base → WACC 선형 수렴
      Terminal (t>total)         : Gordon Growth, g=g_terminal, ROIC=WACC
    """

    # ── 가드레일 ──────────────────────────────────────────────────────────────
    _GUARD_REINV_MIN  = -0.80   # 재투자율 하한 (쇠퇴기 자산 회수 허용)
    _GUARD_REINV_MAX  =  0.95   # 재투자율 상한
    _GUARD_ROIC_FLOOR =  0.001  # ROIC 하한 (0 나눗셈 방지)
    _GUARD_ROIC_CAP   =  0.80   # ROIC 상한 80%

    def __init__(
        self,
        financials:     Financials,
        rf:             float = 0.035,
        erp:            float = 0.075,
        beta:           float = 1.0,
        debt_spread:    float = 0.015,
        equity_weight:  Optional[float] = None,
    ):
        self.fin         = financials
        self.rf          = rf
        self.erp         = erp
        self.beta        = beta
        self.debt_spread = debt_spread
        self._eq_weight  = equity_weight

        # WACC는 생성 시 1회 계산 — 각 스테이지는 여기에 premium/discount 적용
        self.wacc, self.coe, self.cod = self._calculate_wacc()

    # ─────────────────────────────────────────────────────────────────────────
    # A. WACC 계산 (기본값)
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_wacc(self) -> tuple[float, float, float]:
        """
        CoE = rf + β × ERP   (CAPM)
        CoD = (rf + spread) × (1 − t)   (after-tax)
        WACC = e_w × CoE + d_w × CoD
        """
        coe = self.rf + self.beta * self.erp

        spread    = max(self.debt_spread, 0.05) if self.fin.ebit <= 0 else self.debt_spread
        cod_pre   = self.rf + spread
        cod       = cod_pre * (1 - self.fin.tax_rate)

        if self._eq_weight is not None:
            e_w = max(0.0, min(1.0, self._eq_weight))
            d_w = 1.0 - e_w
        else:
            if self.fin.debt > 0:
                nopat_proxy = max(self.fin.ebit * (1 - self.fin.tax_rate), 1e-6)
                implied_ev  = nopat_proxy / max(coe * 0.9, 0.05)
                d_w = min(self.fin.debt / (implied_ev + self.fin.debt), 0.60)
                e_w = 1.0 - d_w
            else:
                e_w, d_w = 1.0, 0.0

        wacc = e_w * coe + d_w * cod
        return wacc, coe, cod

    # ─────────────────────────────────────────────────────────────────────────
    # B. 공통 유틸리티
    # ─────────────────────────────────────────────────────────────────────────

    def _nopat(self) -> float:
        return self.fin.ebit * (1 - self.fin.tax_rate)

    def _base_roic(self) -> float:
        """
        ROIC = NOPAT / IC
        IC = max(D&A×7, Revenue×0.3)  — 보수적 추정(더 큰 IC → 더 낮은 ROIC)
        """
        nopat = self._nopat()
        da    = max(self.fin.depr_amort, 1e-9)
        ic    = max(da * 7.0, self.fin.revenue * 0.3, 1e-9)
        roic  = nopat / ic
        return min(max(roic, self._GUARD_ROIC_FLOOR), self._GUARD_ROIC_CAP)

    def _base_reinvestment_rate(self) -> float:
        """현재 재무제표 역산 RR = (CapEx − D&A + ΔWC) / NOPAT"""
        nopat = self._nopat()
        if nopat <= 0:
            return 0.50
        reinv = self.fin.capex - self.fin.depr_amort + self.fin.change_wc
        rr    = reinv / nopat
        return max(self._GUARD_REINV_MIN, min(rr, self._GUARD_REINV_MAX))

    @staticmethod
    def _pv_factor(rate: float, t: int) -> float:
        return 1.0 / ((1.0 + rate) ** t)

    def _equity_bridge(
        self,
        ev:    float,
        fcffs: Optional[list[dict]] = None,
        extra: Optional[dict]       = None,
    ) -> dict:
        """
        Equity Bridge (K-IFRS 연결 기준)
        Equity = EV + Cash − Debt − Minority Interest
        IV     = Equity (T-KRW) / Shares (T-shares) = 원/주
        """
        equity_value    = ev + self.fin.cash_st - self.fin.debt - self.fin.minority_interest
        price_per_share = (equity_value / self.fin.shares) if self.fin.shares > 0 else 0.0

        result = {
            "intrinsic_value": price_per_share,
            "ev":              ev,
            "equity_value":    equity_value,
            "cash_st":         self.fin.cash_st,
            "debt":            self.fin.debt,
            "minority":        self.fin.minority_interest,
            "wacc":            self.wacc,
            "coe":             self.coe,
            "cod":             self.cod,
            "rf":              self.rf,
            "erp":             self.erp,
            "beta":            self.beta,
            "fcff_schedule":   fcffs or [],
        }
        if extra:
            result.update(extra)
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # C. 공통 성장 경로 엔진  ← 모든 스테이지가 이 함수를 공유
    # ─────────────────────────────────────────────────────────────────────────

    def _growth_path(
        self,
        g_base:       float,
        roic_base:    float,
        g_terminal:   float,
        eff_wacc:     float,
        phase1_years: int   = 5,
        phase2_years: int   = 5,
        nopat_start:  Optional[float] = None,
        note:         str   = "",
    ) -> tuple[list[dict], float, float]:
        """
        모든 스테이지 공유 FCFF 경로 생성기.

        공통 FCFF 공식
        -------------
          NOPAT_t  = NOPAT_{t-1} × (1 + g_t)
          RR_t     = clip( g_t / ROIC_t, RR_MIN, RR_MAX )
          FCFF_t   = NOPAT_t × (1 − RR_t)
          PV_t     = FCFF_t / (1 + eff_wacc)^t

        성장 경로
        ---------
          기간 1 (t=1..phase1_years)  : g=g_base, ROIC=roic_base (유지)
          기간 2 (t=+1..+phase2_years): g → g_terminal 선형 수렴
                                        ROIC → eff_wacc (성숙 기준) 선형 수렴
          Terminal (t=total+1..∞)     : Gordon Growth

        Parameters
        ----------
        g_base       : 1기 성장률
        roic_base    : 1기 ROIC
        g_terminal   : 영구 성장률 (≤ rf 강제)
        eff_wacc     : 이 스테이지의 effective 할인율
        phase1_years : 고성장 유지 기간
        phase2_years : 수렴 기간
        nopat_start  : 시작 NOPAT (None이면 현재 재무제표 기반)
        note         : fcff_schedule에 기록될 스테이지 레이블

        Returns
        -------
        (fcffs, pv_sum, pv_tv)
        """
        g_terminal = min(g_terminal, self.rf)   # 절대 상한: rf
        g_terminal = max(g_terminal, 0.005)     # 최소 0.5% (디플레이션 방지)

        total_years  = phase1_years + phase2_years
        current_nopat = (nopat_start if nopat_start is not None
                         else self._nopat())

        # NOPAT ≤ 0 이면 수익성 회복까지 완충 처리
        if current_nopat <= 0:
            current_nopat = max(self.fin.revenue * 0.01, 1e-9)  # 매출 1% 최소 수익 가정

        fcffs     = []
        pv_sum    = 0.0

        for t in range(1, total_years + 1):
            # ── 성장률·ROIC 경로 ─────────────────────────────────────────────
            if t <= phase1_years:
                g_t    = g_base
                roic_t = roic_base
                phase  = f"phase1_{note}" if note else "phase1"
            else:
                alpha  = (t - phase1_years) / phase2_years   # 0 → 1
                g_t    = g_base    * (1 - alpha) + g_terminal  * alpha
                roic_t = roic_base * (1 - alpha) + eff_wacc    * alpha
                roic_t = max(roic_t, self._GUARD_ROIC_FLOOR)
                phase  = f"phase2_{note}" if note else "phase2"

            # ── 공통 FCFF 계산 ───────────────────────────────────────────────
            rr_t   = max(self._GUARD_REINV_MIN,
                         min(g_t / roic_t, self._GUARD_REINV_MAX))
            current_nopat *= (1 + g_t)
            fcf_t  = current_nopat * (1 - rr_t)
            pv_t   = fcf_t * self._pv_factor(eff_wacc, t)
            pv_sum += pv_t

            fcffs.append({
                "year":       t,
                "growth_g":   round(g_t,           4),
                "roic":       round(roic_t,         4),
                "reinv_rate": round(rr_t,           4),
                "nopat":      round(current_nopat,  4),
                "fcf":        round(fcf_t,          4),
                "pv_fcf":     round(pv_t,           4),
                "eff_wacc":   round(eff_wacc,       4),
                "phase":      phase,
            })

        # ── Terminal Value (Gordon Growth) ───────────────────────────────────
        rr_tv     = g_terminal / max(eff_wacc, g_terminal + 0.001)
        nopat_tv  = current_nopat * (1 + g_terminal)
        fcf_tv    = nopat_tv * (1 - rr_tv)
        tv        = fcf_tv / max(eff_wacc - g_terminal, 0.001)
        pv_tv     = tv * self._pv_factor(eff_wacc, total_years)

        return fcffs, pv_sum, pv_tv

    # ─────────────────────────────────────────────────────────────────────────
    # D. 공개 인터페이스
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_intrinsic_value(self, stage: int = 2, **kwargs) -> dict:
        """
        생애주기(stage)에 따라 DCF를 계산하여 주당 내재가치를 반환.

        Parameters
        ----------
        stage : int
            1 = Startup   (신생·초기 성장기)
            2 = High Growth (고성장기)       ← 대부분의 성장주
            3 = Mature      (성숙 안정기)
            4 = Decline     (쇠퇴·청산기)

        Returns
        -------
        dict : {
            'intrinsic_value' : float,   # KRW per share
            'ev'              : float,   # Enterprise Value (T-KRW)
            'equity_value'    : float,   # Equity Value (T-KRW)
            'wacc'            : float,   # 기본 WACC
            'eff_wacc'        : float,   # 스테이지별 effective WACC
            'fcff_schedule'   : list,    # 연도별 FCFF 내역
            ...
        }
        """
        dispatch = {
            1: self._startup_valuation,
            2: self._high_growth_valuation,
            3: self._mature_valuation,
            4: self._decline_valuation,
        }
        return dispatch.get(stage, self._high_growth_valuation)(**kwargs)

    # ─────────────────────────────────────────────────────────────────────────
    # D-1. Stage 1: Startup
    # ─────────────────────────────────────────────────────────────────────────

    def _startup_valuation(
        self,
        tam:                 float = 0.0,
        target_share:        float = 0.10,
        target_margin:       float = 0.15,
        prob_failure:        float = 0.30,
        liquidation_val_pct: float = 0.50,
        ramp_years:          int   = 10,
        stage_premium:       float = 0.03,
        **kwargs,
    ) -> dict:
        """
        Stage 1 — Startup: TAM 기반 Top-Down 매출 추정

        동일 FCFF 공식 사용. 스타트업 특성만 다음으로 반영:
          - eff_wacc = WACC + stage_premium (default +3%p)
          - 파산 확률(prob_failure) 반영한 기대 EV 계산
          - NOPAT 시작값: TAM 도달 경로의 1년차 추정값

        Parameters
        ----------
        tam                  : 목표 시장 규모 (T-KRW). 0이면 현재 매출 × 20
        target_share         : TAM 대비 목표 점유율 (default 10%)
        target_margin        : 목표 EBIT 마진 (default 15%)
        prob_failure         : 파산 확률 (default 30%)
        liquidation_val_pct  : 파산 시 현금 회수율 (default 50%)
        ramp_years           : 추정 기간 (default 10년)
        stage_premium        : WACC 가산 리스크 프리미엄 (default 3%p)
        """
        eff_wacc = self.wacc + stage_premium

        if tam <= 0:
            tam = self.fin.revenue * 20.0

        rev_yr0  = max(self.fin.revenue, 1e-9)
        rev_yr_n = tam * target_share

        # 1년차 도달 NOPAT 추정 (TAM 경로 기반)
        alpha_1  = 1.0 / ramp_years
        rev_1    = rev_yr0 * ((rev_yr_n / rev_yr0) ** alpha_1)
        margin_1 = self.fin.ebit_margin + alpha_1 * (target_margin - self.fin.ebit_margin)
        nopat_1  = max(rev_1 * margin_1 * (1 - self.fin.tax_rate), 1e-9)

        # 암묵적 성장률: TAM 도달을 위한 연 복리 성장률
        g_base = (rev_yr_n / rev_yr0) ** (1.0 / ramp_years) - 1.0
        g_base = min(g_base, 0.60)   # 최대 60% 성장률 상한

        # ROIC: 초기는 낮음 → target_margin 기반 추정
        roic_start = max(
            target_margin * (1 - self.fin.tax_rate) / 0.30,  # 목표 마진 기준 ROIC
            self._GUARD_ROIC_FLOOR,
        )
        roic_start = min(roic_start, self._GUARD_ROIC_CAP)

        g_terminal = min(0.025, self.rf)

        fcffs, pv_sum, pv_tv = self._growth_path(
            g_base       = g_base,
            roic_base    = roic_start,
            g_terminal   = g_terminal,
            eff_wacc     = eff_wacc,
            phase1_years = ramp_years // 2,
            phase2_years = ramp_years - ramp_years // 2,
            nopat_start  = nopat_1,
            note         = "startup",
        )

        going_concern_ev = pv_sum + pv_tv
        liquidation_val  = (self.fin.cash_st + self.fin.depr_amort * 3) * liquidation_val_pct
        ev               = (going_concern_ev * (1 - prob_failure)
                            + liquidation_val   *  prob_failure)

        extra = {
            "stage":             "startup",
            "eff_wacc":          round(eff_wacc,           4),
            "stage_premium":     stage_premium,
            "tam":               tam,
            "target_share":      target_share,
            "target_margin":     target_margin,
            "prob_failure":      prob_failure,
            "going_concern_ev":  round(going_concern_ev,   4),
            "liquidation_val":   round(liquidation_val,    4),
            "g_base":            round(g_base,             4),
            "terminal_g":        g_terminal,
            "pv_explicit":       round(pv_sum,             4),
            "pv_terminal_value": round(pv_tv,              4),
        }
        return self._equity_bridge(ev, fcffs, extra)

    # ─────────────────────────────────────────────────────────────────────────
    # D-2. Stage 2: High Growth
    # ─────────────────────────────────────────────────────────────────────────

    def _high_growth_valuation(
        self,
        g_override:    Optional[float] = None,
        roic_override: Optional[float] = None,
        rev_cagr:      Optional[float] = None,
        phase1_years:  int   = 5,
        phase2_years:  int   = 5,
        g_terminal:    float = 0.025,
        **kwargs,
    ) -> dict:
        """
        Stage 2 — High Growth: 3-Phase Bottom-Up

        g_base 결정 우선순위
        --------------------
        1) g_override  (직접 입력)
        2) rev_cagr    (역사적 매출 CAGR) — 재무제표 RR 편향 보정
        3) ROIC × RR_base (재무제표 역산)
        하한: max(rf×1.5, WACC×0.4)

        Parameters
        ----------
        g_override    : 성장률 직접 입력 (fraction)
        roic_override : ROIC 직접 입력 (fraction)
        rev_cagr      : 역사적 매출 CAGR (fraction)
        phase1_years  : 고성장 유지 기간 (default 5년)
        phase2_years  : 수렴 기간 (default 5년)
        g_terminal    : 영구 성장률 (default 2.5%, 내부에서 ≤rf 강제)
        """
        eff_wacc  = self.wacc   # Stage 2 = 기본 WACC

        roic      = roic_override if roic_override is not None else self._base_roic()
        rr_base   = self._base_reinvestment_rate()
        g_roic    = roic * max(rr_base, 0.0)

        # g_base 결정
        if g_override is not None:
            g_base = float(g_override)
        elif rev_cagr is not None and rev_cagr > 0:
            g_max   = roic * 0.90   # RR ≤ 90% 보장
            g_base  = max(min(rev_cagr, g_max), g_roic)
        else:
            g_base  = g_roic

        # 하한: 성장주 최소 성장 보장
        g_floor = max(self.rf * 1.5, self.wacc * 0.4)
        if g_base < g_floor and g_override is None:
            g_base = g_floor

        g_base = min(g_base, 0.40)   # 절대 상한 40%

        fcffs, pv_sum, pv_tv = self._growth_path(
            g_base       = g_base,
            roic_base    = roic,
            g_terminal   = g_terminal,
            eff_wacc     = eff_wacc,
            phase1_years = phase1_years,
            phase2_years = phase2_years,
            nopat_start  = self._nopat(),
            note         = "high_growth",
        )

        pv_s1 = sum(r["pv_fcf"] for r in fcffs if r["year"] <= phase1_years)
        pv_s2 = sum(r["pv_fcf"] for r in fcffs if r["year"] >  phase1_years)

        extra = {
            "stage":             "high_growth",
            "eff_wacc":          round(eff_wacc,   4),
            "g_base":            round(g_base,     4),
            "roic_base":         round(roic,       4),
            "rr_base":           round(rr_base,    4),
            "terminal_g":        round(min(g_terminal, self.rf), 4),
            "pv_stage1":         round(pv_s1,      4),
            "pv_stage2":         round(pv_s2,      4),
            "pv_terminal_value": round(pv_tv,      4),
        }
        return self._equity_bridge(pv_sum + pv_tv, fcffs, extra)

    # ─────────────────────────────────────────────────────────────────────────
    # D-3. Stage 3: Mature
    # ─────────────────────────────────────────────────────────────────────────

    def _mature_valuation(
        self,
        g_stable:      float = 0.02,
        g_near:        float = 0.04,
        near_years:    int   = 5,
        wacc_discount: float = 0.005,
        g_terminal:    float = 0.02,
        **kwargs,
    ) -> dict:
        """
        Stage 3 — Mature: 낮은 성장, 안정적 수익

        eff_wacc = WACC − wacc_discount (default −0.5%p)
        → 성숙기 리스크 감소 반영

        [규칙] terminal_g = min(g_terminal, g_stable, rf)

        Parameters
        ----------
        g_stable       : 영구 성장률 (default 2%)
        g_near         : 근기 성장률 (default 4%)
        near_years     : 근기 기간 (default 5년)
        wacc_discount  : eff_wacc 할인폭 (default 0.5%p)
        g_terminal     : 영구 성장률 오버라이드 (default g_stable와 동일)
        """
        eff_wacc   = max(self.wacc - wacc_discount, self.rf + 0.005)  # rf보다는 높게
        g_tv       = min(g_stable, g_terminal, self.rf)

        roic_base  = self._base_roic()
        # 성숙기: ROIC는 WACC 수준으로 수렴 (초과 ROIC 감소)
        roic_start = min(roic_base, eff_wacc * 2.5)   # 최대 WACC × 2.5배로 제한

        fcffs, pv_sum, pv_tv = self._growth_path(
            g_base       = g_near,
            roic_base    = roic_start,
            g_terminal   = g_tv,
            eff_wacc     = eff_wacc,
            phase1_years = near_years,
            phase2_years = 5,
            nopat_start  = self._nopat(),
            note         = "mature",
        )

        pv_near = sum(r["pv_fcf"] for r in fcffs if r["year"] <= near_years)

        extra = {
            "stage":             "mature",
            "eff_wacc":          round(eff_wacc,   4),
            "wacc_discount":     wacc_discount,
            "terminal_g":        round(g_tv,        4),
            "terminal_g_capped": g_tv < g_stable,
            "rf_cap":            self.rf,
            "pv_near":           round(pv_near,    4),
            "pv_terminal_value": round(pv_tv,      4),
        }
        return self._equity_bridge(pv_sum + pv_tv, fcffs, extra)

    # ─────────────────────────────────────────────────────────────────────────
    # D-4. Stage 4: Decline
    # ─────────────────────────────────────────────────────────────────────────

    def _decline_valuation(
        self,
        g_decline:         float = -0.05,
        capex_ratio:       float = 0.50,
        liquidation_years: int   = 10,
        terminal_multiple: float = 3.0,
        **kwargs,
    ) -> dict:
        """
        Stage 4 — Decline: 매출 감소, 자산 청산 흐름

        동일 FCFF 공식 사용. 쇠퇴기 특성:
          - g_base < 0 (음수 성장률)
          - CapEx < D&A → RR < 0 → FCFF > NOPAT (자산 상각으로 현금 창출)
          - Terminal Value 대신 잔존 자산 청산가치 사용

        Parameters
        ----------
        g_decline         : 연간 매출 감소율 (default −5%, 양수 입력 시 자동 음수화)
        capex_ratio       : CapEx / D&A 비율 (default 0.50)
                            → capex_ratio < 1 이면 RR < 0 → 자산 회수 현금흐름
        liquidation_years : 청산 기간 (default 10년)
        terminal_multiple : 잔존 장부가치 회수 배수 (default 3배 D&A)
        """
        g_decline  = -abs(g_decline)
        eff_wacc   = self.wacc    # Stage 4 = 기본 WACC (청산 불확실성 포함)

        da_base       = max(self.fin.depr_amort, 1e-9)
        # 쇠퇴기 ROIC: CapEx < D&A 구조를 ROIC에 반영
        # ROIC_eff = NOPAT / IC  에서 IC를 줄어드는 자산 기준으로 추정
        roic_decline  = max(self._base_roic() * capex_ratio, self._GUARD_ROIC_FLOOR)

        # 초기 NOPAT은 현재 재무제표 기반
        nopat_start   = max(self._nopat(), da_base * 0.1)   # D&A의 10% 최소 수익성 보장

        fcffs, pv_sum, _ = self._growth_path(
            g_base       = g_decline,    # 음수 성장률
            roic_base    = roic_decline,
            g_terminal   = g_decline,    # 쇠퇴기: 수렴 없이 계속 하락
            eff_wacc     = eff_wacc,
            phase1_years = liquidation_years // 2,
            phase2_years = liquidation_years - liquidation_years // 2,
            nopat_start  = nopat_start,
            note         = "decline",
        )

        # Terminal Value 대신 잔존 자산 청산가치
        residual_asset_val = (da_base * terminal_multiple
                              * self._pv_factor(eff_wacc, liquidation_years))
        ev = pv_sum + residual_asset_val

        extra = {
            "stage":           "decline",
            "eff_wacc":        round(eff_wacc,          4),
            "g_decline":       round(g_decline,          4),
            "capex_ratio":     capex_ratio,
            "pv_operating":    round(pv_sum,             4),
            "pv_liquidation":  round(residual_asset_val, 4),
            "terminal_g":      g_decline,
            "note":            "Terminal = residual asset liquidation (no perpetuity)",
        }
        return self._equity_bridge(ev, fcffs, extra)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 빠른 검증용 유틸
# ═══════════════════════════════════════════════════════════════════════════════

def _quick_sanity_check():
    """
    삼성전자 FY2025 근사치로 결과 확인 (단위: 조원)
    실행: python rolling_dcf.py
    """
    fin = Financials(
        revenue           = 333.6,   # 333.6조 매출
        ebit              = 43.6,    # 43.6조 영업이익
        ebit_margin       = 0.131,
        tax_rate          = 0.086,   # 실제 법인세율 8.6%
        depr_amort        = 34.0,    # D&A 34조
        capex             = 53.9,    # CAPEX 53.9조
        change_wc         =  9.6,    # 운전자본 증가 9.6조
        cash_st           = 57.9,    # 기말현금 57.9조
        debt              = 22.0,    # 금융부채 22조
        minority_interest = 12.0,    # 비지배지분 12조
        shares            = 0.005970,  # 59.70억주 → T-shares
    )

    engine = DamodaranDCF(fin, rf=0.032, erp=0.065, beta=1.10)

    print("=" * 65)
    print("  Damodaran 4-Stage Life Cycle DCF (단일 FCFF 공식)")
    print("  Samsung Electronics FY2025 (단위: 조원)")
    print("=" * 65)
    print(f"  기본 WACC : {engine.wacc * 100:.2f}%")
    print(f"  CoE       : {engine.coe  * 100:.2f}%")
    print(f"  CoD(세후)  : {engine.cod  * 100:.2f}%")
    print(f"  ROIC      : {engine._base_roic() * 100:.2f}%")
    print(f"  기본 RR   : {engine._base_reinvestment_rate() * 100:.2f}%")
    print("-" * 65)

    stage_kwargs = {
        1: dict(tam=2000.0, target_share=0.15, target_margin=0.20,
                prob_failure=0.05, stage_premium=0.03),
        2: dict(rev_cagr=0.12, g_terminal=0.025),
        3: dict(g_stable=0.025, g_near=0.04, wacc_discount=0.005),
        4: dict(g_decline=0.05, capex_ratio=0.40),
    }
    stage_names  = {1:"Startup", 2:"High Growth", 3:"Mature", 4:"Decline"}
    stage_wacc_note = {
        1: f"(WACC+3%p={engine.wacc*100+3:.1f}%)",
        2: f"(WACC={engine.wacc*100:.1f}%)",
        3: f"(WACC-0.5%p={engine.wacc*100-0.5:.1f}%)",
        4: f"(WACC={engine.wacc*100:.1f}%)",
    }

    for stage in [1, 2, 3, 4]:
        res = engine.calculate_intrinsic_value(stage=stage, **stage_kwargs[stage])
        iv  = res["intrinsic_value"]
        ev  = res["ev"]
        eff = res.get("eff_wacc", engine.wacc)
        print(f"  Stage {stage} ({stage_names[stage]:12s}) {stage_wacc_note[stage]:20s}: "
              f"IV = {iv:>10,.0f} 원/주  |  EV = {ev:.1f}T")

    print("=" * 65)
    print("  현재 주가: 292,500원")
    print("=" * 65)


if __name__ == "__main__":
    _quick_sanity_check()
