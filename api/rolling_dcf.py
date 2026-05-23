import pandas as pd
import numpy as np
from dataclasses import dataclass, field

@dataclass
class Financials:
    """Latest-year actuals snapshot."""
    revenue:              float          # T-won (조원)
    ebit:                 float          # T-won (조원)
    ebit_margin:          float          # fraction
    tax_rate:             float          # fraction
    depr_amort:           float          # T-won
    capex:                float          # T-won
    change_wc:            float          # T-won
    cash_st:              float          # T-won (Cash + ST Investments)
    debt:                 float          # T-won
    minority_interest:    float          # T-won
    shares:               float          # T-shares (조주)

class DamodaranDCF:
    """
    Aswath Damodaran's 4-Stage Life Cycle DCF Valuation Engine
    """
    def __init__(self, financials: Financials, rf=0.035, erp=0.075, beta=1.0):
        self.fin = financials
        self.rf = rf
        self.erp = erp
        self.beta = beta
        self.wacc = self._calculate_wacc()

    def _calculate_wacc(self):
        coe = self.rf + self.beta * self.erp
        # Simplified Cost of Debt: rf + 1.5% (spread)
        cod = self.rf + 0.015
        
        # Market Cap proxy if not provided (using book equity + some premium is hard, so we use bridge)
        # In a real app, you'd use actual market cap. For the engine, we use weights.
        equity_weight = 0.8  # Default weight
        debt_weight = 0.2
        return (equity_weight * coe) + (debt_weight * cod * (1 - self.fin.tax_rate))

    def calculate_intrinsic_value(self, stage=2, **kwargs):
        """
        stage 1: Startup, 2: High Growth, 3: Mature, 4: Decline
        """
        if stage == 1:
            return self._startup_valuation(**kwargs)
        elif stage == 2:
            return self._high_growth_valuation(**kwargs)
        elif stage == 3:
            return self._mature_valuation(**kwargs)
        elif stage == 4:
            return self._decline_valuation(**kwargs)
        else:
            return self._high_growth_valuation(**kwargs)

    def _startup_valuation(self, **kwargs):
        """Option 1: Top-Down Startup Model"""
        tam = kwargs.get('tam', self.fin.revenue * 10)
        target_share = kwargs.get('target_share', 0.1)
        target_margin = kwargs.get('target_margin', 0.2)
        prob_failure = kwargs.get('prob_failure', 0.3)
        liquidation_val_pct = kwargs.get('liquidation_val_pct', 0.5)

        # 10년 뒤 매출 및 이익 추정
        rev_yr10 = tam * target_share
        ebit_yr10 = rev_yr10 * target_margin
        
        # 10년 뒤 가치 (영구 성장률 2% 가정)
        g_terminal = min(0.02, self.rf)
        rr_terminal = g_terminal / self.wacc
        fcf_yr11 = ebit_yr10 * (1 - self.fin.tax_rate) * (1 - rr_terminal)
        tv_yr10 = fcf_yr11 / (self.wacc - g_terminal)
        
        # 현재가치 할인 (대략적인 중간 경로 무시하고 TV 중심)
        pv_tv = tv_yr10 / ((1 + self.wacc) ** 10)
        
        # 생존 확률 반영 (다모다란)
        going_concern_val = pv_tv
        liquidation_val = self.fin.cash_st * liquidation_val_pct
        
        ev = (going_concern_val * (1 - prob_failure)) + (liquidation_val * prob_failure)
        return self._equity_bridge(ev)

    def _high_growth_valuation(self, **kwargs):
        """Option 2: 3-Stage Bottom-Up (High -> Transition -> Mature)"""
        # ROIC = NOPAT / Invested Capital
        invested_cap = (self.fin.revenue / 2.0) # Proxy if not available
        nopat = self.fin.ebit * (1 - self.fin.tax_rate)
        roic = nopat / invested_cap if invested_cap > 0 else 0.15
        
        # Reinvestment Rate = (CapEx - D&A + ΔWC) / NOPAT
        reinv_rate = (self.fin.capex - self.fin.depr_amort + self.fin.change_wc) / nopat if nopat > 0 else 0.5
        reinv_rate = max(0.1, min(reinv_rate, 0.9))
        
        # 내재성장률 g = ROIC * Reinvestment Rate
        g_base = roic * reinv_rate
        
        years = list(range(1, 11))
        fcffs = []
        current_rev = self.fin.revenue
        current_ebit = self.fin.ebit
        
        # 1-5년: 고성장기, 6-10년: 이행기 (ROIC fades to WACC)
        pv_fcff = 0
        for t in years:
            if t <= 5:
                g_t = g_base
                roic_t = roic
            else:
                # 6-10년: 선형적으로 영구성장률 및 WACC(ROIC)로 수렴
                ratio = (t - 5) / 5
                g_t = g_base * (1 - ratio) + self.rf * ratio
                roic_t = roic * (1 - ratio) + self.wacc * ratio
            
            current_rev *= (1 + g_t)
            # NOPAT 추정 (마진 유지 가정)
            nopat_t = current_rev * self.fin.ebit_margin * (1 - self.fin.tax_rate)
            # 재투자율 추정 (g / ROIC)
            rr_t = g_t / roic_t if roic_t > 0 else 0.5
            fcf_t = nopat_t * (1 - rr_t)
            
            pv_fcff += fcf_t / ((1 + self.wacc) ** t)
            fcffs.append({'year': t, 'fcf': fcf_t})

        # 11년차 (Terminal)
        g_terminal = min(g_base, self.rf)
        # ROIC = WACC 가정이므로 RR = g / WACC
        rr_terminal = g_terminal / self.wacc
        nopat_t11 = nopat_t * (1 + g_terminal)
        fcf_t11 = nopat_t11 * (1 - rr_terminal)
        tv = fcf_t11 / (self.wacc - g_terminal)
        pv_tv = tv / ((1 + self.wacc) ** 10)
        
        ev = pv_fcff + pv_tv
        return self._equity_bridge(ev, fcffs)

    def _mature_valuation(self, **kwargs):
        """Option 3: Mature Growth (Stable)"""
        g_terminal = min(kwargs.get('g', 0.02), self.rf)
        nopat = self.fin.ebit * (1 - self.fin.tax_rate)
        
        # Mature 기업은 ROIC가 WACC에 근접한다고 가정
        rr = g_terminal / self.wacc
        fcf = nopat * (1 - rr)
        
        ev = fcf / (self.wacc - g_terminal)
        return self._equity_bridge(ev)

    def _decline_valuation(self, **kwargs):
        """Option 4: Decline (Liquidating Cash Flow)"""
        # 매출 성장률 마이너스
        g = kwargs.get('g', -0.05)
        # CapEx < D&A (재투자율 마이너스)
        # 현금이 영업이익보다 더 많이 나올 수 있음
        nopat = self.fin.ebit * (1 - self.fin.tax_rate)
        # 자산 상각을 통한 현금 회수 반영 (RR < 0)
        reinv_rate = -0.2 
        
        # 10년 동안 청산하는 모델
        pv_fcff = 0
        current_nopat = nopat
        for t in range(1, 11):
            current_nopat *= (1 + g)
            fcf_t = current_nopat * (1 - reinv_rate) # (1 - (-0.2)) = 1.2
            pv_fcff += fcf_t / ((1 + self.wacc) ** t)
            
        ev = pv_fcff # 영구가치 없음 (청산)
        return self._equity_bridge(ev)

    def _equity_bridge(self, ev, fcffs=None):
        """
        Equity Value = EV + Cash + ST Invest - Debt - Minority Interest
        """
        equity_value = ev + self.fin.cash_st - self.fin.debt - self.fin.minority_interest
        price_per_share = (equity_value * 1e12) / (self.fin.shares * 1e12) if self.fin.shares > 0 else 0
        
        return {
            'ev': ev,
            'equity_value': equity_value,
            'intrinsic_value': price_per_share,
            'wacc': self.wacc,
            'fcff_history': fcffs
        }
