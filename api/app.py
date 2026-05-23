from flask import Flask, render_template, request
import pandas as pd
import requests
import FinanceDataReader as fdr
import re
import math
import os
import statistics
from bs4 import BeautifulSoup

template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')
app = Flask(__name__, template_folder=template_dir)

NAVER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://finance.naver.com',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}

# ── 컬럼명 정규화 ──────────────────────────────────────────────
def get_col_year(c):
    if isinstance(c, tuple):
        for part in c:
            if re.search(r'20\d\d', str(part)):
                return str(part)
    else:
        if re.search(r'20\d\d', str(c)):
            return str(c)
    return None

def norm_year(s):
    s = str(s).strip()
    is_est = '(E)' in s
    m = re.search(r'(\d{4})', s)
    year = m.group(1) if m else s
    mm = re.search(r'[./](\d{2})', s)
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
def _fetch_naver_main(stock_code):
    url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
    resp = requests.get(url, headers=NAVER_HEADERS, timeout=20)
    if resp.status_code != 200:
        return None, None
    resp.encoding = 'utf-8'
    soup = BeautifulSoup(resp.text, 'html.parser')
    return soup, resp.text

def get_naver_finance(stock_code):
    """
    BeautifulSoup으로 '주요재무정보' 테이블을 직접 파싱.
    반환: {
      'years':     ['2023/12', '2024/12', '2025/12', '2026/12(E)'],
      'hist_idx':  [0,1,2],
      'est_idx':   [3],
      'rows':      {'매출액': [...], 'EPS': [...], ...},
      'soup':      BeautifulSoup 객체
    }
    """
    soup, _ = _fetch_naver_main(stock_code)
    if soup is None:
        return None

    tbodies = soup.find_all('tbody')
    if len(tbodies) < 3:
        return None
    fin_tbody = tbodies[2]
    fin_table_tag = fin_tbody.find_parent('table')
    if fin_table_tag is None:
        return None

    thead = fin_table_tag.find('thead')
    if thead is None:
        return None

    header_rows = thead.find_all('tr')
    if len(header_rows) < 2:
        return None

    annual_col_count = 0
    for th in header_rows[0].find_all(['th', 'td']):
        txt = th.get_text(strip=True)
        if '연간' in txt:
            try:
                annual_col_count = int(th.get('colspan', 1))
            except:
                annual_col_count = 4
            break

    year_ths = header_rows[1].find_all(['th', 'td'])
    year_labels = []
    annual_col_indices = []
    for i, th in enumerate(year_ths):
        if annual_col_count and i >= annual_col_count:
            break
        txt = th.get_text(strip=True)
        if re.search(r'20\d\d', txt):
            year_labels.append(norm_year(txt))
            annual_col_indices.append(i)

    if not year_labels:
        return None

    ROW_ALIASES = {
        '매출액':         '매출액',
        '영업이익':       '영업이익',
        '영업이익(발표)': '영업이익',
        '영업이익률':     '영업이익률',
        '당기순이익':     '당기순이익',
        '순이익률':       '순이익률',
        'ROE(지배주주)':  'ROE',
        'ROE(%)':         'ROE',
        '부채비율':       '부채비율',
        '당좌비율':       '당좌비율',
        '유보율':         '자본유보율',
        'EPS(원)':        'EPS',
        'PER(배)':        'PER',
        'BPS(원)':        'BPS',
        'PBR(배)':        'PBR',
        '주당배당금(원)': 'DPS',
        '시가배당률(%)':  '배당수익률',
        '배당성향(%)':    '배당성향',
    }

    rows = {}
    for tr in fin_tbody.find_all('tr'):
        th = tr.find('th')
        raw_name = th.get_text(strip=True) if th else ''
        if not raw_name:
            continue
        mapped = ROW_ALIASES.get(raw_name)
        if mapped is None:
            for alias, key in ROW_ALIASES.items():
                if alias in raw_name:
                    mapped = key
                    break
        if mapped is None:
            mapped = raw_name

        tds = tr.find_all('td')
        vals = []
        for ci in annual_col_indices:
            if ci < len(tds):
                v = tds[ci].get_text(strip=True).replace(',', '')
                vals.append(v if v else '-')
            else:
                vals.append('-')
        rows[mapped] = vals

    hist_idx = [i for i, y in enumerate(year_labels) if '(E)' not in y]
    est_idx  = [i for i, y in enumerate(year_labels) if '(E)' in y]

    return {
        'years':    year_labels,
        'hist_idx': hist_idx,
        'est_idx':  est_idx,
        'rows':     rows,
        'soup':     soup,
    }

