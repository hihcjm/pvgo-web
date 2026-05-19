from flask import Flask, render_template, request
import pandas as pd
import requests
import FinanceDataReader as fdr
import re
import math
import os

template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')
app = Flask(__name__, template_folder=template_dir)

# --- 1. 기업명 -> 종목코드 변환 ---
def get_stock_code(company_name):
    try:
        df_krx = fdr.StockListing('KRX')
        stock = df_krx[df_krx['Name'] == company_name]
        if not stock.empty:
            return stock.iloc[0]['Code']
        return None
    except:
        return None

# --- 2. 재무 하이라이트 테이블 찾기 ---
def find_highlight_table(tables):
    """
    IMPORTHTML(..., "table", 12) 기준과 동일한 테이블을 반환한다.
    tables[11](0-based) = 연간 5년 + 컨센서스 3년, EPS/ROE 포함 9컬럼 테이블.
    해당 인덱스가 조건을 만족하지 않으면 EPS+ROE 기준으로 fallback 탐색.
    """
    # 1순위: 고정 인덱스 11 (IMPORTHTML table 12와 동일)
    if len(tables) > 11:
        t = tables[11]
        first_col = t.iloc[:, 0].astype(str).str.upper().str.replace(" ", "")
        if first_col.str.contains("EPS").any() and first_col.str.contains("ROE").any():
            return t
    # fallback: EPS+ROE 있는 7컬럼 이상 테이블
    for t in tables:
        if len(t.columns) < 7:
            continue
        first_col = t.iloc[:, 0].astype(str).str.upper().str.replace(" ", "")
        if first_col.str.contains("EPS").any() and first_col.str.contains("ROE").any():
            return t
    return None

def find_row(df, keywords):
    """첫 번째 컬럼에서 키워드가 포함된 행을 반환"""
    for idx in range(len(df)):
        row_name = str(df.iloc[idx, 0]).replace(" ", "").upper()
        for kw in keywords:
            if kw.upper() in row_name:
                return df.iloc[idx]
    return None

def safe_float(val):
    try:
        if pd.isna(val):
            return None
        s = str(val).replace(',', '')
        m = re.search(r'-?\d+\.?\d*', s)
        if m:
            return float(m.group())
        return None
    except:
        return None

def get_avg(row, start_col=1, end_col=5, positive_only=False):
    """지정 열 범위의 숫자 평균 (None 제외).
    positive_only=True 이면 0 이하 값을 제외하고 평균.
    """
    if row is None:
        return None
    vals = [safe_float(row.iloc[i]) for i in range(start_col, min(end_col + 1, len(row)))]
    valid = [v for v in vals if v is not None]
    if positive_only:
        valid = [v for v in valid if v > 0]
    return sum(valid) / len(valid) if valid else None

# --- 3. 가치평가 엔진 ---
def calc_pvgo(base_val, roe, payout_ratio, r):
    """DDM 가치평가
    무성장가치 = EPS / r
    이론주가   = DPS / (r - g)  = EPS * payout / (r - g)
    g          = b * ROE = (1 - payout) * ROE
    """
    if base_val is None or roe is None:
        return {"error": "재무 데이터가 부족하여 계산할 수 없습니다."}
    payout_ratio = max(0.0, min(1.0, payout_ratio))
    if base_val < 0:
        payout_ratio = 0.0
    b = 1 - payout_ratio
    g = b * roe
    if r == 0:
        return {"error": "요구수익률은 0이 될 수 없습니다."}
    no_growth_value = base_val / r          # EPS / r
    if g >= r:
        return {
            "base_val": f"{base_val:,.0f}",
            "roe": f"{roe*100:.1f}",
            "g": f"{g*100:.1f}",
            "no_growth": f"{no_growth_value:,.0f}",
            "price": f"{no_growth_value:,.0f}",  # g≥r → 무성장가치로 대체
            "g_exceeds_r": True,
        }
    growth_value = base_val * payout_ratio / (r - g)   # EPS * payout / (r - g)
    return {
        "base_val": f"{base_val:,.0f}",
        "roe": f"{roe*100:.1f}",
        "g": f"{g*100:.1f}",
        "no_growth": f"{no_growth_value:,.0f}",
        "price": f"{growth_value:,.0f}",
    }

