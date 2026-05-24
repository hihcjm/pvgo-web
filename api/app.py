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

FG_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://comp.fnguide.com',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}

# 하위 호환 alias (WiseReport 등 다른 크롤러에서 참조)
NAVER_HEADERS = FG_HEADERS

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

# ── 2. FnGuide highlight_D_Y: 실적 + 26~28(E) 컨센서스 ───────
def get_fnguide_highlight(stock_code):
    """
    FnGuide SVD_Main의 highlight_D_Y div에서 연간 재무 하이라이트 파싱.
    반환: {
      'years':   ['2021/12', ..., '2026/12(E)', '2027/12(E)', '2028/12(E)'],
      'rows':    {'매출액': [...], '영업이익': [...], 'EPS': [...], ...},
      'hist_idx': [...],  # 실적 연도 인덱스
      'est_idx':  [...],  # 추정 연도 인덱스
    }
    연결 기준(ReportGB=D) 연간(Annual) 데이터.
    """
    import io as _io
    try:
        url = (f"https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp"
               f"?pGB=1&gicode=A{stock_code}&cID=&MenuYn=Y&ReportGB=&NewMenuID=101&stkGb=701")
        resp = requests.get(url, headers={**NAVER_HEADERS, 'Referer': 'https://comp.fnguide.com'}, timeout=20)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')

        div = soup.find('div', id='highlight_D_Y')
        if div is None:
            return None

        df = pd.read_html(_io.StringIO(str(div)))[0]

        # MultiIndex 컬럼 → 연도 라벨만 추출
        # 컬럼 구조: (('IFRS(연결)','IFRS(연결)'), ('Annual','2021/12'), ...)
        year_cols = []   # (df_col_index, year_label)
        for ci, col in enumerate(df.columns):
            label = col[1] if isinstance(col, tuple) else str(col)
            label = str(label).strip()
            if re.search(r'20\d\d', label):
                # norm_year 형식으로
                is_est = '(E)' in label
                m = re.search(r'(\d{4})', label)
                mm = re.search(r'[./](\d{2})', label)
                yr  = m.group(1)  if m  else label
                mon = mm.group(1) if mm else '12'
                normed = f"{yr}/{mon}" + ('(E)' if is_est else '')
                year_cols.append((ci, normed))

        if not year_cols:
            return None

        # 항목명 컬럼 (첫 번째 컬럼)
        item_col = df.iloc[:, 0]

        # 항목명 매핑 (FnGuide → 내부 키)
        FG_ALIASES = {
            '매출액':       '매출액',
            '영업이익':     '영업이익',
            '당기순이익':   '당기순이익',
            '지배주주순이익': '지배주주순이익',
            '자산총계':     '자산총계',
            '부채총계':     '부채총계',
            '자본총계':     '자본총계',
            '지배주주지분': '지배주주지분',
            '부채비율':     '부채비율',
            '영업이익률':   '영업이익률',
            'ROE':          'ROE',
            'EPS':          'EPS',
            'BPS':          'BPS',
            'DPS':          'DPS',
            'PER':          'PER',
            'PBR':          'PBR',
            '발행주식수':   '발행주식수',
        }

        rows_out = {}
        years_out = [lbl for _, lbl in year_cols]

        for _, row in df.iterrows():
            raw_nm = str(row.iloc[0]).strip()
            # 키 매핑
            mapped = None
            for alias, key in FG_ALIASES.items():
                if alias in raw_nm:
                    mapped = key
                    break
            if mapped is None:
                continue
            if mapped in rows_out:   # 첫 번째 매칭만 사용 (영업이익 발표기준 중복 방지)
                continue

            vals = []
            for ci, _ in year_cols:
                v = row.iloc[ci]
                if pd.isna(v):
                    vals.append(None)
                else:
                    try:
                        vals.append(str(int(round(float(v)))))
                    except:
                        vals.append(str(v))
            rows_out[mapped] = vals

        hist_idx = [i for i, y in enumerate(years_out) if '(E)' not in y]
        est_idx  = [i for i, y in enumerate(years_out) if '(E)' in y]

        # ── 파생 항목 계산 ──────────────────────────────────────────
        # 영업이익률 (FnGuide highlight에 없으면 계산)
        if '영업이익률' not in rows_out:
            op = rows_out.get('영업이익', [])
            rev = rows_out.get('매출액', [])
            margin = []
            for i in range(len(years_out)):
                o = safe_float(op[i]) if i < len(op) else None
                r = safe_float(rev[i]) if i < len(rev) else None
                if o is not None and r and r > 0:
                    margin.append(str(round(o / r * 100, 2)))
                else:
                    margin.append(None)
            rows_out['영업이익률'] = margin

        # 순이익률 (지배주주순이익 / 매출액)
        if '순이익률' not in rows_out:
            ni = rows_out.get('지배주주순이익', rows_out.get('당기순이익', []))
            rev = rows_out.get('매출액', [])
            margin = []
            for i in range(len(years_out)):
                n = safe_float(ni[i]) if i < len(ni) else None
                r = safe_float(rev[i]) if i < len(rev) else None
                if n is not None and r and r > 0:
                    margin.append(str(round(n / r * 100, 2)))
                else:
                    margin.append(None)
            rows_out['순이익률'] = margin

        # PSR = 주가 / SPS(주당매출액)
        # SPS = 매출액(억원) × 1억 / 발행주식수(주)
        # 주가 = EPS × PER  (실적 연도 역산)
        # 추정 연도는 EPS(E) × PER(E) 또는 BPS(E) × PBR(E) 역산
        sps_row   = rows_out.get('매출액', [])
        eps_row   = rows_out.get('EPS', [])
        per_row   = rows_out.get('PER', [])
        shr_row   = rows_out.get('발행주식수', [])  # 천주 단위
        psr_vals  = []
        for i in range(len(years_out)):
            rev_v = safe_float(sps_row[i]) if i < len(sps_row) else None   # 억원
            eps_v = safe_float(eps_row[i]) if i < len(eps_row) else None   # 원
            per_v = safe_float(per_row[i]) if i < len(per_row) else None   # 배
            shr_v = safe_float(shr_row[i]) if i < len(shr_row) else None   # 천주

            psr = None
            if rev_v and rev_v > 0 and eps_v and eps_v > 0 and per_v and per_v > 0:
                price_est = eps_v * per_v                      # 원 (주가 추정)
                if shr_v and shr_v > 0:
                    shares = shr_v * 1e3                       # 주
                    sps    = (rev_v * 1e8) / shares            # 원/주 (주당매출액)
                else:
                    # 주식수 없으면 EPS 역산: 주식수 ≈ EPS 기준 지배순이익 / EPS
                    # SPS = 매출액 / 지배순이익 × EPS
                    ni_v = safe_float(rows_out.get('지배주주순이익', [None]*i)[i] if i < len(rows_out.get('지배주주순이익',[])) else None)
                    if ni_v and ni_v > 0:
                        sps = (rev_v / ni_v) * eps_v           # 원/주
                    else:
                        sps = None
                if sps and sps > 0:
                    psr = round(price_est / sps, 2)

            psr_vals.append(str(psr) if psr is not None else None)
        rows_out['PSR'] = psr_vals

        return {
            'years':    years_out,
            'rows':     rows_out,
            'hist_idx': hist_idx,
            'est_idx':  est_idx,
        }
    except Exception:
        return None