# ── 2b. 증권사 목표가 ─────────────────────────────────────────
def get_target_prices(stock_code, naver_soup=None):
    result = {}

    if naver_soup:
        inv_table = naver_soup.find('table', summary='투자의견 정보')
        if inv_table:
            ems = inv_table.find_all('em')
            if len(ems) >= 2:
                tp_text = ems[1].get_text(strip=True).replace(',', '')
                try:
                    result['consensus_tp'] = int(tp_text)
                except:
                    pass

    broker_list = []
    try:
        url = f'https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={stock_code}'
        resp = requests.get(url, headers={**NAVER_HEADERS, 'Referer': 'https://navercomp.wisereport.co.kr'}, timeout=15)
        resp.encoding = 'utf-8'
        soup_wr = BeautifulSoup(resp.text, 'html.parser')
        tbodies = soup_wr.find_all('tbody')
        if len(tbodies) >= 7:
            tb = tbodies[6]
            for tr in tb.find_all('tr'):
                tds = [td.get_text(strip=True) for td in tr.find_all('td')]
                if len(tds) >= 3:
                    broker  = tds[0]
                    date    = tds[1]
                    tp_str  = tds[2].replace(',', '')
                    opinion = tds[5] if len(tds) > 5 else ''
                    if broker and re.match(r'\d+', tp_str):
                        try:
                            broker_list.append({
                                'broker':  broker,
                                'tp':      int(tp_str),
                                'date':    date,
                                'opinion': opinion,
                            })
                        except:
                            pass
                    elif broker == '최근 3개월 이내에 제시된 의견이 없습니다.':
                        break
    except:
        pass

    if broker_list:
        prices = [b['tp'] for b in broker_list]
        result['broker_list'] = broker_list
        result['tp_avg']  = int(sum(prices) / len(prices))
        result['tp_high'] = max(prices)
        result['tp_low']  = min(prices)
    else:
        result['broker_list'] = []

    return result if (result.get('consensus_tp') or result.get('broker_list')) else None

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

# ── 6. 베타 (wisereport 52주베타) ───────────────────────────────
def get_beta_wisereport(stock_code):
    try:
        url = f'https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={stock_code}'
        resp = requests.get(
            url,
            headers={**NAVER_HEADERS, 'Referer': 'https://navercomp.wisereport.co.kr'},
            timeout=15
        )
        resp.encoding = 'utf-8'
        soup_wr = BeautifulSoup(resp.text, 'html.parser')
        for tr in soup_wr.find_all('tr'):
            th = tr.find('th')
            if th and '52주베타' in th.get_text():
                td = tr.find('td')
                if td:
                    m = re.search(r'\d+\.\d+', td.get_text())
                    if m:
                        b = float(m.group())
                        if 0.1 < b < 5.0:
                            return b
    except:
        pass
    return 1.0