def extract_year_label(col_name):
    """컬럼명에서 연도 레이블 추출. 예: '2024/12' → '2024', '2026E' → '2026E'"""
    s = str(col_name)
    if re.search(r'\dE', s, re.IGNORECASE):
        m = re.search(r'(\d{4})E', s, re.IGNORECASE)
        return m.group(1) + 'E' if m else s
    m = re.search(r'(\d{4})', s)
    return m.group(1) if m else s

def calc_window_valuations(df, r_value, current_price):
    """3컬럼 슬라이딩 윈도우로 연도별 DDM 가치평가 결과 리스트 반환.
    base_val: 해당 연도(end 컬럼) 단일 EPS / ROE·DPS: 3년 평균
    """
    eps_row = find_row(df, ["EPS"])
    roe_row = find_row(df, ["ROE"])
    dps_row = find_row(df, ["DPS"])
    n_cols = len(df.columns)
    results = []

    for start in range(1, n_cols - 1):
        end = start + 2
        if end >= n_cols:
            break

        year_label = extract_year_label(df.columns[end])
        eps = safe_float(eps_row.iloc[end]) if eps_row is not None else None
        eps_pos = get_avg(eps_row, start, end, positive_only=True)
        roe = get_avg(roe_row, start, end)
        dps = get_avg(dps_row, start, end)
        roe_r = (roe / 100) if roe is not None else 0.10

        def _payout(d, e):
            if d is not None and e is not None and e > 0:
                return d / e
            return 0.30

        val = calc_pvgo(eps, roe_r, _payout(dps, eps_pos), r_value)

        if current_price and val.get('price'):
            theory = float(val['price'].replace(',', ''))
            diff = theory - current_price
            diff_pct = diff / current_price * 100
            val['diff'] = f"{diff:+,.0f}"
            val['diff_pct'] = f"{diff_pct:+.1f}"

        results.append({"year": year_label, "result": val})

    return results


def calc_peg(per_val, eps_row, start_col, end_col):
    """PEG = PER / EPS CAGR(%)
    CAGR = (EPS_end / EPS_start)^(1/n) - 1
    음수 EPS 또는 역성장이면 None 반환
    """
    if per_val is None or eps_row is None:
        return None
    eps_start = safe_float(eps_row.iloc[start_col]) if len(eps_row) > start_col else None
    eps_end   = safe_float(eps_row.iloc[end_col])   if len(eps_row) > end_col   else None
    n = end_col - start_col
    if not eps_start or not eps_end or eps_start <= 0 or eps_end <= 0 or n <= 0:
        return None
    cagr = (eps_end / eps_start) ** (1 / n) - 1
    if cagr <= 0:
        return None
    return round(per_val / (cagr * 100), 2)

def calc_peg_flexible(per_val, eps_row, end_col):
    """end_col 기준으로 가능한 가장 긴 양의 CAGR 구간을 찾아 PEG 계산.
    긴 구간을 우선(안정적), fallback으로 짧은 구간까지 허용.
    """
    if per_val is None or eps_row is None or per_val <= 0:
        return None
    # 긴 구간 우선: start를 최대한 앞에서 시작 (col0은 레이블이므로 제외)
    start_min = max(end_col - 5, 1)
    for s in range(start_min, end_col):
        result = calc_peg(per_val, eps_row, s, end_col)
        if result is not None:
            return result
    return None

