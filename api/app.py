from flask import Flask, render_template, request
import pandas as pd
import requests
import FinanceDataReader as fdr
import re
import math
import os
import json

template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')
app = Flask(__name__, template_folder=template_dir)

NAVER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://finance.naver.com',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}

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

# --- 2. 네이버 증권 모바일 API로 연간 재무 데이터 가져오기 ---
def get_naver_finance(stock_code):
    """
    m.stock.naver.com API에서 연간 재무 + 컨센서스 데이터를 가져옴.
    반환: {
      'years': ['2022', '2023', '2024', '2025', '2026E'],
      'rows': {'매출액': [v1,v2,...], 'EPS': [...], ...}
    }
    """
    url = f"https://m.stock.naver.com/api/stock/{stock_code}/finance/annual"
    resp = requests.get(url, headers=NAVER_HEADERS, timeout=15)
    if resp.status_code != 200:
        return None

    data = resp.json()
    finance_info = data.get('financeInfo', {})
    title_list = finance_info.get('trTitleList', [])   # 연도 컬럼
    row_list   = finance_info.get('rowList', [])        # 지표 행

    if not title_list or not row_list:
        return None

    # 연도 키 순서 정렬 (202112 → 202212 → ...)
    years_sorted = sorted(title_list, key=lambda x: x['key'])
    year_keys    = [y['key'] for y in years_sorted]
    year_labels  = []
    for y in years_sorted:
        label = y['title'].replace('.', '/').rstrip('/')  # '2026.12.' → '2026/12'
        if y.get('isConsensus') == 'Y':
            label += '(E)'
        year_labels.append(label)

    # 행 이름 한글 → 내부 키 매핑
    NAME_MAP = {
        '매출액':   '매출액',
        '영업이익': '영업이익',
        '당기순이익': '당기순이익',
        'ROE':      'ROE',
        'EPS':      'EPS',
        'PER':      'PER',
        'BPS':      'BPS',
        'PBR':      'PBR',
        'DPS':      'DPS',
        '배당수익률': '배당수익률',
        '영업이익률': '영업이익률',
        '순이익률':  '순이익률',
    }

    rows = {}
    for row in row_list:
        title = row.get('title', '').strip()
        key = NAME_MAP.get(title, title)
        cols = row.get('columns', {})
        vals = []
        for yk in year_keys:
            cell = cols.get(yk, {})
            v = cell.get('value', '-') if cell else '-'
            vals.append(v)
        rows[key] = vals

    return {'years': year_labels, 'year_keys': year_keys, 'rows': rows}

# --- 3. 네이버 PC 메인에서 베타 가져오기 ---
def get_beta_naver(stock_code):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
        resp = requests.get(url, headers=NAVER_HEADERS, timeout=15)
        tables = pd.read_html(resp.content, encoding='euc-kr')
        # 테이블 5번(업종비교)에서 베타 행 찾기
        for t in tables:
            for col in t.columns:
                vals = t[col].astype(str)
                for i, v in enumerate(vals):
                    if '베타' in v or 'Beta' in v:
                        # 같은 행의 다음 컬럼에서 숫자 추출
                        row = t.iloc[i]
                        for cell in row:
                            m = re.search(r'\d+\.\d+', str(cell))
                            if m:
                                beta = float(m.group())
                                if 0.1 < beta < 5.0:
                                    return beta
    except:
        pass
    return 1.0  # fallback

# --- 4. 현재주가 조회 ---
def get_current_price(stock_code):
    try:
        start = pd.Timestamp.now().date() - pd.Timedelta(days=7)
        df = fdr.DataReader(stock_code, start)
        if not df.empty:
            return float(df['Close'].iloc[-1])
    except:
        pass
    return None

# --- 5. 무위험률 (미국채 10년물) ---
def get_risk_free_rate():
    try:
        df = fdr.DataReader('^TNX', pd.Timestamp.now().date() - pd.Timedelta(days=5))
        if not df.empty:
            val = float(df['Close'].iloc[-1])
            if 1.0 < val < 20.0:
                return val / 100
    except:
        pass
    return 0.044  # fallback: 4.4%