# ── 2c. 증권사 목표가 ─────────────────────────────────────────
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
        """
        PEG = PER / (EPS CAGR × 100)

        CAGR 계산 규칙:
        - 기준 연도(eps_idx) EPS가 양수여야 함
        - 시작 연도(es) EPS도 양수여야 함
        - 시작~기준 구간 사이에 음수 EPS가 단 1개라도 있으면 해당 구간 제외
          → 연속된 양수 구간만 CAGR 계산에 사용 (가장 먼 유효 구간 우선)
        """
        if per_val is None or per_val <= 0:
            return None
        ee = safe_float(eps_row[eps_idx]) if eps_idx is not None and eps_idx < len(eps_row) else None
        if not ee or ee <= 0:
            return None

        # 가장 먼 시작점부터 탐색 (더 긴 CAGR 기간 우선)
        for s in range(max(0, eps_idx - 5), eps_idx):
            es = safe_float(eps_row[s]) if s < len(eps_row) else None
            if not es or es <= 0:
                continue  # 시작 연도 EPS 음수 → 스킵

            # 시작~기준 구간 내 모든 EPS가 양수인지 검사
            all_positive = all(
                (safe_float(eps_row[k]) or 0) > 0
                for k in range(s, eps_idx + 1)
                if k < len(eps_row)
            )
            if not all_positive:
                continue  # 구간 내 음수 EPS 존재 → 이 구간 제외

            n = eps_idx - s
            if n <= 0:
                continue
            cagr = (ee / es) ** (1 / n) - 1
            if cagr > 0:
                return round(per_val / (cagr * 100), 2)

        return None

    hist_peg = [calc_peg(get_val(rows, 'PER', i), i) for i in hist_idx]
    hist_peg = [v for v in hist_peg if v is not None and v > 0]
    peg_cur  = calc_peg(per_cur, idx_25)  if idx_25  is not None else None
    peg_est  = calc_peg(per_est, idx_26e) if idx_26e is not None else None
    results.append(make_band("PEG", hist_peg, peg_cur, peg_est, no_theory=True))

    # ── PSR ──
    # PSR = 주가 / SPS(주당매출액)
    # SPS(원/주) = 매출액(억원) × 1e8 / 발행주식수(주)
    # 발행주식수(주) = FnGuide raw(천주) × 1000

    # 발행주식수(주) 결정 — 실적 연도 최근값
    shares_actual = None
    share_row = rows.get('발행주식수', [])
    for i in reversed(hist_idx):
        v = safe_float(share_row[i]) if i < len(share_row) else None
        if v and v > 1e5:          # 천주 단위 → 10만 이상이면 유효
            shares_actual = v * 1e3  # 천주 → 주
            break
    # fallback: 지배주주지분(억원) / BPS(원) 역산
    if shares_actual is None:
        eq_v  = get_val(rows, '지배주주지분', idx_25)
        bps_v = get_val(rows, 'BPS', idx_25)
        if eq_v and bps_v and bps_v > 0:
            shares_actual = eq_v * 1e8 / bps_v

    def calc_sps(rev_val):
        """매출액(억원) → SPS(원/주)"""
        if rev_val and rev_val > 0 and shares_actual and shares_actual > 0:
            return round(rev_val * 1e8 / shares_actual, 0)
        return None

    # 역사 PSR: rows['PSR']에 이미 계산된 값 있으면 그대로 사용
    psr_row  = rows.get('PSR', [])
    hist_psr = []
    for i in hist_idx:
        # 1순위: 이미 계산된 PSR 값
        psr_i = safe_float(psr_row[i]) if i < len(psr_row) else None
        if psr_i is None or psr_i <= 0:
            # 2순위: PER × EPS / SPS 직접 계산
            per_i = get_val(rows, 'PER', i)
            eps_i = get_val(rows, 'EPS', i)
            rev_i = get_val(rows, '매출액', i)
            sps_i = calc_sps(rev_i)
            if per_i and eps_i and eps_i > 0 and sps_i and sps_i > 0:
                psr_i = round(per_i * eps_i / sps_i, 2)
        if psr_i and psr_i > 0:
            hist_psr.append(psr_i)

    # 현재/추정 SPS
    sps_cur = calc_sps(get_val(rows, '매출액', idx_25))
    sps_est = calc_sps(get_val(rows, '매출액', idx_26e))

    # 현재가 기준 PSR (밴드의 현재 위치)
    psr_cur = round(current_price / sps_cur, 2) if current_price and sps_cur else None
    psr_est = round(current_price / sps_est, 2) if current_price and sps_est else None
    if psr_cur is not None and psr_cur <= 0: psr_cur = None
    if psr_est is not None and psr_est <= 0: psr_est = None

    results.append(make_band("PSR", hist_psr, psr_cur, psr_est,
                              base_cur=sps_cur, base_est=sps_est, base_label="SPS"))

    return results