# --- 3-C. PER/PBR/PEG/EV·Sales 밴드 분석 ---
def calc_valuation_band(df, current_price):
    """
    PER, PBR, PEG, PSR 밴드 분석.
    - 기준: col1~5 (21~25년, 5년) 평균·표준편차
    - 비교: col5(25년), col6(26E) / EV/Sales는 25년만
    - 역산 적정주가: 평균배수 × 해당연도 지표값
    """
    per_row = find_row(df, ["PER"])
    pbr_row = find_row(df, ["PBR"])
    eps_row = find_row(df, ["EPS"])
    bps_row = find_row(df, ["BPS"])

    def make_band_entry(metric, hist_vals, val_25, val_26e,
                        base_25=None, base_26e=None, base_label=None, no_theory=False):
        """공통 밴드 계산 로직."""
        hist_vals = [v for v in hist_vals if v is not None and v > 0]
        if len(hist_vals) < 2:
            return {"metric": metric, "error": "데이터 부족"}

        avg = sum(hist_vals) / len(hist_vals)
        std = math.sqrt(sum((v - avg) ** 2 for v in hist_vals) / len(hist_vals))

        def grade(val):
            if val is None or std == 0:
                return None
            z = (val - avg) / std
            if z < -2: return "극저평가"
            if z < -1: return "저평가"
            if z <  1: return "적정"
            if z <  2: return "고평가"
            return "초고평가"

        def theory(base):
            return avg * base if base is not None and not no_theory else None

        def diff_info(tp):
            if tp is None or not current_price:
                return None, None
            d = tp - current_price
            return f"{d:+,.0f}", f"{d / current_price * 100:+.1f}"

        tp25  = theory(base_25)
        tp26e = theory(base_26e)
        d25,  dp25  = diff_info(tp25)
        d26e, dp26e = diff_info(tp26e)

        bands = {
            "m3s": round(avg - 3 * std, 2),
            "m2s": round(avg - 2 * std, 2),
            "m1s": round(avg - 1 * std, 2),
            "avg": round(avg, 2),
            "p1s": round(avg + 1 * std, 2),
            "p2s": round(avg + 2 * std, 2),
            "p3s": round(avg + 3 * std, 2),
        }

        return {
            "metric": metric,
            "base_label": base_label,
            "hist_avg": round(avg, 2),
            "hist_std": round(std, 2),
            "bands": bands,
            "val_25":  round(val_25,  2) if val_25  is not None else None,
            "val_26e": round(val_26e, 2) if val_26e is not None else None,
            "grade_25":  grade(val_25),
            "grade_26e": grade(val_26e),
            "theory_25":  f"{tp25:,.0f}"  if tp25  else None,
            "theory_26e": f"{tp26e:,.0f}" if tp26e else None,
            "diff_25":    d25,   "diff_pct_25":  dp25,
            "diff_26e":   d26e,  "diff_pct_26e": dp26e,
        }

    results = []

    # --- PER ---
    if per_row is not None:
        hist = [safe_float(per_row.iloc[i]) for i in range(1, min(6, len(per_row)))]
        results.append(make_band_entry(
            "PER", hist,
            val_25  = safe_float(per_row.iloc[5]) if len(per_row) > 5 else None,
            val_26e = safe_float(per_row.iloc[6]) if len(per_row) > 6 else None,
            base_25  = safe_float(eps_row.iloc[5]) if eps_row is not None and len(eps_row) > 5 else None,
            base_26e = safe_float(eps_row.iloc[6]) if eps_row is not None and len(eps_row) > 6 else None,
            base_label="EPS",
        ))
    else:
        results.append({"metric": "PER", "error": "PER 데이터 없음"})

    # --- PBR ---
    if pbr_row is not None:
        hist = [safe_float(pbr_row.iloc[i]) for i in range(1, min(6, len(pbr_row)))]
        results.append(make_band_entry(
            "PBR", hist,
            val_25  = safe_float(pbr_row.iloc[5]) if len(pbr_row) > 5 else None,
            val_26e = safe_float(pbr_row.iloc[6]) if len(pbr_row) > 6 else None,
            base_25  = safe_float(bps_row.iloc[5]) if bps_row is not None and len(bps_row) > 5 else None,
            base_26e = safe_float(bps_row.iloc[6]) if bps_row is not None and len(bps_row) > 6 else None,
            base_label="BPS",
        ))
    else:
        results.append({"metric": "PBR", "error": "PBR 데이터 없음"})

    # --- PEG (직접 계산: PER / EPS CAGR) ---
    if per_row is not None and eps_row is not None:
        # hist PEG: col1~5 각 연도별 PER에 대해 해당 연도까지 가능한 최장 양의 CAGR 적용
        hist_peg = []
        for ci in range(1, min(6, len(per_row))):
            pv = safe_float(per_row.iloc[ci])
            if pv and pv > 0:
                peg_val = calc_peg_flexible(pv, eps_row, ci)
                if peg_val is not None:
                    hist_peg.append(peg_val)

        # 25년 PEG: 가장 긴 양의 CAGR 구간 (col1→col5 우선, fallback 단기)
        per_25 = safe_float(per_row.iloc[5]) if len(per_row) > 5 else None
        peg_25 = calc_peg_flexible(per_25, eps_row, 5)

        # 26E PEG: col5→col7 또는 col5→col6
        peg_26e = None
        per_26e = safe_float(per_row.iloc[6]) if len(per_row) > 6 else None
        for s, e in ((5, 7), (5, 6)):
            if len(eps_row) > e:
                peg_26e = calc_peg(per_26e, eps_row, s, e)
                if peg_26e is not None:
                    break
        if peg_26e is None:
            peg_26e = calc_peg_flexible(per_26e, eps_row, 6) if len(eps_row) > 6 else None

        results.append(make_band_entry(
            "PEG", hist_peg,
            val_25=peg_25, val_26e=peg_26e,
            no_theory=True,   # PEG는 역산주가 없음
        ))
    else:
        results.append({"metric": "PEG", "error": "PER/EPS 데이터 부족"})

    # --- PSR = 주가 / SPS, SPS = 매출액(억원) / 발행주식수(천주) × 1e5 ---
    rev_row   = find_row(df, ["매출액"])
    share_row = find_row(df, ["발행주식수"])
    if rev_row is not None and share_row is not None:
        def sps(rev_col, share_col):
            """SPS(원) = 매출액(억) / 발행주식수(천주) × 1e5"""
            rev    = safe_float(rev_row.iloc[rev_col])     if len(rev_row)   > rev_col   else None
            shares = safe_float(share_row.iloc[share_col]) if len(share_row) > share_col else None
            if not shares:  # 컨센서스 연도는 주식수 NaN → 직전 연도(col5) 사용
                shares = safe_float(share_row.iloc[5]) if len(share_row) > 5 else None
            if rev and shares and shares > 0:
                return round(rev / shares * 1e5, 0)
            return None

        def psr(price, sps_val):
            if price and sps_val and sps_val > 0:
                return round(price / sps_val, 2)
            return None

        # 과거 5년 PSR: 각 연도 주가 = 해당 연도 PER × EPS (연말 기준 주가)
        hist_psr = []
        for ci in range(1, min(6, len(per_row) if per_row is not None else 0)):
            sps_i = sps(ci, ci)
            per_i = safe_float(per_row.iloc[ci]) if per_row is not None and len(per_row) > ci else None
            eps_i = safe_float(eps_row.iloc[ci]) if eps_row is not None and len(eps_row) > ci else None
            if per_i and eps_i and sps_i:
                price_i = per_i * eps_i  # 해당 연도 연말 주가 추정
                hist_psr.append(round(price_i / sps_i, 2))

        sps_25  = sps(5, 5)
        sps_26e = sps(6, 6)
        psr_25  = psr(current_price, sps_25)
        psr_26e = psr(current_price, sps_26e)

        results.append(make_band_entry(
            "PSR", hist_psr,
            val_25   = psr_25,
            val_26e  = psr_26e,
            base_25  = sps_25,
            base_26e = sps_26e,
            base_label="SPS",
        ))
    else:
        results.append({"metric": "PSR", "error": "매출액/발행주식수 데이터 없음"})

    return results

