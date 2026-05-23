"""
rolling_dcf.py
==============
Aswath Damodaran 4-Stage Life Cycle DCF Valuation Engine
for Korean Stocks (K-IFRS Consolidated Financials)

단위 규약 (Unit Convention)
--------------------------
  - 모든 금액은 조원(T-KRW, 1조 = 1e12 원) 기준으로 통일
  - shares : 조 주(T-shares) → 주당 가치 = equity_value(T) / shares(T) = 원(KRW)
  - 비율(rate, margin 등) : fraction (0.0 ~ 1.0)

사용 예시
---------
  fin = Financials(
      revenue=30.0, ebit=4.5, ebit_margin=0.15, tax_rate=0.22,
      depr_amort=2.0, capex=3.5, change_wc=0.3,
      cash_st=5.0, debt=8.0, minority_interest=0.5,
      shares=0.005,            # 50억주 → 0.005 T-shares
  )
  engine = DamodaranDCF(fin, rf=0.035, erp=0.075, beta=1.1)
  result = engine.calculate_intrinsic_value(stage=2)
  print(result['intrinsic_value'])   # KRW per share
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
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
    shares:             float   # 발행주식수 (T-shares, 조 주 단위)
                                # 예: 5,919,638천주 → 5.919638e9 / 1e12 = 0.005919638


# ═══════════════════════════════════════════════════════════════════════════════
# 2. DCF 엔진 메인 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class DamodaranDCF:
    """
    Aswath Damodaran 4-Stage Life Cycle DCF Valuation Engine

    Parameters
    ----------
    financials : Financials
        크롤링·전처리 파이프라인에서 전달받은 재무 데이터
    rf : float
        한국 국고채 10년물 무위험수익률 (fraction, default 3.5%)
    erp : float
        한국 내재 ERP = Base ERP(글로벌) + CRP(Korea) (fraction, default 7.5%)
        * 다모다란 ERP 기준: Base 5% + Korea CRP ≈ 2.5% → 7.5%
    beta : float
        52주 또는 5년 주간 베타 (default 1.0)
    debt_spread : float
        부채 신용 스프레드 (fraction, default 1.5%)
        * EBIT이 음수이면 내부적으로 자동으로 5.0%로 확대
    equity_weight : float | None
        자본 비중. None이면 fin.cash_st·debt·shares 등으로 추정.
    """

    _GUARD_REINV_MIN  = -0.5    # 재투자율 하한 (쇠퇴기 포함)
    _GUARD_REINV_MAX  =  0.95   # 재투자율 상한
    _GUARD_ROIC_FLOOR =  0.001  # ROIC 하한 (0 나눗셈 방지)

    def __init__(
        self,
        financials:     Financials,
        rf:             float = 0.035,
        erp:            float = 0.075,
        beta:           float = 1.0,
        debt_spread:    float = 0.015,
        equity_weight:  Optional[float] = None,
    ):
        self.fin          = financials
        self.rf           = rf
        self.erp          = erp
        self.beta         = beta
        self.debt_spread  = debt_spread
        self._eq_weight   = equity_weight

        # WACC는 생성 시 1회 계산 (시장 자본구조 기준)
        self.wacc, self.coe, self.cod = self._calculate_wacc()

    # ─────────────────────────────────────────────────────────────────────────
    # A. WACC 계산
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_wacc(self) -> tuple[float, float, float]:
        """
        한국 시장 특성을 반영한 WACC 계산.

        Cost of Equity  = rf + β × (Base_ERP + CRP)         ← CAPM
        Cost of Debt    = rf + spread (EBIT 음수 시 5% 스프레드)
        가중치           = 시장가치 기준 D/(D+E), E/(D+E)

        Equity 시장가치 추정:
          - fin.shares(T-shares) × 주당 BV 같은 직접값이 없으므로
            'debt 대비 경험적 D/E 비율'은 사용하지 않고,
            대신 caller가 equity_weight를 명시하거나,
            debt / (debt + implied_equity) 방식으로 추정.
          - implied_equity: 연결 자본총계를 조원 단위로 전달하거나
            없을 경우 전통적 70:30 (equity:debt) 기본값 사용.
        """
        # ── Cost of Equity (CAPM) ───────────────────────────────────────────
        coe = self.rf + self.beta * self.erp

        # ── Cost of Debt ────────────────────────────────────────────────────
        # EBIT이 음수(적자)이면 부도 위험 상승 → 스프레드 확대
        if self.fin.ebit <= 0:
            effective_spread = max(self.debt_spread, 0.05)
        else:
            effective_spread = self.debt_spread
        cod_pretax = self.rf + effective_spread
        cod        = cod_pretax * (1 - self.fin.tax_rate)   # After-tax

        # ── 자본구조 가중치 ──────────────────────────────────────────────────
        if self._eq_weight is not None:
            e_w = max(0.0, min(1.0, self._eq_weight))
            d_w = 1.0 - e_w
        else:
            total = self.fin.debt
            if total > 0:
                # EBIT 기반 간이 Debt/EV 추정
                # NOPAT 배수(EV/NOPAT ≈ 15배)로 implied EV 추정
                nopat_proxy = max(self.fin.ebit * (1 - self.fin.tax_rate), 1e-6)
                implied_ev  = nopat_proxy / max(self.wacc if hasattr(self, 'wacc') else 0.08, 0.05)
                # 첫 번째 호출이므로 wacc 미정 → 대신 COE 기반 EV 추정
                implied_ev  = nopat_proxy / max(coe * 0.9, 0.05)
                d_w = min(total / (implied_ev + total), 0.60)   # Debt 비중 상한 60%
                e_w = 1.0 - d_w
            else:
                e_w, d_w = 1.0, 0.0

        wacc = e_w * coe + d_w * cod
        return wacc, coe, cod

    # ─────────────────────────────────────────────────────────────────────────
    # B. 공통 유틸리티
    # ─────────────────────────────────────────────────────────────────────────

    def _nopat(self) -> float:
        """세후 영업이익 (NOPAT = EBIT × (1 − t))"""
        return self.fin.ebit * (1 - self.fin.tax_rate)

    def _base_reinvestment_rate(self) -> float:
        """
        재투자율 = (CapEx − D&A + ΔWC) / NOPAT
        NOPAT ≤ 0이면 수익성 없는 성장으로 간주하여 0.5 반환.
        """
        nopat = self._nopat()
        if nopat <= 0:
            return 0.5
        reinv  = self.fin.capex - self.fin.depr_amort + self.fin.change_wc
        rr     = reinv / nopat
        return max(self._GUARD_REINV_MIN, min(rr, self._GUARD_REINV_MAX))

    def _base_roic(self) -> float:
        """
        ROIC = NOPAT / Invested Capital

        Invested Capital 추정 (3가지 방법 중 최대값 사용):
          1. D&A 기반 자산 역산: D&A × 상각연수(7년) → 총자산 프록시
             - 자본집약적 기업(반도체·중공업 등 D&A 大)에 적합
             - D&A가 클수록 IC도 크게 반영 → ROIC 과대 방지
          2. 매출 배수: revenue × 0.8 (자산회전율 1.25x 가정)
             - 일반 제조/서비스업 기본값
          3. 부채+자본 장부 프록시: debt + equity_proxy
             - equity_proxy = NOPAT / (CoE - rf) 역산 불가 시 생략

        [수정 전 문제]
          (CapEx - D&A) * 5 + revenue * 0.25 공식은
          CapEx ≈ D&A인 기업(SK하이닉스 등)에서 IC를 극단적으로 과소추정
          → ROIC 69%처럼 비현실적 수치 발생 → g 과대 → 재투자율 클램프
          → 오히려 FCFF가 적게 계산되는 역설 발생
        """
        nopat = self._nopat()
        da    = max(self.fin.depr_amort, 1e-9)

        # IC 추정: max(D&A × 5년, 매출 × 0.5)
        #
        # D&A × 5 : 총 유형·무형자산 장부가 역산 (평균 상각연수 5년 가정)
        #   - 반도체·중공업 등 D&A 大 자본집약 기업에 주효
        #   - 예) SK하이닉스 D&A 20조 → IC 100조 → ROIC 18% (실제 22%에 근접)
        # 매출 × 0.5 : 자산회전율 2.0x 기반 IC 하한
        #   - 경자산(light-asset) 기업에서 D&A가 작을 때 IC 과소 방지
        # 두 값의 max → 어느 업종이든 한쪽은 현실적 IC를 잡아줌
        #
        # [수정 전 문제 요약]
        #   (CapEx - D&A) * 5 + Rev * 0.25 공식:
        #   CapEx ≈ D&A인 기업에서 (CapEx-D&A) ≈ 0 → IC = Rev*0.25만 남음
        #   → IC 과소 → ROIC 50%+ 비현실적 → g 클램프 → FCFF 왜곡
        ic_da_based  = da * 5.0              # D&A 기반 (자산회전 중심 업종)
        ic_rev_based = self.fin.revenue * 0.5  # 매출 기반 (경자산 업종 하한)
        ic   = max(ic_da_based, ic_rev_based, 1e-9)
        roic = nopat / ic

        # ROIC 상한 40%: 초과 시 IC 추정 오류 가능성, 다모다란 실증 범위
        return min(max(roic, self._GUARD_ROIC_FLOOR), 0.40)

    @staticmethod
    def _pv_factor(wacc: float, t: int) -> float:
        return 1.0 / ((1.0 + wacc) ** t)

    def _equity_bridge(
        self,
        ev:    float,
        fcffs: Optional[list[dict]] = None,
        extra: Optional[dict]       = None,
    ) -> dict:
        """
        Equity Value Bridge (K-IFRS 연결재무제표 기준)
        ─────────────────────────────────────────────
        Equity Value = EV + Cash & ST Invest
                          − Total Debt
                          − Minority Interest    ← K-IFRS 연결 특성 반영
        주당 내재가치 = Equity Value (T-KRW) / Shares (T-shares)
                     = KRW per share
        """
        equity_value     = ev + self.fin.cash_st - self.fin.debt - self.fin.minority_interest
        # shares: T-shares (조 주), equity_value: T-KRW (조원)
        # 조원 / 조주 = 원/주  → 단위 완벽 일치
        price_per_share  = (equity_value / self.fin.shares) if self.fin.shares > 0 else 0.0

        result = {
            "intrinsic_value": price_per_share,     # KRW per share
            "ev":              ev,                   # T-KRW
            "equity_value":    equity_value,         # T-KRW
            "cash_st":         self.fin.cash_st,     # T-KRW
            "debt":            self.fin.debt,        # T-KRW
            "minority":        self.fin.minority_interest,  # T-KRW
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
    # C. 공개 인터페이스
    # ─────────────────────────────────────────────────────────────────────────

    def calculate_intrinsic_value(self, stage: int = 2, **kwargs) -> dict:
        """
        생애주기(stage)에 따라 DCF 로직을 분기하여 주당 내재가치를 반환.

        Parameters
        ----------
        stage : int
            1 = Startup   (신생·초기 성장기)
            2 = High Growth (고성장기)
            3 = Mature      (성숙 안정기)
            4 = Decline     (쇠퇴·청산기)
        **kwargs : 각 스테이지별 추가 파라미터 (하단 메서드 doc 참조)

        Returns
        -------
        dict : {
            'intrinsic_value' : float,   # KRW per share ← 핵심 출력값
            'ev'              : float,   # Enterprise Value (T-KRW)
            'equity_value'    : float,   # Equity Value    (T-KRW)
            'wacc'            : float,
            'fcff_schedule'   : list,    # 연도별 추정 FCFF 내역
            ... (스테이지별 추가 정보)
        }
        """
        dispatch = {
            1: self._startup_valuation,
            2: self._high_growth_valuation,
            3: self._mature_valuation,
            4: self._decline_valuation,
        }
        func = dispatch.get(stage, self._high_growth_valuation)
        return func(**kwargs)

    # ─────────────────────────────────────────────────────────────────────────
    # D-1. Stage 1: Startup — Top-Down 추정
    # ─────────────────────────────────────────────────────────────────────────

    def _startup_valuation(
        self,
        tam:                float = 0.0,
        target_share:       float = 0.10,
        target_margin:      float = 0.15,
        prob_failure:       float = 0.30,
        liquidation_val_pct: float = 0.50,
        ramp_years:         int   = 10,
        **kwargs,
    ) -> dict:
        """
        Option 1: 신생·초기 성장기 — Top-Down 추정

        Parameters
        ----------
        tam              : 목표 시장 규모 T-KRW (미입력 시 현재 매출의 20배)
        target_share     : 10년 뒤 시장 점유율 (default 10%)
        target_margin    : 10년 뒤 EBIT 마진  (default 15%)
        prob_failure     : 파산 확률           (default 30%)
        liquidation_val_pct : 파산 시 현금성 자산 회수율 (default 50%)
        ramp_years       : 추정 기간            (default 10년)

        로직
        ----
        1. 현재 매출 → 10년 뒤 목표 매출(TAM × 점유율)로 직선 성장률 계산
        2. EBIT 마진도 현재 → 목표 마진으로 선형 개선
        3. 재투자율은 초기 높고(0.8) 점차 낮아짐(rr_terminal)
        4. EV = Σ PV(FCFF) + PV(Terminal Value)
        5. Going-Concern Value × (1 − P_failure) + Liquidation Value × P_failure
        """
        # ── 기본 파라미터 설정 ──────────────────────────────────────────────
        if tam <= 0:
            tam = self.fin.revenue * 20.0

        rev_yr0     = max(self.fin.revenue, 1e-9)
        rev_yr_n    = tam * target_share
        margin_yr0  = self.fin.ebit_margin
        margin_yr_n = target_margin

        # 초기 재투자율: 스타트업은 거의 모든 현금 재투자
        rr_initial  = 0.85
        g_terminal  = min(0.025, self.rf)       # 영구 성장률 ≤ rf (최대 2.5%)
        rr_terminal = g_terminal / max(self.wacc, 0.01)

        # ── 연도별 FCFF 추정 ────────────────────────────────────────────────
        fcffs     = []
        pv_fcff   = 0.0

        for t in range(1, ramp_years + 1):
            alpha       = t / ramp_years                                 # 0 → 1 선형 보간
            rev_t       = rev_yr0 * ((rev_yr_n / rev_yr0) ** alpha)     # 지수 경로
            margin_t    = margin_yr0 + alpha * (margin_yr_n - margin_yr0)
            ebit_t      = rev_t * margin_t
            nopat_t     = ebit_t * (1 - self.fin.tax_rate)

            # 재투자율: 초기 높고 → 터미널 RR로 수렴
            rr_t        = rr_initial + alpha * (rr_terminal - rr_initial)
            rr_t        = max(0.0, min(rr_t, 0.95))
            fcf_t       = nopat_t * (1 - rr_t)

            pv_t        = fcf_t * self._pv_factor(self.wacc, t)
            pv_fcff    += pv_t

            fcffs.append({
                "year":      t,
                "revenue":   round(rev_t,    4),
                "ebit":      round(ebit_t,   4),
                "nopat":     round(nopat_t,  4),
                "reinv_rate": round(rr_t,    4),
                "fcf":       round(fcf_t,    4),
                "pv_fcf":    round(pv_t,     4),
                "note":      "ramp",
            })

        # ── Terminal Value (Year ramp_years 이후) ───────────────────────────
        last_nopat = fcffs[-1]["nopat"] * (1 + g_terminal)
        fcf_term   = last_nopat * (1 - rr_terminal)
        tv         = fcf_term / max(self.wacc - g_terminal, 0.001)
        pv_tv      = tv * self._pv_factor(self.wacc, ramp_years)

        # ── Going-Concern Value ─────────────────────────────────────────────
        going_concern_ev = pv_fcff + pv_tv

        # ── 파산 확률 반영 (다모다란 Distress Value) ─────────────────────────
        # 파산 시 청산 가치: 현금성 자산의 일부 회수
        liquidation_val  = (self.fin.cash_st + self.fin.depr_amort * 3) * liquidation_val_pct
        ev               = (going_concern_ev * (1 - prob_failure)
                           + liquidation_val   *  prob_failure)

        extra = {
            "stage":              "startup",
            "tam":                tam,
            "target_share":       target_share,
            "target_margin":      target_margin,
            "prob_failure":       prob_failure,
            "going_concern_ev":   going_concern_ev,
            "liquidation_val":    liquidation_val,
            "terminal_g":         g_terminal,
            "pv_stage":           round(pv_fcff, 4),
            "pv_terminal_value":  round(pv_tv,   4),
        }
        return self._equity_bridge(ev, fcffs, extra)

    # ─────────────────────────────────────────────────────────────────────────
    # D-2. Stage 2: High Growth — 3-Stage Bottom-Up
    # ─────────────────────────────────────────────────────────────────────────

    def _high_growth_valuation(
        self,
        g_override:    Optional[float] = None,
        roic_override: Optional[float] = None,
        rev_cagr:      Optional[float] = None,
        **kwargs,
    ) -> dict:
        """
        Option 2: 고성장기 — 3-Stage Bottom-Up (Damodaran 방법론)

        구조
        ----
        Phase 1 (t=1~5)  : 고성장기 — g_base 유지, RR=g/ROIC
        Phase 2 (t=6~10) : 이행기  — g와 ROIC 모두 rf/WACC로 선형 수렴
        Phase 3 (t=11~∞) : 영구기  — g=g_terminal(≤rf), ROIC=WACC

        성장률(g_base) 결정 로직
        -----------------------
        1순위: g_override 명시값
        2순위: rev_cagr (역사적 매출 CAGR) — 가장 신뢰도 높은 성장 입력
               단, rev_cagr > ROIC이면 RR=1에 걸리므로 min(rev_cagr, ROIC*0.85)
        3순위: ROIC × RR_base (재무제표 역산)
        하한:  max(rf × 1.5, WACC × 0.4) — 성장주 최소 성장 보장

        RR = g / ROIC 공식의 의미
        --------------------------
        Damodaran의 Reinvestment Rate = g / ROIC 는
        "성장률 g를 달성하기 위해 NOPAT의 몇 %를 재투자해야 하나"를 나타냄.
        g가 높으면 RR도 높아져 단기 FCFF가 줄지만, 고성장이 지속되면
        장기 Terminal Value가 이를 상쇄하므로 전체 EV는 증가함.
        이것이 Damodaran 모델의 정상 동작임.
        """
        roic    = roic_override if roic_override is not None else self._base_roic()
        rr_base = self._base_reinvestment_rate()
        g_roic  = roic * max(rr_base, 0.0)

        # ── g_base 결정 ─────────────────────────────────────────────────────
        if g_override is not None:
            g_base = float(g_override)
        elif rev_cagr is not None and rev_cagr > 0:
            # rev_cagr을 직접 사용 (블렌딩 최소화)
            # ROIC 제약: RR=g/ROIC가 95%를 넘지 않도록 g 상한 설정
            g_max_by_roic = roic * 0.90   # RR ≤ 90%
            g_base = min(rev_cagr, g_max_by_roic)
            # 단, g_roic가 더 높으면 g_roic도 고려
            g_base = max(g_base, g_roic)
        else:
            g_base = g_roic

        # ── 하한 보정 ───────────────────────────────────────────────────────
        # 고성장 기업 지정 시 최소한 rf*1.5 이상은 성장해야 함
        g_floor = max(self.rf * 1.5, self.wacc * 0.4)
        if g_base < g_floor and g_override is None:
            g_base = g_floor

        g_base = min(g_base, 0.40)   # 절대 상한 40%

        nopat_base    = self._nopat()
        current_nopat = nopat_base
        fcffs, pv_fcff, pv_stage1, pv_stage2 = [], 0.0, 0.0, 0.0

        for t in range(1, 11):
            if t <= 5:
                g_t    = g_base
                roic_t = roic
                phase  = "high_growth"
            else:
                alpha  = (t - 5) / 5.0
                g_t    = g_base * (1 - alpha) + self.rf * alpha
                roic_t = max(roic * (1 - alpha) + self.wacc * alpha,
                             self._GUARD_ROIC_FLOOR)
                phase  = "transition"

            # RR = g / ROIC (Damodaran 핵심 공식)
            rr_t = max(0.0, min(g_t / roic_t, self._GUARD_REINV_MAX))

            current_nopat *= (1 + g_t)
            fcf_t  = current_nopat * (1 - rr_t)
            pv_t   = fcf_t * self._pv_factor(self.wacc, t)
            pv_fcff += pv_t
            if t <= 5:
                pv_stage1 += pv_t
            else:
                pv_stage2 += pv_t

            fcffs.append({
                "year":       t,
                "growth_g":   round(g_t,          4),
                "roic":       round(roic_t,        4),
                "reinv_rate": round(rr_t,          4),
                "nopat":      round(current_nopat, 4),
                "fcf":        round(fcf_t,         4),
                "pv_fcf":     round(pv_t,          4),
                "phase":      phase,
            })

        # ── Phase 3: 영구 성장기 ─────────────────────────────────────────────
        # g_terminal = min(g_base, rf) — 절대로 무위험수익률 초과 불가
        g_terminal  = max(min(g_base, self.rf), 0.01)
        rr_terminal = g_terminal / max(self.wacc, 0.001)
        nopat_t11   = current_nopat * (1 + g_terminal)
        fcf_t11     = nopat_t11 * (1 - rr_terminal)
        tv          = fcf_t11 / max(self.wacc - g_terminal, 0.001)
        pv_tv       = tv * self._pv_factor(self.wacc, 10)

        ev = pv_fcff + pv_tv
        extra = {
            "stage":             "high_growth",
            "g_base":            round(g_base,    4),
            "roic_base":         round(roic,       4),
            "rr_base":           round(rr_base,    4),
            "terminal_g":        round(g_terminal, 4),
            "pv_stage1":         round(pv_stage1,  4),
            "pv_stage2":         round(pv_stage2,  4),
            "pv_terminal_value": round(pv_tv,      4),
        }
        return self._equity_bridge(ev, fcffs, extra)

    # ─────────────────────────────────────────────────────────────────────────
    # D-3. Stage 3: Mature — 1-Stage 또는 2-Stage 안정 성장 모델
    # ─────────────────────────────────────────────────────────────────────────

    def _mature_valuation(
        self,
        g_stable:      float = 0.02,
        use_two_stage: bool  = True,
        g_near:        float = 0.04,
        near_years:    int   = 5,
        **kwargs,
    ) -> dict:
        """
        Option 3: 성숙 안정기

        [엄격한 규칙] terminal_g = min(g_stable, rf)
        → 성숙 기업의 영구 성장률은 국가 경제 성장률(≈ rf)을 초과 불가.

        Parameters
        ----------
        g_stable      : 영구 성장률 (default 2%). 내부적으로 min(g_stable, rf) 적용.
        use_two_stage : True면 근기(1~near_years) + 영구기 2-Stage,
                        False면 단순 고든 성장 모델 (Gordon Growth).
        g_near        : 2-Stage 사용 시 근기 성장률 (default 4%)
        near_years    : 근기 기간 (default 5년)
        """
        # ── [엄격한 규칙] 영구 성장률 캡핑 ─────────────────────────────────
        g_terminal = min(g_stable, self.rf)    # 절대로 rf 초과 불가
        nopat_base = self._nopat()

        # 재투자율 = g / WACC (성숙기에는 ROIC ≈ WACC)
        rr_terminal = g_terminal / max(self.wacc, 0.001)

        fcffs   = []
        pv_fcff = 0.0

        if use_two_stage:
            # ── 2-Stage: 근기 고성장 → 영구 안정 성장 ─────────────────────
            g_near_eff   = min(g_near, self.wacc)   # 근기도 WACC 초과 방지 (보수적)
            current_nopat = nopat_base

            for t in range(1, near_years + 1):
                rr_t = g_near_eff / max(self._base_roic(), 0.001)
                rr_t = max(rr_terminal, min(rr_t, 0.80))
                current_nopat *= (1 + g_near_eff)
                fcf_t  = current_nopat * (1 - rr_t)
                pv_t   = fcf_t * self._pv_factor(self.wacc, t)
                pv_fcff += pv_t

                fcffs.append({
                    "year":       t,
                    "growth_g":   round(g_near_eff,   4),
                    "reinv_rate": round(rr_t,          4),
                    "nopat":      round(current_nopat, 4),
                    "fcf":        round(fcf_t,         4),
                    "pv_fcf":     round(pv_t,          4),
                    "phase":      "near_stable",
                })

            # 영구 성장기 (near_years 이후)
            nopat_tv  = current_nopat * (1 + g_terminal)
            fcf_tv    = nopat_tv * (1 - rr_terminal)
            tv        = fcf_tv / max(self.wacc - g_terminal, 0.001)
            pv_tv     = tv * self._pv_factor(self.wacc, near_years)
            ev        = pv_fcff + pv_tv

        else:
            # ── 1-Stage: 단순 고든 성장 모델 ────────────────────────────────
            fcf_stable = nopat_base * (1 + g_terminal) * (1 - rr_terminal)
            tv         = fcf_stable / max(self.wacc - g_terminal, 0.001)
            pv_tv      = tv   # 즉시 적용 (t=0 기준)
            pv_fcff    = 0.0
            ev         = pv_tv

            fcffs.append({
                "year":       "∞",
                "growth_g":   round(g_terminal,     4),
                "reinv_rate": round(rr_terminal,    4),
                "nopat":      round(nopat_base,     4),
                "fcf":        round(fcf_stable / (1 + g_terminal), 4),
                "pv_fcf":     round(ev,             4),
                "phase":      "gordon_growth",
            })
            pv_tv = ev

        extra = {
            "stage":             "mature",
            "terminal_g":        round(g_terminal,  4),
            "terminal_g_capped": g_terminal < g_stable,  # 캡핑 여부 표시
            "rf_cap":            self.rf,
            "two_stage":         use_two_stage,
            "pv_near":           round(pv_fcff,     4),
            "pv_terminal_value": round(pv_tv,       4),
        }
        return self._equity_bridge(ev, fcffs, extra)

    # ─────────────────────────────────────────────────────────────────────────
    # D-4. Stage 4: Decline — Liquidating Cash Flow 모델
    # ─────────────────────────────────────────────────────────────────────────

    def _decline_valuation(
        self,
        g_decline:        float = -0.05,
        capex_ratio:      float = 0.50,
        liquidation_years: int  = 10,
        terminal_multiple: float = 3.0,
        **kwargs,
    ) -> dict:
        """
        Option 4: 쇠퇴기 — Liquidating Cash Flow 모델

        핵심 메커니즘
        -------------
        1. 매출 성장률 < 0 (g_decline, default −5%)
        2. CapEx << D&A → 재투자율 음수 → 자산 상각으로 현금 창출
        3. 10년 청산 구조: 영구가치 대신 잔존 청산가치(BV 배수) 적용

        Parameters
        ----------
        g_decline         : 연간 매출 감소율 (default −5%, 양수 입력 시 자동 음수화)
        capex_ratio       : CapEx / D&A 비율 (default 0.50, 즉 CapEx = D&A의 50%)
                            → 재투자율 = (CapEx − D&A + ΔWC) / NOPAT < 0
        liquidation_years : 청산 기간 (default 10년)
        terminal_multiple : 잔존 장부가치 회수 배수 (default 3배 D&A 기반 추정)

        [검증]
        CapEx = D&A × capex_ratio (< D&A)
        재투자율 = (CapEx − D&A + 0) / NOPAT
               = D&A × (capex_ratio − 1) / NOPAT
               < 0   (capex_ratio < 1이므로)
        → (1 − 재투자율) > 1 → NOPAT보다 많은 현금 창출 ✓
        """
        # 성장률이 양수로 입력된 경우 음수로 강제 변환
        g_decline = -abs(g_decline)

        nopat_base    = self._nopat()
        da_base       = self.fin.depr_amort
        current_nopat = nopat_base

        # 쇠퇴기 CapEx 설정 (D&A의 일부분)
        effective_capex = da_base * capex_ratio

        # 기초 재투자율 (음수가 되도록 설계)
        # change_wc는 쇠퇴기에 운전자본 축소(음수 ΔWC = 현금 유입)로 설정
        wc_release_rate = 0.02   # 연간 매출 2%씩 운전자본 회수
        rr_initial = (effective_capex - da_base) / max(abs(nopat_base), 1e-9)
        # rr_initial ≈ (0.5 × D&A − D&A) / NOPAT = −0.5 × D&A / NOPAT < 0

        fcffs     = []
        pv_fcff   = 0.0
        rev_t     = self.fin.revenue

        for t in range(1, liquidation_years + 1):
            # 매출 감소 적용
            rev_t        *= (1 + g_decline)
            da_t          = da_base * max(1 + g_decline * t * 0.5, 0.2)   # D&A도 점차 감소
            capex_t       = da_t * capex_ratio
            wc_inflow_t   = rev_t * wc_release_rate                        # 운전자본 회수 현금 유입

            # NOPAT: 영업이익 악화 (매출 감소, 마진 압박)
            margin_t      = self.fin.ebit_margin * max(1 + g_decline * t * 0.3, 0.3)
            ebit_t        = rev_t * margin_t
            nopat_t       = ebit_t * (1 - self.fin.tax_rate)

            # 재투자율 (음수 → 현금 창출 가속)
            reinv_t       = (capex_t - da_t - wc_inflow_t) / max(abs(nopat_t), 1e-9)
            reinv_t       = max(-0.80, min(reinv_t, 0.20))   # 재투자율 범위 −80%~+20%

            # FCFF > NOPAT (자산 상각으로 현금 추가 창출)
            fcf_t  = nopat_t * (1 - reinv_t)
            pv_t   = fcf_t * self._pv_factor(self.wacc, t)
            pv_fcff += pv_t

            fcffs.append({
                "year":       t,
                "revenue":    round(rev_t,    4),
                "ebit":       round(ebit_t,   4),
                "nopat":      round(nopat_t,  4),
                "capex":      round(capex_t,  4),
                "da":         round(da_t,     4),
                "reinv_rate": round(reinv_t,  4),
                "fcf":        round(fcf_t,    4),
                "pv_fcf":     round(pv_t,     4),
                "phase":      "decline",
            })

        # ── 청산 잔존 가치 (Terminal Liquidation Value) ─────────────────────
        # 10년 후 남은 유형자산 가치 추정: D&A × terminal_multiple 기반
        residual_asset_val = da_base * terminal_multiple * self._pv_factor(self.wacc, liquidation_years)
        ev = pv_fcff + residual_asset_val

        extra = {
            "stage":            "decline",
            "g_decline":        round(g_decline,         4),
            "capex_ratio":      capex_ratio,
            "pv_operating":     round(pv_fcff,           4),
            "pv_liquidation":   round(residual_asset_val, 4),
            "terminal_g":       g_decline,
            "note":             "No perpetuity; residual asset liquidation value applied",
        }
        return self._equity_bridge(ev, fcffs, extra)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 간단한 연산 검증용 유틸 (선택 사항)
# ═══════════════════════════════════════════════════════════════════════════════

def _quick_sanity_check():
    """
    삼성전자 FY2024 근사치로 결과 확인용 (단위: 조원)
    실행: python rolling_dcf.py
    """
    fin = Financials(
        revenue           = 300.0,   # 약 300조 매출
        ebit              = 32.0,    # 약 32조 영업이익
        ebit_margin       = 0.107,
        tax_rate          = 0.22,
        depr_amort        = 27.0,
        capex             = 35.0,
        change_wc         =  3.0,
        cash_st           = 98.0,
        debt              = 15.0,
        minority_interest =  3.5,
        shares            = 0.005975,  # ≈ 59.75억주 → T-shares
    )

    engine = DamodaranDCF(fin, rf=0.035, erp=0.075, beta=1.05)

    print("=" * 65)
    print("  Damodaran 4-Stage Life Cycle DCF - Sanity Check")
    print("=" * 65)
    print(f"  WACC  : {engine.wacc * 100:.2f}%")
    print(f"  CoE   : {engine.coe  * 100:.2f}%")
    print(f"  CoD   : {engine.cod  * 100:.2f}% (after-tax)")
    print("-" * 65)

    for stage, name in [(1,"Startup"),(2,"High Growth"),(3,"Mature"),(4,"Decline")]:
        kw = {}
        if stage == 1:
            kw = dict(tam=1000.0, target_share=0.15, target_margin=0.15, prob_failure=0.05)
        res = engine.calculate_intrinsic_value(stage=stage, **kw)
        iv  = res['intrinsic_value']
        ev  = res['ev']
        print(f"  Stage {stage} ({name:12s}): IV = {iv:>10,.0f} 원/주  |  EV = {ev:.1f}T")

    print("=" * 65)


if __name__ == "__main__":
    _quick_sanity_check()