# ── 8. EPS 가치평가 ──────────────────────────────────────────────
def calc_eps_valuations(fin_data, r, current_price):
    """
    연도별 EPS/r 및 EPS×ROE×(1-r) 내재가치 계산.
    r: 요구수익률 (소수, e.g. 0.131)
    ROE: FnGuide에서 정수 % (e.g. 11) → 배수 그대로 사용
    """
    rows     = fin_data['rows']
    years    = fin_data['years']

    result_rows = []
    eps_list = rows.get('EPS', [])
    roe_list = rows.get('ROE', [])

    for i, yr in enumerate(years):
        eps = None
        if i < len(eps_list):
            eps = safe_float(eps_list[i])

        roe = None
        if i < len(roe_list):
            roe = safe_float(roe_list[i])

        is_est = '(E)' in str(yr)

        if eps is None:
            continue

        # 1. EPS / r
        val_eps_r = round(eps / r) if r and r > 0 else None
        gap_eps_r = round((val_eps_r - current_price) / current_price * 100, 1) \
                    if val_eps_r is not None and current_price and current_price > 0 else None

        # 2. EPS × ROE(%) × (1 - r)
        val_eps_roe = round(eps * roe * (1 - r)) if roe is not None and r and r > 0 else None
        gap_eps_roe = round((val_eps_roe - current_price) / current_price * 100, 1) \
                      if val_eps_roe is not None and current_price and current_price > 0 else None

        result_rows.append({
            'year':        yr,
            'is_est':      is_est,
            'eps':         round(eps),
            'roe':         roe,
            'val_eps_r':   f"{val_eps_r:,}" if val_eps_r is not None else '-',
            'gap_eps_r':   gap_eps_r,
            'val_eps_roe': f"{val_eps_roe:,}" if val_eps_roe is not None else '-',
            'gap_eps_roe': gap_eps_roe,
        })

    return {'r': round(r * 100, 2), 'rows': result_rows}