# --- 3-C. 현재주가 조회 (장 미개장 시 전일 종가 사용) ---
def get_current_price(stock_code):
    try:
        # 최근 7일 조회 → 가장 최근 거래일 종가 반환
        start = pd.Timestamp.now().date() - pd.Timedelta(days=7)
        df = fdr.DataReader(stock_code, start)
        if not df.empty:
            return float(df['Close'].iloc[-1])
    except:
        pass
    return None

# --- 3-D. 무위험률 (미국채 10년물) 크롤링 ---
def get_risk_free_rate():
    # ^TNX: Yahoo Finance 미국채 10년물 (단위: % → 소수 변환)
    try:
        df = fdr.DataReader('^TNX', pd.Timestamp.now().date() - pd.Timedelta(days=5))
        if not df.empty:
            val = float(df['Close'].iloc[-1])
            if 1.0 < val < 20.0:   # 퍼센트 단위인지 sanity check
                return val / 100
    except:
        pass
    return 0.044  # fallback: 4.4%

# --- 3-E. 베타 크롤링 (fnguide SVD_Main tables 재사용) ---
def get_beta(tables):
    try:
        for t in tables:
            row = find_row(t, ["베타", "Beta"])
            if row is not None:
                val = safe_float(row.iloc[1])
                if val is not None:
                    return val
    except:
        pass
    return 1.0  # fallback

