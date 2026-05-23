import math

class DCFValuator:
    """
    다모다란 2단계 (5년 성장 + 영구성장) DCF 엔진
    """
    def __init__(self, fin_data, rf=0.035, erp=0.075, beta=1.0):
        self.data = fin_data
        self.rf = rf      # 국고채 10년물 등
        self.erp = erp    # Base ERP + CRP
        self.beta = beta

    def calculate_wacc(self):
        # 1. Cost of Equity (CAPM)
        self.coe = self.rf + self.beta * self.erp
        
        # 2. Cost of Debt (Synthetic Rating)
        # 이자보상배율 (EBIT / Interest Exp) 기반이나 여기서는 단순 스프레드 적용 예시
        # 영업이익이 양수일 경우 1.5% 가산금리 적용 (단순화)
        if self.data['ebit'] > 0:
            self.cod = self.rf + 0.015
        else:
            self.cod = self.rf + 0.05
            
        # 3. WACC
        market_equity = self.data['market_cap_won'] if 'market_cap_won' in self.data else self.data['equity']
        total_cap = market_equity + self.data['total_debt']
        
        if total_cap > 0:
            e_weight = market_equity / total_cap
            d_weight = self.data['total_debt'] / total_cap
            self.wacc = (e_weight * self.coe) + (d_weight * self.cod * (1 - self.data['eff_tax_rate']))
        else:
            self.wacc = self.coe
            
        return self.wacc

    def run_valuation(self):
        wacc = self.calculate_wacc()
        
        # 초기가치 설정 (억원 단위 기준)
        ebit = self.data['ebit']
        tax_rate = self.data['eff_tax_rate']
        nopat = ebit * (1 - tax_rate)
        
        # 재투자율 (RR) = (CapEx - D&A + ΔWC) / NOPAT
        # 여기서는 단순화하여 (CapEx / NOPAT) 비율로 추정하거나 고정값 적용
        reinvestment = self.data['capex'] - self.data['depr_amort']
        rr = reinvestment / nopat if nopat > 0 else 0.5
        rr = max(0.1, min(rr, 0.8)) # 가드레일 (10%~80%)

        # ROIC = NOPAT / Invested Capital (여기서는 단순 Equity + Debt)
        invested_cap = self.data['equity'] + self.data['total_debt']
        roic = nopat / invested_cap if invested_cap > 0 else 0.1
        
        # 내재성장률 = ROIC * RR
        g = roic * rr
        g = min(g, 0.15) # 고성장기 상한 15%

        # 1단계: 5년 고성장기 현금흐름 할인
        pv_fcff = 0
        current_fcf = nopat * (1 - rr)
        
        for t in range(1, 6):
            fcf_t = current_fcf * ((1 + g) ** t)
            pv_fcff += fcf_t / ((1 + wacc) ** t)

        # 2단계: 영구성장가치 (Terminal Value)
        # 영구성장률은 국고채 금리(rf)를 초과할 수 없음
        terminal_g = min(g, self.rf)
        
        # 영구 성장기에는 ROIC = WACC로 수렴한다고 가정 (다모다란)
        # 즉, 초과이익이 사라지는 시점 -> 재투자율 = g / WACC
        terminal_rr = terminal_g / wacc
        fcf_n_plus_1 = nopat * ((1 + g) ** 5) * (1 + terminal_g) * (1 - terminal_rr)
        
        tv = fcf_n_plus_1 / (wacc - terminal_g)
        pv_tv = tv / ((1 + wacc) ** 5)

        # 기업가치 (EV)
        operating_value = pv_fcff + pv_tv
        
        # 자기자본가치 (Equity Value) = EV + Cash - Debt - Minority
        equity_value = operating_value + self.data['cash_st'] - self.data['total_debt'] - self.data['minority']
        
        return {
            'wacc': wacc,
            'operating_value': operating_value,
            'equity_value': equity_value,
            'growth_stage1': g,
            'terminal_g': terminal_g
        }