# ── 9. 재무 테이블 HTML ────────────────────────────────────────
def build_raw_table_html(naver_data):
    rows  = naver_data['rows']
    years = naver_data['years']

    # 섹션별 구분선을 위한 그룹 구조
    DISPLAY = [
        # ── 손익계산서 ──────────────────────────────
        ('__section__',       '손익계산서'),
        ('매출액',            '매출액 (억원)'),
        ('영업이익',          '영업이익 (억원)'),
        ('지배주주순이익',    '지배주주순이익 (억원)'),
        ('당기순이익',        '당기순이익 (억원)'),
        ('영업이익률',        '영업이익률(%)'),
        ('순이익률',          '순이익률(%)'),
        # ── 재무상태표 ──────────────────────────────
        ('__section__',       '재무상태표'),
        ('자산총계',          '자산총계 (억원)'),
        ('부채총계',          '부채총계 (억원)'),
        ('자본총계',          '자본총계 (억원)'),
        ('지배주주지분',      '지배주주지분 (억원)'),
        ('부채비율',          '부채비율(%)'),
        # ── 주요 투자지표 ────────────────────────────
        ('__section__',       '주요 투자지표'),
        ('ROE',               'ROE(%)'),
        ('EPS',               'EPS(원)'),
        ('BPS',               'BPS(원)'),
        ('DPS',               'DPS(원)'),
        ('PER',               'PER(배)'),
        ('PBR',               'PBR(배)'),
        ('PSR',               'PSR(배)'),
        ('배당수익률',        '배당수익률(%)'),
    ]

    n_years = len(years)
    header = ('<thead><tr><th>항목</th>'
              + ''.join(
                  f'<th class="{"cons-col" if "(E)" in y else "hist-col"}">{"★ " if "(E)" in y else ""}{y}</th>'
                  for y in years)
              + '</tr></thead>')
    body = '<tbody>'
    for key, label in DISPLAY:
        if key == '__section__':
            body += (f'<tr class="section-row">'
                     f'<td colspan="{n_years + 1}">{label}</td></tr>')
            continue
        if key not in rows:
            continue
        vals = rows[key]
        # rows 길이가 years보다 짧을 수 있으므로 패딩
        padded = list(vals) + [None] * (n_years - len(vals))
        cells = ''
        for i in range(n_years):
            v   = padded[i]
            cls = ' class="cons-col"' if '(E)' in years[i] else ''
            disp = v if v not in (None, '', 'nan', 'None', 'NaN') else '-'
            cells += f'<td{cls}>{disp}</td>'
        body += f'<tr><td class="row-label">{label}</td>{cells}</tr>'
    body += '</tbody>'

    return f'<table class="financial-table">{header}{body}</table>'