# --- 6. 유틸 ---
def safe_float(val):
    try:
        if val is None or str(val).strip() in ('-', '', 'nan', 'None'):
            return None
        s = str(val).replace(',', '').replace('%', '').strip()
        m = re.search(r'-?\d+\.?\d*', s)
        return float(m.group()) if m else None
    except:
        return None

def avg_of(vals, positive_only=False):
    nums = [safe_float(v) for v in vals]
    nums = [v for v in nums if v is not None]
    if positive_only:
        nums = [v for v in nums if v > 0]
    return sum(nums) / len(nums) if nums else None

# --- 7. 밴드 분석 ---
def calc_valuation_band(naver_data, current_price):
    rows  = naver_data['rows']
    years = naver_data['years']
    n     = len(years)

    # 컨센서스 여부로 hist/estimate 구분
    hist_idx = [i for i, y in enumerate(years) if '(E)' not in y]
    est_idx  = [i for i, y in enumerate(years) if '(E)' in y]

    # 25년(마지막 확정), 26E(첫 번째 컨센서스)
    idx_25  = hist_idx[-1] if hist_idx else None
    idx_26e = est_idx[0]   if est_idx  else None

    def get_vals(key, idxs):
        row = rows.get(key, [])
        return [safe_float(row[i]) for i in idxs if i < len(row)]

    def make_band(metric, hist_vals, val_25, val_26e,
                  base_25=None, base_26e=None, base_label=None, no_theory=False):
        hist_vals = [v for v in hist_vals if v is not None and v > 0]
        if len(hist_vals) < 2:
            return {"metric": metric, "error": "데이터 부족"}

        avg = sum(hist_vals) / len(hist_vals)
        std = math.sqrt(sum((v - avg) ** 2 for v in hist_vals) / len(hist_vals))

        def grade(val):
            if val is None or std == 0: return None
            z = (val - avg) / std
            if z < -2: return "극저평가"
            if z < -1: return "저평가"
            if z <  1: return "적정"
            if z <  2: return "고평가"
            return "초고평가"

        def theory(base):
            return avg * base if base is not None and not no_theory else None

        def diff_info(tp):
            if tp is None or not current_price: return None, None
            d = tp - current_price
            return f"{d:+,.0f}", f"{d / current_price * 100:+.1f}"

        tp25  = theory(base_25)
        tp26e = theory(base_26e)
        d25,  dp25  = diff_info(tp25)
        d26e, dp26e = diff_info(tp26e)

        bands = {k: round(avg + s * std, 2)
                 for k, s in [('m3s',-3),('m2s',-2),('m1s',-1),('avg',0),('p1s',1),('p2s',2),('p3s',3)]}

        return {
            "metric": metric, "base_label": base_label,
            "hist_avg": round(avg, 2), "hist_std": round(std, 2),
            "bands": bands,
            "val_25":  round(val_25,  2) if val_25  is not None else None,
            "val_26e": round(val_26e, 2) if val_26e is not None else None,
            "grade_25":  grade(val_25),  "grade_26e": grade(val_26e),
            "theory_25":  f"{tp25:,.0f}"  if tp25  else None,
            "theory_26e": f"{tp26e:,.0f}" if tp26e else None,
            "diff_25": d25, "diff_pct_25": dp25,
            "diff_26e": d26e, "diff_pct_26e": dp26e,
        }

    results = []
    per_hist = get_vals('PER', hist_idx)
    eps_row  = rows.get('EPS', [])
    bps_row  = rows.get('BPS', [])
    pbr_hist = get_vals('PBR', hist_idx)

    per_25  = safe_float(rows.get('PER',  [])[idx_25])  if idx_25  is not None and idx_25  < len(rows.get('PER', [])) else None
    per_26e = safe_float(rows.get('PER',  [])[idx_26e]) if idx_26e is not None and idx_26e < len(rows.get('PER', [])) else None
    eps_25  = safe_float(eps_row[idx_25])  if idx_25  is not None and idx_25  < len(eps_row) else None
    eps_26e = safe_float(eps_row[idx_26e]) if idx_26e is not None and idx_26e < len(eps_row) else None
    bps_25  = safe_float(bps_row[idx_25])  if idx_25  is not None and idx_25  < len(bps_row) else None
    bps_26e = safe_float(bps_row[idx_26e]) if idx_26e is not None and idx_26e < len(bps_row) else None
    pbr_25  = safe_float(rows.get('PBR',  [])[idx_25])  if idx_25  is not None and idx_25  < len(rows.get('PBR', [])) else None
    pbr_26e = safe_float(rows.get('PBR',  [])[idx_26e]) if idx_26e is not None and idx_26e < len(rows.get('PBR', [])) else None

    # PER 밴드
    results.append(make_band("PER", per_hist, per_25, per_26e,
                              base_25=eps_25, base_26e=eps_26e, base_label="EPS"))
    # PBR 밴드
    results.append(make_band("PBR", pbr_hist, pbr_25, pbr_26e,
                              base_25=bps_25, base_26e=bps_26e, base_label="BPS"))

    # PEG 밴드
    def calc_peg(per_val, eps_vals, end_i):
        if per_val is None or per_val <= 0: return None
        for start_i in range(max(0, end_i - 5), end_i):
            es = safe_float(eps_vals[start_i]) if start_i < len(eps_vals) else None
            ee = safe_float(eps_vals[end_i])   if end_i   < len(eps_vals) else None
            n  = end_i - start_i
            if es and ee and es > 0 and ee > 0 and n > 0:
                cagr = (ee / es) ** (1 / n) - 1
                if cagr > 0:
                    return round(per_val / (cagr * 100), 2)
        return None

    hist_peg = [calc_peg(safe_float(rows.get('PER',[])[i]), eps_row, i)
                for i in hist_idx if i < len(rows.get('PER',[]))]
    hist_peg = [v for v in hist_peg if v is not None]
    peg_25   = calc_peg(per_25,  eps_row, idx_25)  if idx_25  is not None else None
    peg_26e  = calc_peg(per_26e, eps_row, idx_26e) if idx_26e is not None else None
    results.append(make_band("PEG", hist_peg, peg_25, peg_26e, no_theory=True))

    # PSR 밴드
    rev_row   = rows.get('매출액', [])
    shares_fixed = 5919638  # 천주 (고정값, 크롤링 불가시 fallback)
    # 발행주식수는 API에 없으므로 현재주가·PER·EPS 역산으로 대체
    def calc_sps(rev_val, shares_k):
        if rev_val and shares_k and shares_k > 0:
            return round(rev_val / shares_k * 1e5, 0)
        return None

    # 역산 shares: PER × EPS = 주가 → shares는 별도 조회 필요, PSR은 생략하고 현재주가 기준만
    hist_psr = []
    for i in hist_idx:
        per_i = safe_float(rows.get('PER', [])[i]) if i < len(rows.get('PER', [])) else None
        eps_i = safe_float(eps_row[i]) if i < len(eps_row) else None
        rev_i = safe_float(rev_row[i]) if i < len(rev_row) else None
        if per_i and eps_i and rev_i and rev_i > 0:
            price_i = per_i * eps_i
            sps_i = calc_sps(rev_i, shares_fixed)
            if sps_i and sps_i > 0:
                hist_psr.append(round(price_i / sps_i, 2))

    rev_25  = safe_float(rev_row[idx_25])  if idx_25  is not None and idx_25  < len(rev_row) else None
    rev_26e = safe_float(rev_row[idx_26e]) if idx_26e is not None and idx_26e < len(rev_row) else None
    sps_25  = calc_sps(rev_25,  shares_fixed)
    sps_26e = calc_sps(rev_26e, shares_fixed)
    psr_25  = round(current_price / sps_25,  2) if current_price and sps_25  else None
    psr_26e = round(current_price / sps_26e, 2) if current_price and sps_26e else None
    results.append(make_band("PSR", hist_psr, psr_25, psr_26e,
                              base_25=sps_25, base_26e=sps_26e, base_label="SPS"))

    return results

