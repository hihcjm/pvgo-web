from flask import Flask, render_template, request
import pandas as pd
import requests
import FinanceDataReader as fdr
import re
import math
import os
import io

template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')
app = Flask(__name__, template_folder=template_dir)

NAVER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://finance.naver.com',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}

# ── 컬럼명 정규화 ──────────────────────────────────────────────
# 네이버 PC HTML 컬럼 헤더가 멀티레벨 튜플로 오므로 마지막 레벨만 추출
def flatten_col(c):
    if isinstance(c, tuple):
        return str(c[-1])
    return str(c)

# 연도 레이블 정규화: '2026/12(E)' 형태 유지, 확정은 '2021/12' 형태
def norm_year(s):
    s = str(s).strip()
    # 컨센서스 표시
    is_est = '(E)' in s or 'E)' in s
    # 연도 4자리 추출
    m = re.search(r'(\d{4})', s)
    year = m.group(1) if m else s
    # 월 추출
    mm = re.search(r'/(\d{2})', s)
    month = mm.group(1) if mm else '12'
    label = f"{year}/{month}"
    if is_est:
        label += '(E)'
    return label

# ── 1. 종목코드 변환 ───────────────────────────────────────────
def get_stock_code(company_name):
    try:
        df_krx = fdr.StockListing('KRX')
        stock = df_krx[df_krx['Name'] == company_name]
        if not stock.empty:
            return stock.iloc[0]['Code']
        return None
    except:
        return None

# ── 2. 네이버 PC Financial Summary 크롤링 ─────────────────────
def get_naver_finance(stock_code):
    """
    finance.naver.com/item/main.naver 의 '주요재무정보' 테이블(멀티레벨 헤더)을 파싱.
    반환: {
      'years':     ['2021/12', ..., '2026/12(E)', '2027/12(E)', '2028/12(E)'],
      'hist_idx':  [0,1,2,3,4],   # 확정 연도 인덱스
      'est_idx':   [5,6,7],       # 컨센서스 인덱스
      'rows':      {'매출액': [v0,...,v7], 'EPS': [...], ...}
    }
    """
    url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
    resp = requests.get(url, headers=NAVER_HEADERS, timeout=20)
    if resp.status_code != 200:
        return None

    # requests가 HTTP 헤더 charset을 자동 감지해 디코딩
    resp.encoding = resp.apparent_encoding or 'utf-8'
    tables = pd.read_html(io.StringIO(resp.text))

    # 재무 테이블 찾기: 컬럼이 튜플이고 '매출액' 행이 있는 테이블
    fin_table = None
    for t in tables:
        cols_flat = [flatten_col(c) for c in t.columns]
        # 연도 컬럼이 4개 이상이고 EPS 행이 있으면 선택
        year_cols = [c for c in cols_flat if re.search(r'\d{4}', c)]
        if len(year_cols) < 4:
            continue
        first_col_vals = t.iloc[:, 0].astype(str)
        if first_col_vals.str.contains('매출액').any() and first_col_vals.str.contains('EPS').any():
            fin_table = t
            break

    if fin_table is None:
        return None

    # 컬럼 평탄화 및 연도 레이블 추출
    flat_cols = [flatten_col(c) for c in fin_table.columns]
    year_labels = []
    year_col_indices = []   # 연도 데이터가 담긴 컬럼 위치
    for i, c in enumerate(flat_cols):
        if re.search(r'\d{4}', c):
            year_labels.append(norm_year(c))
            year_col_indices.append(i)

    if not year_labels:
        return None

    # 행 이름 → 값 딕셔너리 구성
    # 행 이름은 첫 번째 컬럼
    ROW_ALIASES = {
        '매출액':           '매출액',
        '영업이익':         '영업이익',
        '영업이익(발표기준)': '영업이익(발표)',
        '세전계속사업이익':  '세전이익',
        '당기순이익':       '당기순이익',
        '당기순이익(지배)':  '당기순이익(지배)',
        'ROE(%)':           'ROE',
        'ROA(%)':           'ROA',
        '영업이익률':       '영업이익률',
        '순이익률':         '순이익률',
        'EPS(원)':          'EPS',
        'PER(배)':          'PER',
        'BPS(원)':          'BPS',
        'PBR(배)':          'PBR',
        '현금DPS(원)':      'DPS',
        '현금배당수익률':   '배당수익률',
        '현금배당성향(%)':  '배당성향',
        'CAPEX':            'CAPEX',
        'FCF':              'FCF',
        '발행주식수(보통주)': '발행주식수',
        '영업활동현금흐름':  '영업CF',
        '투자활동현금흐름':  '투자CF',
        '재무활동현금흐름':  '재무CF',
        '부채비율':         '부채비율',
        '자본유보율':       '자본유보율',
        '자산총계':         '자산총계',
        '부채총계':         '부채총계',
        '자본총계':         '자본총계',
    }

    rows = {}
    for _, row in fin_table.iterrows():
        raw_name = str(row.iloc[0]).strip()
        # 별칭 매핑
        mapped = None
        for alias, key in ROW_ALIASES.items():
            if alias in raw_name or raw_name in alias:
                mapped = key
                break
        if mapped is None:
            mapped = raw_name  # 매핑 없으면 원본 사용

        vals = []
        for ci in year_col_indices:
            cell = row.iloc[ci]
            vals.append(str(cell).strip() if not pd.isna(cell) else '-')
        rows[mapped] = vals

    hist_idx = [i for i, y in enumerate(year_labels) if '(E)' not in y]
    est_idx  = [i for i, y in enumerate(year_labels) if '(E)' in y]

    return {
        'years':    year_labels,
        'hist_idx': hist_idx,
        'est_idx':  est_idx,
        'rows':     rows,
    }