# ── 10. 메인 분석 함수 ─────────────────────────────────────────
def analyze_stock(company_name):
    stock_code = get_stock_code(company_name)
    if not stock_code:
        return {"error": f"'{company_name}'(을)를 찾을 수 없습니다."}

    try:
        # ── FnGuide를 주 데이터소스로 사용 ──────────────────────────
        fin_data = get_fnguide_highlight(stock_code)
        if fin_data is None:
            return {"error": f"FnGuide에서 재무 데이터를 가져오지 못했습니다. (code={stock_code})"}

        current_price = get_current_price(stock_code)
        rf            = get_risk_free_rate()
        beta          = get_beta_wisereport(stock_code)

        # 다모다란: Base ERP(5%) + Korea CRP(2.5%) = 7.5%
        erp_korea = 0.075
        r_value   = rf + beta * erp_korea

        # 증권사 목표가 (WiseReport에서만 가져옴, 네이버 soup 불필요)
        tp_data = get_target_prices(stock_code, naver_soup=None)

        # 주요 지표 추출 (실적 연도 기준 최근값)
        hist_idx = fin_data['hist_idx']
        rows     = fin_data['rows']

        def last_val(key):
            """실적 연도 중 가장 최근 유효값"""
            for i in reversed(hist_idx):
                v = get_val(rows, key, i)
                if v is not None:
                    return v
            return None

        # ── KPI 지표 추출 (실적 최근 연도 기준) ─────────────────────
        per_trail  = last_val('PER')
        pbr_trail  = last_val('PBR')
        psr_val    = last_val('PSR')
        roe        = last_val('ROE')
        eps_val    = last_val('EPS')
        op_margin  = last_val('영업이익률')
        debt_ratio = last_val('부채비율')

        # 배당수익률: FnGuide highlight에 없으므로 DPS ÷ 주가추정 으로 계산
        # 주가추정 = EPS × PER (실적 기준 역산)
        div_yield = None
        for i in reversed(hist_idx):
            dps_v = get_val(rows, 'DPS', i)
            eps_v = get_val(rows, 'EPS', i)
            per_v = get_val(rows, 'PER', i)
            if dps_v and dps_v > 0 and eps_v and eps_v > 0 and per_v and per_v > 0:
                implied_price = eps_v * per_v          # 원 (주가 역산)
                div_yield = round(dps_v / implied_price * 100, 2)
                break
        # 현재가 기준 배당수익률이 더 정확하면 덮어씀
        if current_price and current_price > 0:
            for i in reversed(hist_idx):
                dps_v = get_val(rows, 'DPS', i)
                if dps_v and dps_v > 0:
                    div_yield = round(dps_v / current_price * 100, 2)
                    break

        # 시가총액 (현재가 × 발행주식수)
        # FnGuide 발행주식수 단위: 천주 → ×1000 해야 실제 주 수
        market_cap = None
        share_row  = rows.get('발행주식수', [])
        for i in reversed(hist_idx):
            v = safe_float(share_row[i]) if i < len(share_row) else None
            if v and v > 1e5 and current_price:
                shares_actual = v * 1e3   # 천주 → 주
                market_cap = round(current_price * shares_actual / 1e12, 2)  # 조원
                break

        return {
            "name":              company_name,
            "code":              stock_code,
            "raw_table":         build_raw_table_html(fin_data),
            "current_price":     f"{current_price:,.0f}" if current_price else "조회 실패",
            "current_price_raw": current_price,
            "market_cap":        market_cap,
            "r_info": {
                "rf":   f"{rf*100:.2f}",
                "beta": f"{beta:.2f}",
                "r":    f"{r_value*100:.2f}",
            },
            "kpi": {
                "per":        round(per_trail, 1)  if per_trail  else None,
                "pbr":        round(pbr_trail, 2)  if pbr_trail  else None,
                "psr":        round(psr_val,   2)  if psr_val    else None,
                "roe":        round(roe,        1)  if roe        else None,
                "eps":        round(eps_val,    0)  if eps_val    else None,
                "op_margin":  round(op_margin,  1)  if op_margin  else None,
                "div_yield":  div_yield,
                "debt_ratio": round(debt_ratio, 1)  if debt_ratio else None,
            },
            "valuation": calc_eps_valuations(fin_data, r_value, current_price),
            "band": calc_valuation_band(fin_data, current_price),
            "tp":   tp_data,
        }

    except Exception:
        import traceback
        return {"error": f"서버 처리 중 오류:\n{traceback.format_exc()}"}


@app.route('/', methods=['GET', 'POST'])
def index():
    result       = None
    company_name = ""
    if request.method == 'POST':
        company_name = request.form.get('company_name', '')
        result = analyze_stock(company_name)
    return render_template('index.html', result=result, company_name=company_name)


if __name__ == '__main__':
    app.run(debug=True)