# --- 3-F. SVD_Invest 크롤링 (FCFF 구성요소: 세후영업이익, 상각비, 총투자) ---
def get_fcff_components(stock_code, headers):
    """SVD_Invest.asp Table1에서 세후영업이익, 유무형자산상각비, 총투자 행 반환.
    col1~5 = 21~25년
    """
    try:
        url = (
            f"http://comp.fnguide.com/SVO2/ASP/SVD_Invest.asp"
            f"?pGB=1&gicode=A{stock_code}&cID=&MenuYn=Y&ReportGB=&NewMenuID=13&stkGb=701"
        )
        resp = requests.get(url, headers=headers, timeout=15)
        tables = pd.read_html(resp.text)
        for t in tables:
            t.columns = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in t.columns]
            nopat_row = find_row(t, ["세후영업이익"])
            da_row    = find_row(t, ["유무형자산상각비"])
            capex_row = find_row(t, ["총투자"])
            if nopat_row is not None:
                return nopat_row, da_row, capex_row
    except Exception:
        pass
    return None, None, None


# --- 3-H. DCF 가치평가 (영업이익 기반 FCFF) ---
def calc_dcf(df, stock_code, headers, r, current_price, g_terminal=0.025):
    """
    FCFF = 세후영업이익(NOPAT) + 상각비(D&A) - CAPEX(총투자)
    과거 21~25년 FCFF/매출액 마진 평균으로 컨센서스 26~28E FCFF 추정
    TV = FCFF_28E × (1+g) / (WACC - g)
    적정주가 = PV합계 / 발행주식수
    """
    try:
        rev_row   = find_row(df, ["매출액"])
        op_row    = find_row(df, ["영업이익"])
        share_row = find_row(df, ["발행주식수"])
        if rev_row is None or share_row is None:
            return {"error": "매출액/발행주식수 데이터 없음"}

        nopat_row, da_row, capex_row = get_fcff_components(stock_code, headers)
        if nopat_row is None:
            return {"error": "FCFF 구성요소 데이터 없음 (SVD_Invest)"}

        # --- 세율: SVD_Finance 손익계산서에서 법인세/세전이익 (22~25년) ---
        tax_rates = []
        try:
            fin_url = (f"http://comp.fnguide.com/SVO2/ASP/SVD_Finance.asp"
                       f"?pGB=1&gicode=A{stock_code}&cID=&MenuYn=Y&ReportGB=&NewMenuID=12&stkGb=701")
            fin_resp = requests.get(fin_url, headers=headers, timeout=15)
            fin_tables = pd.read_html(fin_resp.text)
            for ft in fin_tables:
                pretax_row = find_row(ft, ["세전계속사업이익"])
                tax_row    = find_row(ft, ["법인세비용"])
                if pretax_row is not None and tax_row is not None and len(pretax_row) >= 5:
                    for ci in range(1, min(5, len(pretax_row))):
                        pretax = safe_float(pretax_row.iloc[ci])
                        tax    = safe_float(tax_row.iloc[ci])
                        if pretax and tax and pretax > 0 and tax > 0:
                            tax_rates.append(tax / pretax)
                    break
        except Exception:
            pass
        avg_tax_rate = sum(tax_rates) / len(tax_rates) if tax_rates else 0.22

        # --- 과거 FCFF 마진 (21~25년, SVD_Invest col1~5 / SVD_Main col1~5) ---
        # FCFF = 세후영업이익(NOPAT) + 상각비(D&A) - 총투자(CAPEX)
        hist_da_margin    = []
        hist_capex_margin = []
        hist_fcff_margin  = []
        for invest_col, main_col in [(1,1),(2,2),(3,3),(4,4),(5,5)]:
            nopat = safe_float(nopat_row.iloc[invest_col]) if len(nopat_row) > invest_col else None
            da    = safe_float(da_row.iloc[invest_col])    if da_row    is not None and len(da_row)    > invest_col else None
            capex = safe_float(capex_row.iloc[invest_col]) if capex_row is not None and len(capex_row) > invest_col else None
            rev   = safe_float(rev_row.iloc[main_col])     if len(rev_row) > main_col else None
            if nopat and rev and rev > 0:
                fcff = (nopat or 0) + (da or 0) - (capex or 0)
                hist_fcff_margin.append(fcff / rev)
                if da:    hist_da_margin.append(da / rev)
                if capex: hist_capex_margin.append(capex / rev)

        if len(hist_fcff_margin) < 2:
            return {"error": "FCFF 과거 데이터 부족"}

        avg_da_margin    = sum(hist_da_margin)    / len(hist_da_margin)    if hist_da_margin    else 0
        avg_capex_margin = sum(hist_capex_margin) / len(hist_capex_margin) if hist_capex_margin else 0
        avg_fcff_margin  = sum(hist_fcff_margin)  / len(hist_fcff_margin)

        # --- 컨센서스 FCFF 추정 (26E~28E): 컨센서스 영업이익 × (1-세율) + D&A - CAPEX ---
        fcf_years = []
        for col, label in zip([6, 7, 8], ["26E", "27E", "28E"]):
            rev_e = safe_float(rev_row.iloc[col]) if len(rev_row) > col else None
            op_e  = safe_float(op_row.iloc[col])  if op_row is not None and len(op_row) > col else None
            if rev_e and op_e and op_e > 0:
                nopat_e = op_e * (1 - avg_tax_rate)
                da_e    = rev_e * avg_da_margin
                capex_e = rev_e * avg_capex_margin
                fcff_e  = nopat_e + da_e - capex_e
                fcf_years.append((label, fcff_e))
            elif rev_e:
                fcf_years.append((label, rev_e * avg_fcff_margin))

        if not fcf_years:
            return {"error": "컨센서스 매출액 데이터 없음"}

        # --- 현재가치 할인 ---
        if r <= g_terminal:
            return {"error": f"할인율({r*100:.1f}%)이 터미널성장률({g_terminal*100:.1f}%)보다 낮음"}

        # --- 발행주식수 ---
        shares = safe_float(share_row.iloc[5]) if len(share_row) > 5 else None  # 천주
        if not shares or shares <= 0:
            return {"error": "발행주식수 없음"}

        def pv_to_price(pv_total):
            """PV 합계(억원) → 주당 가치(원)"""
            return pv_total * 1e8 / (shares * 1e3)

        def diff_str(fv):
            if not current_price: return None, None
            d = fv - current_price
            return f"{d:+,.0f}", f"{d / current_price * 100:+.1f}"

        # --- 연도별 FCF PV 누적 및 연도별 적정주가 계산 ---
        # 연도별 적정주가 = Σ PV(FCF_i, i=1..n) + PV(TV_n)
        # TV_n = FCF_n × (1+g) / (r-g), 할인은 n기
        pv_fcfs = []
        cumulative_pv = 0
        for t_idx, (label, fcf_e) in enumerate(fcf_years):
            n = t_idx + 1
            pv = fcf_e / (1 + r) ** n
            cumulative_pv += pv

            # 이 연도 기준 터미널 밸류
            tv_n   = fcf_e * (1 + g_terminal) / (r - g_terminal)
            pv_tv_n = tv_n / (1 + r) ** n
            total_pv_n = cumulative_pv + pv_tv_n
            fv_n = pv_to_price(total_pv_n)
            d, dp = diff_str(fv_n)

            pv_fcfs.append({
                "year":       label,
                "fcf":        round(fcf_e),
                "pv":         round(pv),
                "pv_tv":      round(pv_tv_n),
                "total_pv":   round(total_pv_n),
                "fair_value": f"{fv_n:,.0f}",
                "diff":       d,
                "diff_pct":   dp,
            })

        return {
            "avg_fcff_margin": round(avg_fcff_margin * 100, 1),
            "avg_tax_rate":    round(avg_tax_rate * 100, 1),
            "g_terminal":      round(g_terminal * 100, 1),
            "r":               round(r * 100, 2),
            "pv_fcfs":         pv_fcfs,
        }
    except Exception as e:
        return {"error": f"DCF 계산 오류: {e}"}