# --- 8. DCF 가치평가 (영업이익 기반 간이 FCFF) ---
def calc_dcf(naver_data, r, current_price, g_terminal=0.025):
    """
    영업이익 × (1-세율) = NOPAT → FCFF 마진 과거 평균으로 컨센서스 추정
    """
    try:
        rows  = naver_data['rows']
        years = naver_data['years']

        hist_idx = [i for i, y in enumerate(years) if '(E)' not in y]
        est_idx  = [i for i, y in enumerate(years) if '(E)' in y]

        rev_row = rows.get('매출액', [])
        op_row  = rows.get('영업이익', [])

        if not rev_row or not op_row:
            return {"error": "매출액/영업이익 데이터 없음"}

        # 과거 영업이익률(FCFF 마진 proxy, 세율 22% 가정)
        TAX_RATE = 0.22
        DA_RATIO  = 0.05   # D&A: 매출액의 5% 가정
        CAPEX_RATIO = 0.06  # CAPEX: 매출액의 6% 가정

        hist_fcff_margin = []
        for i in hist_idx:
            rev = safe_float(rev_row[i]) if i < len(rev_row) else None
            op  = safe_float(op_row[i])  if i < len(op_row)  else None
            if rev and op and rev > 0 and op > 0:
                nopat = op * (1 - TAX_RATE)
                fcff  = nopat + rev * DA_RATIO - rev * CAPEX_RATIO
                hist_fcff_margin.append(fcff / rev)

        if len(hist_fcff_margin) < 2:
            return {"error": "과거 FCFF 데이터 부족"}

        avg_fcff_margin = sum(hist_fcff_margin) / len(hist_fcff_margin)

        # 컨센서스 연도별 FCFF 추정
        fcf_years = []
        for i in est_idx:
            label = years[i]
            rev_e = safe_float(rev_row[i]) if i < len(rev_row) else None
            op_e  = safe_float(op_row[i])  if i < len(op_row)  else None
            if rev_e and op_e and op_e > 0:
                nopat_e = op_e * (1 - TAX_RATE)
                da_e    = rev_e * DA_RATIO
                capex_e = rev_e * CAPEX_RATIO
                fcff_e  = nopat_e + da_e - capex_e
            elif rev_e:
                fcff_e = rev_e * avg_fcff_margin
            else:
                continue
            fcf_years.append((label, fcff_e))

        if not fcf_years:
            return {"error": "컨센서스 매출액 데이터 없음"}

        if r <= g_terminal:
            return {"error": f"할인율({r*100:.1f}%)이 터미널성장률({g_terminal*100:.1f}%)보다 낮음"}

        # 발행주식수: FinanceDataReader로 시가총액/주가 역산 또는 고정값
        shares = 5919638  # 천주 (삼성전자 기준 fallback — 아래에서 동적으로 계산)
        try:
            start = pd.Timestamp.now().date() - pd.Timedelta(days=7)
            price_df = fdr.DataReader(naver_data.get('stock_code',''), start)
            # 발행주식수는 별도 조회 불가 → 네이버 API에도 없음 → 고정 사용
        except:
            pass

        # 발행주식수를 네이버 PC에서 가져오기 시도
        try:
            pc_url = f"https://finance.naver.com/item/main.naver?code={naver_data.get('stock_code','')}"
            pc_resp = requests.get(pc_url, headers=NAVER_HEADERS, timeout=10)
            pc_tables = pd.read_html(pc_resp.content, encoding='euc-kr')
            for t in pc_tables:
                for col in t.columns:
                    for i, v in enumerate(t[col].astype(str)):
                        if '발행주식수' in v or '상장주식수' in v:
                            row_vals = t.iloc[i].astype(str)
                            for cell in row_vals:
                                m = re.search(r'[\d,]{7,}', cell.replace(' ', ''))
                                if m:
                                    s = safe_float(m.group())
                                    if s and s > 1e6:
                                        shares = s / 1000  # 주 → 천주
                                        break
        except:
            pass

        def pv_to_price(pv_total):
            return pv_total * 1e8 / (shares * 1e3)

        def diff_str(fv):
            if not current_price: return None, None
            d = fv - current_price
            return f"{d:+,.0f}", f"{d / current_price * 100:+.1f}"

        pv_fcfs = []
        cumulative_pv = 0
        for t_idx, (label, fcf_e) in enumerate(fcf_years):
            n = t_idx + 1
            pv = fcf_e / (1 + r) ** n
            cumulative_pv += pv
            tv_n    = fcf_e * (1 + g_terminal) / (r - g_terminal)
            pv_tv_n = tv_n / (1 + r) ** n
            total_pv_n = cumulative_pv + pv_tv_n
            fv_n = pv_to_price(total_pv_n)
            d, dp = diff_str(fv_n)
            pv_fcfs.append({
                "year": label, "fcf": round(fcf_e), "pv": round(pv),
                "pv_tv": round(pv_tv_n), "total_pv": round(total_pv_n),
                "fair_value": f"{fv_n:,.0f}", "diff": d, "diff_pct": dp,
            })

        return {
            "avg_fcff_margin": round(avg_fcff_margin * 100, 1),
            "avg_tax_rate":    round(TAX_RATE * 100, 1),
            "g_terminal":      round(g_terminal * 100, 1),
            "r":               round(r * 100, 2),
            "pv_fcfs":         pv_fcfs,
        }
    except Exception as e:
        return {"error": f"DCF 계산 오류: {e}"}