# ── 6b. FnGuide 현금흐름표 (영업활동CF + CAPEX) ──────────────────
def get_fnguide_cashflow(stock_code):
    """
    comp.fnguide.com에서 연결 현금흐름표 파싱.
    반환: {'2023/12': {'op_cf': 42782, 'capex': 83251, 'fcf': -40469}, ...}
    """
    try:
        url = (f'https://comp.fnguide.com/SVO2/ASP/SVD_Finance.asp'
               f'?pGB=1&gicode=A{stock_code}&cID=&MenuYn=Y&ReportGB='
               f'&NewMenuID=104&stkGb=701')
        headers = {**NAVER_HEADERS, 'Referer': 'https://comp.fnguide.com'}
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            return None
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')

        tables = soup.find_all('table')
        cf_table = None
        cf_years = []
        for t in tables:
            thead = t.find('thead')
            if thead:
                ths = [th.get_text(strip=True) for th in thead.find_all('th')]
                if 'IFRS(연결)' in ths and any(re.search(r'20\d\d/\d\d', th) for th in ths):
                    cf_years = [th for th in ths if re.search(r'20\d\d/\d\d', th)]
                    tbody = t.find('tbody')
                    if tbody:
                        rows = tbody.find_all('tr')
                        if rows:
                            first_text = rows[0].find('th') or rows[0].find('td')
                            if first_text and '영업활동' in first_text.get_text():
                                cf_table = tbody
                                break

        if cf_table is None:
            tbodies = soup.find_all('tbody')
            if len(tbodies) >= 5:
                cf_table = tbodies[4]
                all_tables = soup.find_all('table')
                if len(all_tables) >= 5:
                    th_row = all_tables[4].find('thead')
                    if th_row:
                        cf_years = [th.get_text(strip=True)
                                    for th in th_row.find_all('th')
                                    if re.search(r'20\d\d/\d\d', th.get_text())]

        if cf_table is None or not cf_years:
            return None

        def parse_val(s):
            s = s.strip().replace(',', '').replace(' ', '')
            if s in ('-', '', 'N/A'):
                return None
            try:
                return float(s)
            except:
                return None

        result = {}
        rows_list = cf_table.find_all('tr')

        def get_row_vals(row):
            cells = row.find_all('td')
            return [parse_val(c.get_text(strip=True)) for c in cells]

        op_cf_vals = None
        capex_vals = None

        for r in rows_list:
            th = r.find('th')
            label = th.get_text(strip=True) if th else ''
            if not label:
                first_td = r.find('td')
                label = first_td.get_text(strip=True) if first_td else ''
            clean = re.sub(r'계산에 참여한 계정 펼치기', '', label).strip()

            if '영업활동으로인한현금흐름' in clean and op_cf_vals is None:
                op_cf_vals = get_row_vals(r)
            elif '유형자산의증가' in clean and capex_vals is None:
                capex_vals = get_row_vals(r)

            if op_cf_vals and capex_vals:
                break

        if not op_cf_vals:
            return None

        for i, yr in enumerate(cf_years):
            y_norm = norm_year(yr)
            op_cf = op_cf_vals[i] if i < len(op_cf_vals) else None
            capex = capex_vals[i] if capex_vals and i < len(capex_vals) else None
            if op_cf is not None:
                fcf = op_cf - (capex or 0)
                result[y_norm] = {
                    'op_cf': round(op_cf),
                    'capex': round(capex) if capex is not None else 0,
                    'fcf':   round(fcf),
                }

        return result if result else None

    except Exception:
        return None