# ── 3. 유틸 ──────────────────────────────────────────────────
def safe_float(val):
    try:
        if val is None: return None
        s = str(val).replace(',', '').replace('%', '').strip()
        if s in ('-', '', 'nan', 'None', 'NaN'): return None
        m = re.search(r'-?\d+\.?\d*', s)
        return float(m.group()) if m else None
    except:
        return None

def get_val(rows, key, idx):
    row = rows.get(key, [])
    if idx is None or idx >= len(row): return None
    return safe_float(row[idx])

# ── 4. 현재주가 ────────────────────────────────────────────────
def get_current_price(stock_code):
    try:
        start = pd.Timestamp.now().date() - pd.Timedelta(days=7)
        df = fdr.DataReader(stock_code, start)
        if not df.empty:
            return float(df['Close'].iloc[-1])
    except:
        pass
    return None

# ── 5. 무위험률 ────────────────────────────────────────────────
def get_risk_free_rate():
    try:
        df = fdr.DataReader('^TNX', pd.Timestamp.now().date() - pd.Timedelta(days=5))
        if not df.empty:
            val = float(df['Close'].iloc[-1])
            if 1.0 < val < 20.0:
                return val / 100
    except:
        pass
    return 0.044

# ── 6. 베타 (네이버 PC 업종비교 테이블) ─────────────────────────
def get_beta_naver(stock_code):
    try:
        url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
        resp = requests.get(url, headers=NAVER_HEADERS, timeout=15)
        resp.encoding = resp.apparent_encoding or 'utf-8'
        tables = pd.read_html(io.StringIO(resp.text))
        for t in tables:
            for col in t.columns:
                for i, v in enumerate(t[col].astype(str)):
                    if '베타' in v:
                        for cell in t.iloc[i].astype(str):
                            m = re.search(r'\d+\.\d+', cell)
                            if m:
                                b = float(m.group())
                                if 0.1 < b < 5.0:
                                    return b
    except:
        pass
    return 1.0