# --- 9. 재무 테이블 HTML 생성 ---
def build_raw_table_html(naver_data):
    rows  = naver_data['rows']
    years = naver_data['years']

    # 표시할 행 순서
    DISPLAY_ROWS = [
        '매출액', '영업이익', '당기순이익',
        '영업이익률', '순이익률', 'ROE',
        'EPS', 'BPS', 'DPS', 'PER', 'PBR', '배당수익률',
    ]

    header = '<thead><tr><th>항목</th>' + ''.join(f'<th>{y}</th>' for y in years) + '</tr></thead>'
    body = '<tbody>'
    for key in DISPLAY_ROWS:
        if key not in rows:
            continue
        vals = rows[key]
        cells = ''.join(f'<td>{v if v else "-"}</td>' for v in vals)
        body += f'<tr><td>{key}</td>{cells}</tr>'
    body += '</tbody>'

    return f'<table class="financial-table">{header}{body}</table>'

# --- 10. 메인 분석 함수 ---
def analyze_stock(company_name):
    stock_code = get_stock_code(company_name)
    if not stock_code:
        return {"error": f"'{company_name}'(을)를 찾을 수 없습니다."}

    try:
        # 네이버 증권 API로 재무 데이터 수집
        naver_data = get_naver_finance(stock_code)
        if naver_data is None:
            return {"error": "네이버 증권에서 재무 데이터를 가져오지 못했습니다."}

        naver_data['stock_code'] = stock_code  # DCF에서 사용

        # 현재주가
        current_price = get_current_price(stock_code)

        # CAPM r 계산
        rf   = get_risk_free_rate()
        beta = get_beta_naver(stock_code)
        erp  = 0.05
        r_value = rf + beta * erp

        # 재무 테이블 HTML
        raw_table_html = build_raw_table_html(naver_data)

        return {
            "name":          company_name,
            "code":          stock_code,
            "raw_table":     raw_table_html,
            "current_price": f"{current_price:,.0f}" if current_price else "조회 실패",
            "r_info": {
                "rf":   f"{rf*100:.2f}",
                "beta": f"{beta:.2f}",
                "r":    f"{r_value*100:.2f}",
            },
            "dcf":  calc_dcf(naver_data, r_value, current_price),
            "band": calc_valuation_band(naver_data, current_price),
        }

    except Exception as e:
        err_msg = str(e)
        if len(err_msg) > 200 or '<html' in err_msg.lower():
            err_msg = "데이터 수집 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        return {"error": f"서버 처리 중 오류 발생: {err_msg}"}


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