# ── 7. 밴드 분석 ───────────────────────────────────────────────
def calc_valuation_band(naver_data, current_price):
    rows     = naver_data['rows']
    years    = naver_data['years']
    hist_idx = naver_data['hist_idx']
    est_idx  = naver_data['est_idx']

    idx_25  = hist_idx[-1] if hist_idx else None
    idx_26e = est_idx[0]   if est_idx  else None

    def make_band(metric, hist_vals, val_cur, val_est,
                  base_cur=None, base_est=None, base_label=None, no_theory=False):
        # 양수 값만 사용
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
            return round(avg * base) if base is not None and not no_theory else None

        def diff_info(tp):
            if tp is None or not current_price: return None, None
            d = tp - current_price
            return f"{d:+,.0f}", f"{d / current_price * 100:+.1f}"

        tp_cur, tp_est = theory(base_cur), theory(base_est)
        d_cur,  dp_cur  = diff_info(tp_cur)
        d_est,  dp_est  = diff_info(tp_est)

        # fwd_val: 추정치 우선, 없으면 현재
        fwd_val = val_est if val_est is not None else val_cur

        bands = {k: round(avg + s * std, 2)
                 for k, s in [('m3s',-3),('m2s',-2),('m1s',-1),
                               ('avg',0),('p1s',1),('p2s',2),('p3s',3)]}

        def grade_label(g):
            mapping = {
                '극저평가': 'Significantly Undervalued',
                '저평가':   'Undervalued',
                '적정':     'Fair',
                '고평가':   'Overvalued',
                '초고평가': 'Significantly Overvalued',
            }
            return mapping.get(g, g)

        grade_fwd = grade(fwd_val)
        # theory_fwd: 추정 기준 우선
        theory_fwd = tp_est if tp_est else tp_cur
        diff_fwd   = d_est  if d_est  else d_cur
        diff_pct_fwd = dp_est if dp_est else dp_cur

        return {
            "metric": metric, "base_label": base_label,
            "hist_avg": round(avg, 2), "hist_std": round(std, 2),
            "hist_vals": hist_vals,
            "bands": bands,
            "fwd_val": fwd_val,
            "val_cur":  round(val_cur,  2) if val_cur  is not None else None,
            "val_est":  round(val_est,  2) if val_est  is not None else None,
            "grade_fwd": grade_label(grade_fwd),
            "theory_fwd": f"{theory_fwd:,}" if theory_fwd else None,
            "diff_fwd": diff_fwd,
            "diff_pct_fwd": diff_pct_fwd,
            # 이전 호환용
            "grade_25": grade(val_cur), "grade_26e": grade(val_est),
            "theory_25": f"{tp_cur:,}" if tp_cur else None,
            "theory_26e": f"{tp_est:,}" if tp_est else None,
        }

    eps_row = rows.get('EPS', [])

    # ── PER ──
    per_hist = []
    for i in hist_idx:
        eps_i = get_val(rows, 'EPS', i)
        per_i = get_val(rows, 'PER', i)
        if eps_i is not None and eps_i > 0 and per_i is not None and per_i > 0:
            per_hist.append(per_i)

    eps_cur = get_val(rows, 'EPS', idx_25)
    eps_est = get_val(rows, 'EPS', idx_26e)
    if eps_cur is not None and eps_cur <= 0: eps_cur = None
    if eps_est is not None and eps_est <= 0: eps_est = None

    per_cur = get_val(rows, 'PER', idx_25)
    per_est = get_val(rows, 'PER', idx_26e)
    if per_cur is not None and per_cur <= 0: per_cur = None
    if per_est is not None and per_est <= 0: per_est = None

    results = [make_band("PER", per_hist, per_cur, per_est,
                          base_cur=eps_cur, base_est=eps_est, base_label="EPS")]

    # ── PBR ──
    pbr_hist = []
    for i in hist_idx:
        bps_i = get_val(rows, 'BPS', i)
        pbr_i = get_val(rows, 'PBR', i)
        if bps_i is not None and bps_i > 0 and pbr_i is not None and pbr_i > 0:
            pbr_hist.append(pbr_i)

    bps_cur = get_val(rows, 'BPS', idx_25)
    bps_est = get_val(rows, 'BPS', idx_26e)
    if bps_cur is not None and bps_cur <= 0: bps_cur = None
    if bps_est is not None and bps_est <= 0: bps_est = None

    pbr_cur = get_val(rows, 'PBR', idx_25)
    pbr_est = get_val(rows, 'PBR', idx_26e)
    if pbr_cur is not None and pbr_cur <= 0: pbr_cur = None
    if pbr_est is not None and pbr_est <= 0: pbr_est = None

    results.append(make_band("PBR", pbr_hist, pbr_cur, pbr_est,
                              base_cur=bps_cur, base_est=bps_est, base_label="BPS"))

    # ── PEG ──
    def calc_peg(per_val, eps_idx):
        if per_val is None or per_val <= 0: return None
        ee = safe_float(eps_row[eps_idx]) if eps_idx is not None and eps_idx < len(eps_row) else None
        if not ee or ee <= 0: return None
        for s in range(max(0, eps_idx-5), eps_idx):
            es = safe_float(eps_row[s]) if s < len(eps_row) else None
            n  = eps_idx - s
            if es and es > 0 and n > 0:
                cagr = (ee/es)**(1/n) - 1
                if cagr > 0:
                    return round(per_val / (cagr*100), 2)
        return None

    hist_peg = [calc_peg(get_val(rows,'PER',i), i) for i in hist_idx]
    hist_peg = [v for v in hist_peg if v is not None and v > 0]
    peg_cur  = calc_peg(per_cur, idx_25) if idx_25 is not None else None
    peg_est  = calc_peg(per_est, idx_26e) if idx_26e is not None else None
    results.append(make_band("PEG", hist_peg, peg_cur, peg_est, no_theory=True))

    # ── PSR ──
    shares_k = None
    share_row = rows.get('발행주식수', [])
    for i in reversed(hist_idx):
        v = safe_float(share_row[i]) if i < len(share_row) else None
        if v and v > 1e6:
            shares_k = v / 1000
            break
    if shares_k is None:
        shares_k = 5919638

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
        if per_i and eps_i and eps_i > 0 and rev_i and rev_i > 0:
            price_i = per_i * eps_i
            sps_i   = sps(rev_i)
            if sps_i and sps_i > 0:
                hist_psr.append(round(price_i / sps_i, 2))

    sps_cur  = sps(get_val(rows,'매출액',idx_25))
    sps_est  = sps(get_val(rows,'매출액',idx_26e))
    psr_cur  = round(current_price/sps_cur,  2) if current_price and sps_cur  else None
    psr_est  = round(current_price/sps_est, 2) if current_price and sps_est else None
    if psr_cur is not None and psr_cur <= 0: psr_cur = None
    if psr_est is not None and psr_est <= 0: psr_est = None

    results.append(make_band("PSR", hist_psr, psr_cur, psr_est,
                              base_cur=sps_cur, base_est=sps_est, base_label="SPS"))

    return results