# ── 7. 밴드 분석 ───────────────────────────────────────────────
def calc_valuation_band(naver_data, current_price):
    rows     = naver_data['rows']
    years    = naver_data['years']
    hist_idx = naver_data['hist_idx']
    est_idx  = naver_data['est_idx']

    # 25년(마지막 확정), 26E(첫 컨센서스)
    idx_25  = hist_idx[-1] if hist_idx else None
    idx_26e = est_idx[0]   if est_idx  else None

    def make_band(metric, hist_vals, val_25, val_26e,
                  base_25=None, base_26e=None, base_label=None, no_theory=False):
        hist_vals = [v for v in hist_vals if v is not None and v > 0]
        if len(hist_vals) < 2:
            return {"metric": metric, "error": "데이터 부족"}

        avg = sum(hist_vals) / len(hist_vals)
        std = math.sqrt(sum((v - avg)**2 for v in hist_vals) / len(hist_vals))

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

        tp25, tp26e = theory(base_25), theory(base_26e)
        d25, dp25   = diff_info(tp25)
        d26e, dp26e = diff_info(tp26e)

        bands = {k: round(avg + s * std, 2)
                 for k, s in [('m3s',-3),('m2s',-2),('m1s',-1),
                               ('avg',0),('p1s',1),('p2s',2),('p3s',3)]}
        return {
            "metric": metric, "base_label": base_label,
            "hist_avg": round(avg,2), "hist_std": round(std,2),
            "bands": bands,
            "val_25":  round(val_25,  2) if val_25  is not None else None,
            "val_26e": round(val_26e, 2) if val_26e is not None else None,
            "grade_25":  grade(val_25),  "grade_26e": grade(val_26e),
            "theory_25":  f"{tp25:,.0f}"  if tp25  else None,
            "theory_26e": f"{tp26e:,.0f}" if tp26e else None,
            "diff_25": d25, "diff_pct_25": dp25,
            "diff_26e": d26e, "diff_pct_26e": dp26e,
        }

    # ── PER ──
    per_hist = [get_val(rows,'PER',i) for i in hist_idx]
    eps_row  = rows.get('EPS', [])
    per_25   = get_val(rows,'PER',idx_25)
    per_26e  = get_val(rows,'PER',idx_26e)
    eps_25   = get_val(rows,'EPS',idx_25)
    eps_26e  = get_val(rows,'EPS',idx_26e)

    results = [make_band("PER", per_hist, per_25, per_26e,
                          base_25=eps_25, base_26e=eps_26e, base_label="EPS")]

    # ── PBR ──
    pbr_hist = [get_val(rows,'PBR',i) for i in hist_idx]
    bps_25   = get_val(rows,'BPS',idx_25)
    bps_26e  = get_val(rows,'BPS',idx_26e)
    results.append(make_band("PBR", pbr_hist,
                              get_val(rows,'PBR',idx_25),
                              get_val(rows,'PBR',idx_26e),
                              base_25=bps_25, base_26e=bps_26e, base_label="BPS"))

    # ── PEG ──
    def calc_peg(per_val, eps_idx):
        if per_val is None or per_val <= 0: return None
        for s in range(max(0, eps_idx-5), eps_idx):
            es = safe_float(eps_row[s]) if s < len(eps_row) else None
            ee = safe_float(eps_row[eps_idx]) if eps_idx < len(eps_row) else None
            n  = eps_idx - s
            if es and ee and es > 0 and ee > 0 and n > 0:
                cagr = (ee/es)**(1/n) - 1
                if cagr > 0:
                    return round(per_val / (cagr*100), 2)
        return None

    hist_peg = [calc_peg(get_val(rows,'PER',i), i) for i in hist_idx]
    hist_peg = [v for v in hist_peg if v is not None]
    results.append(make_band("PEG", hist_peg,
                              calc_peg(per_25, idx_25) if idx_25 is not None else None,
                              calc_peg(per_26e, idx_26e) if idx_26e is not None else None,
                              no_theory=True))

    # ── PSR ──
    # 발행주식수(보통주) 단위: 주 → 천주로 변환
    shares_k = None
    share_row = rows.get('발행주식수', [])
    for i in reversed(hist_idx):          # 가장 최근 확정치
        v = safe_float(share_row[i]) if i < len(share_row) else None
        if v and v > 1e6:
            shares_k = v / 1000           # 주 → 천주
            break
    if shares_k is None:
        shares_k = 5919638                # fallback

    rev_row = rows.get('매출액', [])

    def sps(rev_val):
        if rev_val and shares_k and shares_k > 0:
            return round(rev_val / shares_k * 1e5, 0)
        return None

    hist_psr = []
    for i in hist_idx:
        per_i = get_val(rows,'PER',i)
        eps_i = safe_float(eps_row[i]) if i < len(eps_row) else None
        rev_i = get_val(rows,'매출액',i)
        if per_i and eps_i and rev_i:
            price_i = per_i * eps_i
            sps_i   = sps(rev_i)
            if sps_i and sps_i > 0:
                hist_psr.append(round(price_i / sps_i, 2))

    sps_25   = sps(get_val(rows,'매출액',idx_25))
    sps_26e  = sps(get_val(rows,'매출액',idx_26e))
    psr_25   = round(current_price/sps_25,  2) if current_price and sps_25  else None
    psr_26e  = round(current_price/sps_26e, 2) if current_price and sps_26e else None
    results.append(make_band("PSR", hist_psr, psr_25, psr_26e,
                              base_25=sps_25, base_26e=sps_26e, base_label="SPS"))

    return results