# --- 4. 메인 분석 함수 ---
def analyze_stock(company_name):
    stock_code = get_stock_code(company_name)
    if not stock_code:
        return {"error": f"'{company_name}'(을)를 찾을 수 없습니다."}

    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = (
            f"http://comp.fnguide.com/SVO2/ASP/SVD_Main.asp"
            f"?pGB=1&gicode=A{stock_code}&cID=&MenuYn=Y&ReportGB=&NewMenuID=11&stkGb=701"
        )
        response = requests.get(url, headers=headers, timeout=15)
        tables = pd.read_html(response.text)

        df = find_highlight_table(tables)
        if df is None:
            return {"error": "재무 데이터 테이블을 찾을 수 없습니다."}

        # 멀티레벨 헤더를 단일 레벨로 평탄화
        df.columns = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in df.columns]
        raw_table_html = df.to_html(classes='financial-table', index=False)

        # --- CAPM r 자동계산 ---
        rf = get_risk_free_rate()
        beta = get_beta(tables)
        erp = 0.05
        r_value = rf + beta * erp

        # --- 현재주가 조회 ---
        current_price = get_current_price(stock_code)

        return {
            "name": company_name,
            "code": stock_code,
            "raw_table": raw_table_html,
            "current_price": f"{current_price:,.0f}" if current_price else "조회 실패",
            "r_info": {
                "rf": f"{rf*100:.2f}",
                "beta": f"{beta:.2f}",
                "r": f"{r_value*100:.2f}",
            },
            "dcf": calc_dcf(df, stock_code, headers, r_value, current_price),
            "band": calc_valuation_band(df, current_price),
        }

    except Exception as e:
        return {"error": f"서버 처리 중 오류 발생: {e}"}


@app.route('/', methods=['GET', 'POST'])
def index():
    result = None
    company_name = ""

    if request.method == 'POST':
        company_name = request.form['company_name']
        result = analyze_stock(company_name)

    return render_template('index.html', result=result, company_name=company_name)


if __name__ == '__main__':
    app.run(debug=True)