# ── 8. Rolling DCF (Damodaran 7-Stage Lifecycle) ─────────────────
def calc_rolling_dcf(naver_data, r, current_price, stock_code=None):
    """
    네이버 크롤링 데이터 → rolling_dcf 7-Stage 모듈로 변환.
    - 한국 기업은 억원 단위 → 조원(T)으로 변환 후 $B 동일 구조로 처리
    - 7-Stage Lifecycle: Pre-Revenue / Start-up / High-Growth /
      Mature-Growth / Mature-Stable / Cyclical / Declining 자동 분류
    - WACC decay: compounded discrete discount factors (per-year WACC schedule)
    - TV = FCF_final+1 / (terminal_WACC - rf)
    """
    try:
        from api.rolling_dcf import (
            Financials, ConsensusYear,
            classify_and_configure, calculate_rolling_targets, build_full_schedule,
        )
    except ImportError:
        try:
            import sys
            sys.path.insert(0, os.path.dirname(__file__))
            from rolling_dcf import (
                Financials, ConsensusYear,
                classify_and_configure, calculate_rolling_targets, build_full_schedule,
            )
        except ImportError as e:
            return {"error": f"rolling_dcf import failed: {e}"}

    try:
        rows     = naver_data['rows']
        years    = naver_data['years']
        hist_idx = naver_data['hist_idx']
        est_idx  = naver_data['est_idx']

        # 단위: 억원 → T원(조)  (1T = 10000억)
        UNIT = 1e4  # 억 → T(조)

        def to_T(v):
            """억원 → 조원"""
            return v / UNIT if v is not None else None

        def get_latest(key):
            for i in hist_idx:
                v = get_val(rows, key, i)
                if v is not None: return v
            return None

        def get_two_latest(key):
            vals = [(get_val(rows, key, i), i) for i in hist_idx if get_val(rows, key, i) is not None]
            if len(vals) >= 2:
                return vals[0][0], vals[1][0]
            return (vals[0][0] if vals else None, None)

        rev_latest_raw, rev_prev_raw = get_two_latest('매출액')
        op_latest_raw                = get_latest('영업이익')

        rev_latest = to_T(rev_latest_raw) or 1.0
        rev_prev   = to_T(rev_prev_raw)
        op_latest  = to_T(op_latest_raw)

        ebit_margin    = (op_latest / rev_latest) if op_latest is not None and rev_latest else 0.0
        rev_growth_act = ((rev_latest / rev_prev) - 1) if rev_prev and rev_prev > 0 else 0.05

        # ── 과거 EBIT 마진 시계열 (Cyclical 감지용) ─────────────────
        hist_ebit_margins = []
        for i in hist_idx:
            rev_i = to_T(get_val(rows, '매출액', i))
            op_i  = to_T(get_val(rows, '영업이익', i))
            if rev_i and rev_i > 0 and op_i is not None:
                hist_ebit_margins.append(op_i / rev_i)

        # ── 과거 매출 성장률 시계열 (Declining 감지 + Extrapolation용) ─
        rev_all_T = [to_T(get_val(rows, '매출액', i)) for i in hist_idx]
        rev_all_T = [v for v in rev_all_T if v is not None and v > 0]
        hist_rev_growth: list[float] = []
        for i in range(len(rev_all_T) - 1):
            hist_rev_growth.append(rev_all_T[i] / rev_all_T[i+1] - 1)
        hist_rev_growth = hist_rev_growth[:5]

        # ── FnGuide 현금흐름표로 CapEx 추정 ─────────────────────────
        fnguide_cf = None
        if stock_code:
            fnguide_cf = get_fnguide_cashflow(stock_code)

        # ── 발행주식수 → 조주 환산 (주식수 천주 단위) ────────────────
        shares_k = None
        share_row = rows.get('발행주식수', [])
        for i in reversed(hist_idx):
            v = safe_float(share_row[i]) if i < len(share_row) else None
            if v and v > 1e6:
                shares_k = v / 1000   # 주 → 천주
                break
        if not shares_k:
            for i in reversed(hist_idx):
                eps = get_val(rows, 'EPS', i)
                net = get_val(rows, '당기순이익', i)
                if eps and net and abs(eps) > 100 and net > 0:
                    shares_k = (net * 1e8 / abs(eps)) / 1000
                    break
        if not shares_k and stock_code:
            try:
                df_krx = fdr.StockListing('KRX')
                row_krx = df_krx[df_krx['Code'] == stock_code]
                if not row_krx.empty:
                    for col in ['Shares', 'ListingShares']:
                        if col in row_krx.columns:
                            v = float(row_krx.iloc[0][col])
                            if v > 1e6:
                                shares_k = v / 1000
                                break
            except:
                pass
        if not shares_k:
            shares_k = 1000000   # 1억주 fallback (천주 단위)

        # shares → 조주 단위
        # 시가총액(조원) = 발행주식수(천주) × 1000 × 주가(원) / 1e12
        shares_T = shares_k * 1000 / 1e12  # 천주 → 조주

        # Cash: 보수적으로 0 처리 (네이버에서 직접 추출 어려움)
        cash_T = 0.0

        # 부채: 부채비율 × 자기자본 (자기자본 = BPS × shares)
        bps_latest = get_val(rows, 'BPS', hist_idx[-1] if hist_idx else None)
        debt_ratio = get_val(rows, '부채비율', hist_idx[-1] if hist_idx else None)
        if bps_latest and shares_T > 0:
            equity_T = bps_latest * shares_T  # 조원
            debt_T   = equity_T * (debt_ratio / 100) if debt_ratio else 0.0
        else:
            debt_T = 0.0

        # ── rf 역산 ──────────────────────────────────────────────────
        # r = rf + beta*ERP(5%), 한국 ERP 5% 가정
        rf = r - 0.05
        rf = max(rf, 0.025)

        # ── Financials ──────────────────────────────────────────────
        actuals = Financials(
            revenue           = rev_latest,
            ebit_margin       = ebit_margin,
            revenue_growth    = rev_growth_act,
            cash              = cash_T,
            debt              = debt_T,
            shares            = shares_T,
            hist_ebit_margins = hist_ebit_margins,
            hist_rev_growth   = hist_rev_growth,
        )

        # ── Industry margin 추정 ─────────────────────────────────────
        if ebit_margin <= 0:
            industry_margin = 0.10
        else:
            industry_margin = max(min((ebit_margin + 0.15) / 2, 0.30), 0.05)

        # ── Sales-to-Capital 추정 ────────────────────────────────────
        capex_T = 0.0
        if fnguide_cf:
            capex_vals = [abs(v['capex']) for v in fnguide_cf.values() if v.get('capex')]
            if capex_vals:
                capex_T = to_T(sum(capex_vals) / len(capex_vals)) or rev_latest * 0.05
        if not capex_T:
            capex_T = rev_latest * 0.05

        stc = max(rev_latest / max(capex_T * 5, 0.001), 0.5)
        stc = min(stc, 5.0)

        # ── ConsensusYear 구성 ──────────────────────────────────────
        # 네이버 컨센서스 연도 파싱 → 2026·2027·2028 매핑
        consensus_years: list[ConsensusYear] = []
        available_est_years = []
        for i in est_idx:
            yr_m = re.search(r'(\d{4})', years[i])
            if yr_m:
                available_est_years.append((int(yr_m.group(1)), i))

        for target_yr in (2026, 2027, 2028):
            matched = next((i for yr, i in available_est_years if yr == target_yr), None)
            if matched is not None:
                rev_v  = to_T(get_val(rows, '매출액', matched)) or rev_latest * (1 + max(rev_growth_act, 0.03))
                op_v   = to_T(get_val(rows, '영업이익', matched))
                if op_v is not None and rev_v and rev_v > 0:
                    margin_est = op_v / rev_v
                else:
                    margin_est = ebit_margin
            else:
                # 이전 추정치 또는 최근 실적에서 성장률 연장
                if consensus_years:
                    prev_rev_c = consensus_years[-1].revenue
                else:
                    prev_rev_c = rev_latest
                rev_v      = prev_rev_c * (1 + max(rev_growth_act, 0.03))
                margin_est = ebit_margin

            margin_est = max(min(margin_est, 0.60), -5.0)
            consensus_years.append(ConsensusYear(
                year        = target_yr,
                revenue     = rev_v,
                ebit_margin = margin_est,
            ))

        # ── 7단계 분류 + StageConfig ────────────────────────────────
        stage, cfg = classify_and_configure(
            actuals          = actuals,
            rf               = rf,
            industry_margin  = industry_margin,
            sales_to_capital = stc,
            consensus        = consensus_years,
        )

        tax = 0.22

        # ── FCF 스케줄 ──────────────────────────────────────────────
        schedule_df = build_full_schedule(actuals, consensus_years, cfg, rf, tax)
        schedule = []
        for yr, row in schedule_df.iterrows():
            schedule.append({
                'year':         int(yr),
                'revenue':      row.get('revenue'),
                'rev_growth':   row.get('rev_growth'),
                'ebit_margin':  row.get('ebit_margin'),
                'nopat':        row.get('nopat'),
                'reinvestment': row.get('reinvestment'),
                'fcf':          row.get('fcf'),
                'wacc':         row.get('wacc'),
                'source':       row.get('source'),
            })

        # ── 롤링 타겟 ───────────────────────────────────────────────
        targets_df = calculate_rolling_targets(actuals, consensus_years, cfg, rf, tax)
        targets = []
        for yr, row in targets_df.iterrows():
            tp     = float(row['target_price'])
            # tp = equity(조원) / shares(조주) = 원/주 (단위 일관성 유지)
            tp_won = round(tp * 1e12 / (shares_k * 1000))  # 조원 → 원/주
            upside = round((tp_won - current_price) / current_price * 100, 1) if current_price and current_price > 0 else None
            targets.append({
                'year':           int(yr),
                'stage':          row['stage'],
                'horizon':        int(row['horizon']),
                'proj_window':    row['proj_window'],
                'survival_prob':  float(row['survival_prob']),
                'wacc_start_pct': float(row['wacc_start_pct']),
                'wacc_end_pct':   float(row['wacc_end_pct']),
                'pv_fcfs_T':      float(row['pv_fcfs']),   # 조원
                'pv_tv_T':        float(row['pv_tv']),
                'ev_T':           float(row['ev']),
                'base_cash_T':    float(row['base_cash']),
                'debt_T':         float(row['debt']),
                'equity_T':       float(row['equity']),
                'target_price':   f"{tp_won:,.0f}",
                'target_price_raw': tp_won,
                'upside_pct':     upside,
            })

        return {
            'stage':           stage,
            'horizon':         cfg.horizon,
            'wacc_start':      round(cfg.wacc_start * 100, 2),
            'wacc_end':        round(cfg.wacc_end   * 100, 2),
            'rf':              round(rf * 100, 2),
            'terminal_g':      round(rf * 100, 2),
            'industry_margin': round(industry_margin * 100, 1),
            'tax_rate':        round(tax * 100, 1),
            'stc':             round(stc, 2),
            'survival_prob':   round(cfg.survival_prob, 2),
            'target_margin':   round(cfg.target_margin * 100, 1),
            'schedule':        schedule,
            'targets':         targets,
            'unit':            '조원',
        }

    except Exception:
        import traceback
        return {"error": traceback.format_exc()}