# ── 8. DCF (FCF 직접 사용 + 컨센서스 26~28E) ─────────────────
def calc_dcf(naver_data, r, current_price, g_terminal=0.025):
    """
    네이버 Financial Summary의 FCF를 직접 사용.
    과거 FCF/매출액 마진 평균 → 컨센서스 매출액에 적용해 26~28E FCFF 추정.
    발행주식수도 테이블에서 직접 읽음.
    """
    try:
        rows     = naver_data['rows']
        years    = naver_data['years']
        hist_idx = naver_data['hist_idx']
        est_idx  = naver_data['est_idx']

        rev_row = rows.get('매출액', [])
        fcf_row = rows.get('FCF', [])
        op_row  = rows.get('영업이익(발표)', rows.get('영업이익', []))

        if not rev_row:
            return {"error": "매출액 데이터 없음"}

        # ── 과거 FCF 마진 계산 (FCF가 있으면 직접, 없으면 영업이익 기반) ──
        hist_fcff_margin = []
        for i in hist_idx:
            rev = get_val(rows, '매출액', i)
            fcf = safe_float(fcf_row[i]) if i < len(fcf_row) else None
            if rev and rev > 0 and fcf is not None:
                hist_fcff_margin.append(fcf / rev)

        # FCF 데이터 부족시 영업이익 기반으로 대체
        if len(hist_fcff_margin) < 2:
            TAX = 0.22; DA = 0.05; CAPEX_R = 0.06
            hist_fcff_margin = []
            for i in hist_idx:
                rev = get_val(rows,'매출액',i)
                op  = get_val(rows,'영업이익(발표)',i) or get_val(rows,'영업이익',i)
                if rev and op and rev > 0 and op > 0:
                    fcff = op*(1-TAX) + rev*DA - rev*CAPEX_R
                    hist_fcff_margin.append(fcff / rev)

        if len(hist_fcff_margin) < 2:
            return {"error": "과거 FCF 데이터 부족"}

        avg_fcff_margin = sum(hist_fcff_margin) / len(hist_fcff_margin)

        # ── 컨센서스 FCF 추정 (26E~28E) ──
        fcf_years = []
        for i in est_idx:
            label = years[i]
            rev_e = get_val(rows, '매출액', i)
            op_e  = get_val(rows, '영업이익(발표)', i) or get_val(rows, '영업이익', i)
            fcf_e_direct = safe_float(fcf_row[i]) if i < len(fcf_row) else None

            if fcf_e_direct is not None and fcf_e_direct > 0:
                fcf_years.append((label, fcf_e_direct))
            elif rev_e and op_e and op_e > 0:
                TAX = 0.22; DA = 0.05; CAPEX_R = 0.06
                fcff_e = op_e*(1-TAX) + rev_e*DA - rev_e*CAPEX_R
                fcf_years.append((label, fcff_e))
            elif rev_e:
                fcf_years.append((label, rev_e * avg_fcff_margin))

        if not fcf_years:
            return {"error": "컨센서스 FCF 추정 불가"}

        if r <= g_terminal:
            return {"error": f"할인율({r*100:.1f}%)이 터미널성장률({g_terminal*100:.1f}%)보다 낮음"}

        # ── 발행주식수 (주 단위 → 천주 변환) ──
        shares_k = None
        share_row = rows.get('발행주식수', [])
        for i in reversed(hist_idx):
            v = safe_float(share_row[i]) if i < len(share_row) else None
            if v and v > 1e6:
                shares_k = v / 1000
                break
        if not shares_k:
            shares_k = 5919638

        def pv_to_price(pv_total):
            return pv_total * 1e8 / (shares_k * 1e3)

        def diff_str(fv):
            if not current_price: return None, None
            d = fv - current_price
            return f"{d:+,.0f}", f"{d/current_price*100:+.1f}"

        pv_fcfs = []
        cumulative_pv = 0
        for t_idx, (label, fcf_e) in enumerate(fcf_years):
            n = t_idx + 1
            pv = fcf_e / (1+r)**n
            cumulative_pv += pv
            tv_n    = fcf_e * (1+g_terminal) / (r-g_terminal)
            pv_tv_n = tv_n / (1+r)**n
            total_pv_n = cumulative_pv + pv_tv_n
            fv_n = pv_to_price(total_pv_n)
            d, dp = diff_str(fv_n)
            pv_fcfs.append({
                "year": label, "fcf": round(fcf_e), "pv": round(pv),
                "pv_tv": round(pv_tv_n), "total_pv": round(total_pv_n),
                "fair_value": f"{fv_n:,.0f}", "diff": d, "diff_pct": dp,
            })

        return {
            "avg_fcff_margin": round(avg_fcff_margin*100, 1),
            "avg_tax_rate":    22.0,
            "g_terminal":      round(g_terminal*100, 1),
            "r":               round(r*100, 2),
            "pv_fcfs":         pv_fcfs,
        }
    except Exception as e:
        return {"error": f"DCF 계산 오류: {e}"}