# ── 9. 재무 테이블 HTML ────────────────────────────────────────
def build_raw_table_html(naver_data):
    rows  = naver_data['rows']
    years = naver_data['years']

    DISPLAY = [
        ('매출액',     '매출액 (억원)'),
        ('영업이익',   '영업이익 (억원)'),
        ('당기순이익', '당기순이익 (억원)'),
        ('영업이익률', '영업이익률(%)'),
        ('순이익률',   '순이익률(%)'),
        ('ROE',        'ROE(%)'),
        ('EPS',        'EPS(원)'),
        ('BPS',        'BPS(원)'),
        ('DPS',        'DPS(원)'),
        ('PER',        'PER(배)'),
        ('PBR',        'PBR(배)'),
        ('배당수익률', '배당수익률(%)'),
        ('부채비율',   '부채비율(%)'),
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
        naver_data = get_naver_finance(stock_code)
        if naver_data is None:
            return {"error": f"네이버 증권에서 재무 데이터를 가져오지 못했습니다. (code={stock_code})"}

        soup         = naver_data.get('soup')
        current_price = get_current_price(stock_code)
        rf           = get_risk_free_rate()
        beta         = get_beta_wisereport(stock_code)
        r_value      = rf + beta * 0.05

        # 증권사 목표가
        tp_data = get_target_prices(stock_code, naver_soup=soup)

        # 주요 지표 추출
        hist_idx = naver_data['hist_idx']
        rows     = naver_data['rows']

        def last_val(key):
            for i in hist_idx:
                v = get_val(rows, key, i)
                if v is not None: return v
            return None

        roe        = last_val('ROE')
        per_trail  = last_val('PER')
        pbr_trail  = last_val('PBR')
        div_yield  = last_val('배당수익률')
        debt_ratio = last_val('부채비율')

        # 시가총액 계산 (현재가 × 발행주식수)
        market_cap = None
        share_row  = rows.get('발행주식수', [])
        for i in reversed(hist_idx):
            v = safe_float(share_row[i]) if i < len(share_row) else None
            if v and v > 1e6 and current_price:
                market_cap = round(current_price * v / 1e12, 2)  # 조원
                break

        return {
            "name":          company_name,
            "code":          stock_code,
            "raw_table":     build_raw_table_html(naver_data),
            "current_price": f"{current_price:,.0f}" if current_price else "조회 실패",
            "current_price_raw": current_price,
            "market_cap":    market_cap,
            "r_info": {
                "rf":   f"{rf*100:.2f}",
                "beta": f"{beta:.2f}",
                "r":    f"{r_value*100:.2f}",
            },
            "kpi": {
                "per":       round(per_trail, 1) if per_trail else None,
                "pbr":       round(pbr_trail, 2) if pbr_trail else None,
                "roe":       round(roe, 1)       if roe       else None,
                "div_yield": round(div_yield, 2) if div_yield else None,
                "debt_ratio":round(debt_ratio, 1)if debt_ratio else None,
            },
            "rdcf":  calc_rolling_dcf(naver_data, r_value, current_price, stock_code=stock_code),
            "band":  calc_valuation_band(naver_data, current_price),
            "tp":    tp_data,
        }

    except Exception:
        import traceback
        return {"error": f"서버 처리 중 오류:\n{traceback.format_exc()}"}


@app.route('/', methods=['GET', 'POST'])
def index():
    result       = None
    company_name = ""
    if request.method == 'POST':
        company_name = request.form['company_name']
        result = analyze_stock(company_name)
    return render_template('index.html', result=result, company_name=company_name)


if __name__ == '__main__':
    app.run(debug=True)