# ── 9. 재무 테이블 HTML ────────────────────────────────────────
def build_raw_table_html(naver_data):
    rows  = naver_data['rows']
    years = naver_data['years']

    DISPLAY = [
        ('매출액',     '매출액'),
        ('영업이익(발표)', '영업이익'),
        ('당기순이익', '당기순이익'),
        ('FCF',       'FCF'),
        ('영업이익률', '영업이익률(%)'),
        ('순이익률',   '순이익률(%)'),
        ('ROE',       'ROE(%)'),
        ('ROA',       'ROA(%)'),
        ('EPS',       'EPS(원)'),
        ('BPS',       'BPS(원)'),
        ('DPS',       'DPS(원)'),
        ('PER',       'PER(배)'),
        ('PBR',       'PBR(배)'),
        ('배당수익률', '배당수익률(%)'),
        ('부채비율',   '부채비율(%)'),
        ('발행주식수', '발행주식수(주)'),
    ]

    header = ('<thead><tr><th>항목</th>'
              + ''.join(f'<th>{"★" if "(E)" in y else ""}{y}</th>' for y in years)
              + '</tr></thead>')
    body = '<tbody>'
    for key, label in DISPLAY:
        if key not in rows:
            continue
        vals = rows[key]
        cells = ''
        for i, v in enumerate(vals):
            cls = ' class="cons-col"' if '(E)' in years[i] else ''
            cells += f'<td{cls}>{v if v not in ("", "nan", "None") else "-"}</td>'
        body += f'<tr><td>{label}</td>{cells}</tr>'
    body += '</tbody>'

    return f'<table class="financial-table">{header}{body}</table>'

# ── 10. 메인 분석 함수 ─────────────────────────────────────────
def analyze_stock(company_name):
    stock_code = get_stock_code(company_name)
    if not stock_code:
        return {"error": f"'{company_name}'(을)를 찾을 수 없습니다."}

    try:
        try:
            naver_data = get_naver_finance(stock_code)
        except Exception as e:
            import traceback
            return {"error": f"[get_naver_finance 오류] {traceback.format_exc()}"}

        if naver_data is None:
            return {"error": f"네이버 증권에서 재무 데이터를 가져오지 못했습니다. (code={stock_code})"}

        current_price = get_current_price(stock_code)
        rf   = get_risk_free_rate()
        beta = get_beta_naver(stock_code)
        r_value = rf + beta * 0.05

        return {
            "name":          company_name,
            "code":          stock_code,
            "raw_table":     build_raw_table_html(naver_data),
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
        import traceback
        err_msg = traceback.format_exc()
        return {"error": f"서버 처리 중 오류: {err_msg}"}


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
