from flask import Flask, render_template, request
import pandas as pd
import requests
import FinanceDataReader as fdr
import re
import math
import os
import io
from bs4 import BeautifulSoup

template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')
app = Flask(__name__, template_folder=template_dir)

NAVER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://finance.naver.com',
    'Accept-Language': 'ko-KR,ko;q=0.9',
}

# ── 컬럼명 정규화 ──────────────────────────────────────────────
# 네이버 주요재무정보 테이블: 3레벨 튜플 ('주요재무정보', '2023.12', 'IFRS연결')
# 연도는 두 번째 레벨(c[1])에 있음
def get_col_year(c):
    """튜플 컬럼에서 연도 문자열 추출. 없으면 None."""
    if isinstance(c, tuple):
        for part in c:
            if re.search(r'20\d\d', str(part)):
                return str(part)
    else:
        if re.search(r'20\d\d', str(c)):
            return str(c)
    return None

# 연도 레이블 정규화: '2026/12(E)' 형태, 확정은 '2023/12' 형태
# 입력: '2023.12', '2026.12(E)' 등
def norm_year(s):
    s = str(s).strip()
    is_est = '(E)' in s
    m = re.search(r'(\d{4})', s)
    year = m.group(1) if m else s
    # 월 추출 - '.' 또는 '/' 구분자 모두 처리
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
    """main.naver 페이지를 한 번만 요청, (soup, resp_text) 반환."""
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
    thead에서 연도 추출, tbody에서 행별 값 추출.
    반환: {
      'years':     ['2023/12', '2024/12', '2025/12', '2026/12(E)'],
      'hist_idx':  [0,1,2],
      'est_idx':   [3],
      'rows':      {'매출액': [...], 'EPS': [...], ...},
      'soup':      BeautifulSoup 객체 (목표가 등 재사용용)
    }
    """
    soup, _ = _fetch_naver_main(stock_code)
    if soup is None:
        return None

    # tbody[2] = 주요재무정보 연간+분기 데이터
    tbodies = soup.find_all('tbody')
    if len(tbodies) < 3:
        return None
    fin_tbody = tbodies[2]
    fin_table_tag = fin_tbody.find_parent('table')
    if fin_table_tag is None:
        return None

    # ── thead에서 연간 연도 추출 ──────────────────────────────
    # thead 구조:
    #   tr[0]: 주요재무정보 | 최근 연간 실적 (colspan=4) | 최근 분기 실적 (colspan=6)
    #   tr[1]: 2023.12 | 2024.12 | 2025.12 | 2026.12(E) | 2025.03 | ...
    #   tr[2]: IFRS연결 | ...
    thead = fin_table_tag.find('thead')
    if thead is None:
        return None

    header_rows = thead.find_all('tr')
    if len(header_rows) < 2:
        return None

    # tr[0]에서 '최근 연간 실적' colspan 계산
    annual_col_count = 0
    for th in header_rows[0].find_all(['th', 'td']):
        txt = th.get_text(strip=True)
        if '연간' in txt:
            try:
                annual_col_count = int(th.get('colspan', 1))
            except:
                annual_col_count = 4
            break

    # tr[1]에서 연도 추출 (첫 번째 th 건너뛰고, 연간 개수만큼)
    year_ths = header_rows[1].find_all(['th', 'td'])
    # 첫 번째 칸은 '주요재무정보' 헤더 (건너뜀)
    # 실제 연도는 index 1부터
    year_labels = []
    annual_col_indices = []  # tbody의 td 인덱스 (첫 번째 th 제외)
    for i, th in enumerate(year_ths):
        if annual_col_count and i >= annual_col_count:
            break  # 분기 영역 시작
        txt = th.get_text(strip=True)
        if re.search(r'20\d\d', txt):
            year_labels.append(norm_year(txt))
            annual_col_indices.append(i)

    if not year_labels:
        return None

    # ── 행 이름 매핑 ─────────────────────────────────────────
    ROW_ALIASES = {
        '매출액':         '매출액',
        '영업이익':       '영업이익',
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
        'soup':     soup,   # 목표가 등 재사용
    }

# ── 2b. 증권사 목표가 ─────────────────────────────────────────
def get_target_prices(stock_code, naver_soup=None):
    """
    증권사 목표가 수집.
    - 컨센서스 평균: main.naver 투자의견 테이블
    - 증권사별 목표가: navercomp.wisereport.co.kr tbody[6]
      (제공처 | 최종일자 | 목표가 | 직전목표가 | 변동률 | 투자의견 | 직전투자의견)
    반환: {
      'consensus_tp': 380417,
      'broker_list':  [{'broker':'미래에셋', 'tp':480000, 'date':'26/05/20', 'opinion':'매수'}, ...],
      'tp_avg': 460000, 'tp_high': 570000, 'tp_low': 390000,
    }
    """
    result = {}

    # ① 컨센서스 평균 목표가 (main.naver 투자의견 테이블)
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

    # ② 증권사별 목표가 리스트 (wisereport)
    broker_list = []
    try:
        url = f'https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={stock_code}'
        resp = requests.get(url, headers={**NAVER_HEADERS, 'Referer': 'https://navercomp.wisereport.co.kr'}, timeout=15)
        resp.encoding = 'utf-8'
        soup_wr = BeautifulSoup(resp.text, 'html.parser')
        tbodies = soup_wr.find_all('tbody')
        # tbody[6] = '제공처별 투자의견 및 목표주가' 테이블
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
    """
    navercomp.wisereport.co.kr c1010001 페이지의 '52주베타' 값을 가져옴.
    실패 시 1.0 반환.
    """
    try:
        url = f'https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={stock_code}'
        resp = requests.get(
            url,
            headers={**NAVER_HEADERS, 'Referer': 'https://navercomp.wisereport.co.kr'},
            timeout=15
        )
        resp.encoding = 'utf-8'
        soup_wr = BeautifulSoup(resp.text, 'html.parser')
        # <th>52주베타</th><td class="num">1.23</td> 구조
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

def get_beta_from_soup(soup):
    """레거시 호환용 — wisereport로 대체됨."""
    return 1.0

# ── 6b. FnGuide 현금흐름표 (영업활동CF + CAPEX) ──────────────────
def get_fnguide_cashflow(stock_code):
    """
    comp.fnguide.com에서 연결 현금흐름표 파싱.
    반환: {
      '2023/12': {'op_cf': 42782, 'capex': 83251, 'fcf': -40469},
      '2024/12': {'op_cf': 297959, 'capex': 159455, 'fcf': 138504},
      ...
    }
    연도-값 딕셔너리. 실패 시 None.
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
        # 연결 현금흐름 테이블: thead='IFRS(연결)' + tbody[4]
        # table[4] = 연간 연결 현금흐름표
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
                        # 첫 번째 행이 '영업활동으로인한현금흐름'인지 확인
                        rows = tbody.find_all('tr')
                        if rows:
                            first_text = rows[0].find('th') or rows[0].find('td')
                            if first_text and '영업활동' in first_text.get_text():
                                cf_table = tbody
                                break

        if cf_table is None:
            # fallback: tbody[4] 직접 접근
            tbodies = soup.find_all('tbody')
            if len(tbodies) >= 5:
                cf_table = tbodies[4]
                # 연도는 table[4] thead에서
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
            if s in ('-', '', 'N/A', '-'):
                return None
            try:
                return float(s)
            except:
                return None

        result = {}
        rows = cf_table.find_all('tr')

        # 행별 값 추출 함수
        def get_row_vals(row):
            cells = row.find_all('td')
            return [parse_val(c.get_text(strip=True)) for c in cells]

        op_cf_vals   = None   # 영업활동으로인한현금흐름
        capex_vals   = None   # 유형자산의증가

        for r in rows:
            th = r.find('th')
            label = th.get_text(strip=True) if th else ''
            # 그룹 제목 행이 없으면 첫 번째 td로
            if not label:
                first_td = r.find('td')
                label = first_td.get_text(strip=True) if first_td else ''

            # '계산에 참여한 계정 펼치기' 등 설명 제거
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
            # yr = '2023/12'
            y_norm = norm_year(yr)   # '2023/12'
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

    except Exception as e:
        return None

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

    eps_row  = rows.get('EPS', [])

    # ── PER ──
    # EPS > 0 인 연도만 PER 히스토리에 포함 (적자 연도 제외)
    per_hist = []
    for i in hist_idx:
        eps_i = get_val(rows, 'EPS', i)
        per_i = get_val(rows, 'PER', i)
        if eps_i is not None and eps_i > 0 and per_i is not None and per_i > 0:
            per_hist.append(per_i)

    per_25   = get_val(rows,'PER',idx_25)
    per_26e  = get_val(rows,'PER',idx_26e)
    eps_25   = get_val(rows,'EPS',idx_25)
    eps_26e  = get_val(rows,'EPS',idx_26e)
    # 현재/추정 시점도 EPS 음수면 이론가 산출 불가 → None 처리
    if eps_25  is not None and eps_25  <= 0: eps_25  = None
    if eps_26e is not None and eps_26e <= 0: eps_26e = None

    results = [make_band("PER", per_hist, per_25, per_26e,
                          base_25=eps_25, base_26e=eps_26e, base_label="EPS")]

    # ── PBR ──
    # BPS > 0 인 연도만 포함 (자본잠식 연도 제외)
    pbr_hist = []
    for i in hist_idx:
        bps_i = get_val(rows, 'BPS', i)
        pbr_i = get_val(rows, 'PBR', i)
        if bps_i is not None and bps_i > 0 and pbr_i is not None and pbr_i > 0:
            pbr_hist.append(pbr_i)

    bps_25   = get_val(rows,'BPS',idx_25)
    bps_26e  = get_val(rows,'BPS',idx_26e)
    if bps_25  is not None and bps_25  <= 0: bps_25  = None
    if bps_26e is not None and bps_26e <= 0: bps_26e = None

    results.append(make_band("PBR", pbr_hist,
                              get_val(rows,'PBR',idx_25),
                              get_val(rows,'PBR',idx_26e),
                              base_25=bps_25, base_26e=bps_26e, base_label="BPS"))

    # ── PEG ──
    # 시작·끝 EPS 모두 양수인 구간만 CAGR 계산
    def calc_peg(per_val, eps_idx):
        if per_val is None or per_val <= 0: return None
        ee = safe_float(eps_row[eps_idx]) if eps_idx < len(eps_row) else None
        if not ee or ee <= 0: return None          # 분모 EPS 음수 → 불가
        for s in range(max(0, eps_idx-5), eps_idx):
            es = safe_float(eps_row[s]) if s < len(eps_row) else None
            n  = eps_idx - s
            if es and es > 0 and n > 0:
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

    # PSR: EPS > 0 이고 매출 > 0 인 연도만 히스토리에 포함
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

    sps_25   = sps(get_val(rows,'매출액',idx_25))
    sps_26e  = sps(get_val(rows,'매출액',idx_26e))
    psr_25   = round(current_price/sps_25,  2) if current_price and sps_25  else None
    psr_26e  = round(current_price/sps_26e, 2) if current_price and sps_26e else None
    results.append(make_band("PSR", hist_psr, psr_25, psr_26e,
                              base_25=sps_25, base_26e=sps_26e, base_label="SPS"))

    return results

# ── 8. DCF (FnGuide 실제 FCF 우선 사용 + 컨센서스 26~28E) ───────
def calc_dcf(naver_data, r, current_price, g_terminal=0.025, stock_code=None):
    """
    FnGuide 현금흐름표에서 실제 FCF(영업활동CF - CAPEX)를 가져와 사용.
    부족 시 영업이익 기반 추정으로 대체.
    컨센서스 매출 × 평균FCF마진 → 26~28E FCFF 추정.
    """
    try:
        rows     = naver_data['rows']
        years    = naver_data['years']
        hist_idx = naver_data['hist_idx']
        est_idx  = naver_data['est_idx']

        rev_row = rows.get('매출액', [])

        if not rev_row:
            return {"error": "매출액 데이터 없음"}

        # ── FnGuide 실제 FCF 가져오기 (영업활동CF - CAPEX) ──────────
        fnguide_cf = None
        if stock_code:
            fnguide_cf = get_fnguide_cashflow(stock_code)

        # ── 과거 FCF 마진 계산 ─────────────────────────────────────
        # 우선순위: ① FnGuide 실제 FCF → ② 영업이익 기반 추정
        hist_fcff_margin = []
        hist_fcf_detail  = []   # 화면 표시용

        if fnguide_cf:
            for i in hist_idx:
                yr = years[i]   # '2023/12', '2024/12' 등
                rev = get_val(rows, '매출액', i)
                cf_data = fnguide_cf.get(yr)
                if rev and rev > 0 and cf_data:
                    fcf = cf_data['fcf']   # 억원
                    margin = fcf / rev
                    # FCF 음수(적자) 연도는 마진 평균 산출에서 제외
                    if fcf > 0:
                        hist_fcff_margin.append(margin)
                    hist_fcf_detail.append({
                        'year': yr,
                        'fcf': round(fcf),
                        'op_cf': cf_data['op_cf'],
                        'capex': cf_data['capex'],
                        'margin': round(margin*100, 1),
                        'src': 'FnGuide실적',
                        'excluded': fcf <= 0,   # 음수 연도 표시용
                    })

        # FnGuide 데이터 부족시 영업이익 기반 추정
        if len(hist_fcff_margin) < 2:
            TAX = 0.22; DA = 0.05; CAPEX_R = 0.06
            hist_fcff_margin = []
            hist_fcf_detail  = []
            for i in hist_idx:
                rev = get_val(rows,'매출액',i)
                op  = get_val(rows,'영업이익(발표)',i) or get_val(rows,'영업이익',i)
                # 영업이익 < 0 인 연도는 추정에서 제외
                if rev and op and rev > 0 and op > 0:
                    fcff = op*(1-TAX) + rev*DA - rev*CAPEX_R
                    margin = fcff / rev
                    hist_fcff_margin.append(margin)
                    hist_fcf_detail.append({'year': years[i], 'fcf': round(fcff), 'margin': round(margin*100,1), 'src': '영업이익추정', 'excluded': False})

        if len(hist_fcff_margin) < 2:
            return {"error": "과거 FCF 데이터 부족"}

        avg_fcff_margin = sum(hist_fcff_margin) / len(hist_fcff_margin)

        # ── 과거 매출 성장률 CAGR 계산 ──
        rev_hist = [get_val(rows, '매출액', i) for i in hist_idx]
        rev_hist = [v for v in rev_hist if v and v > 0]
        if len(rev_hist) >= 2:
            cagr = (rev_hist[-1] / rev_hist[0]) ** (1 / (len(rev_hist) - 1)) - 1
            # 마지막 1년 성장률
            last_g = rev_hist[-1] / rev_hist[-2] - 1
            # 비정상 고성장(사이클) 완화: CAGR과 마지막 성장률의 평균, 상한 20%
            rev_growth = min((cagr + last_g) / 2, 0.20)
        else:
            rev_growth = 0.05  # fallback 5%

        # ── 컨센서스 FCF 추정 (26E) ──
        # FnGuide 실적 기반 마진 × 컨센서스 매출 적용
        fcf_years = []
        for i in est_idx:
            label = years[i]
            rev_e = get_val(rows, '매출액', i)
            if rev_e:
                fcff_e = rev_e * avg_fcff_margin
                fcf_years.append((label, fcff_e, rev_e, '컨센서스'))

        if not fcf_years:
            return {"error": "컨센서스 FCF 추정 불가"}

        # ── 컨센서스 이후 연도 마진 기반 연장 (총 3개년이 될 때까지) ──
        TOTAL_PROJ_YEARS = 3
        last_label = fcf_years[-1][0]           # '2026/12(E)'
        last_rev_e = fcf_years[-1][2]           # 마지막 컨센서스 매출
        yr_m = re.search(r'(\d{4})/(\d{2})', last_label)
        base_year = int(yr_m.group(1)) if yr_m else 2026
        month_str = yr_m.group(2) if yr_m else '12'

        extra_needed = TOTAL_PROJ_YEARS - len(fcf_years)
        for k in range(1, extra_needed + 1):
            next_year  = base_year + k
            next_label = f"{next_year}/{month_str}(E)"
            rev_ext    = last_rev_e * (1 + rev_growth) ** k if last_rev_e else None
            if rev_ext:
                fcf_ext = rev_ext * avg_fcff_margin
                fcf_years.append((next_label, fcf_ext, rev_ext, '마진추정'))

        if r <= g_terminal:
            return {"error": f"할인율({r*100:.1f}%)이 터미널성장률({g_terminal*100:.1f}%)보다 낮음"}

        # ── 발행주식수 취득 (우선순위: 네이버 테이블 → EPS/순이익 역산 → KRX) ──
        shares_k = None

        # ① 네이버 테이블 '발행주식수' 행 (단위: 주)
        share_row = rows.get('발행주식수', [])
        for i in reversed(hist_idx):
            v = safe_float(share_row[i]) if i < len(share_row) else None
            if v and v > 1e6:
                shares_k = v / 1000   # 주 → 천주
                break

        # ② EPS / 당기순이익 역산 (흑자 연도 우선)
        #    주식수(주) = 순이익(억원) × 1e8 / EPS(원/주)
        if not shares_k:
            for i in reversed(hist_idx):
                eps = get_val(rows, 'EPS', i)
                net = get_val(rows, '당기순이익', i)
                if eps and net and abs(eps) > 100 and net > 0:
                    shares_k = (net * 1e8 / abs(eps)) / 1000  # 주 → 천주
                    break

        # ③ FinanceDataReader KRX 상장주식수
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
            shares_k = 1000000  # 1억주 중립 fallback

        def pv_to_price(pv_total):
            return pv_total * 1e8 / (shares_k * 1e3)

        def diff_str(fv):
            if not current_price: return None, None
            d = fv - current_price
            return f"{d:+,.0f}", f"{d/current_price*100:+.1f}"

        pv_fcfs = []
        cumulative_pv = 0
        for t_idx, (label, fcf_e, _rev, src) in enumerate(fcf_years):
            n = t_idx + 1
            pv = fcf_e / (1+r)**n
            cumulative_pv += pv
            tv_n    = fcf_e * (1+g_terminal) / (r-g_terminal)
            pv_tv_n = tv_n / (1+r)**n
            total_pv_n = cumulative_pv + pv_tv_n
            fv_n = pv_to_price(total_pv_n)
            d, dp = diff_str(fv_n)
            # 마진추정 연도는 레이블에 * 표시
            display_label = f"{label}*" if src == '마진추정' else label
            pv_fcfs.append({
                "year": display_label, "fcf": round(fcf_e), "pv": round(pv),
                "pv_tv": round(pv_tv_n), "total_pv": round(total_pv_n),
                "fair_value": f"{fv_n:,.0f}", "diff": d, "diff_pct": dp,
            })

        return {
            "avg_fcff_margin": round(avg_fcff_margin*100, 1),
            "avg_tax_rate":    22.0,
            "g_terminal":      round(g_terminal*100, 1),
            "r":               round(r*100, 2),
            "rev_growth":      round(rev_growth*100, 1),
            "shares":          round(shares_k * 1000 / 1e6, 1),  # 백만주 단위
            "hist_fcf":        hist_fcf_detail,   # 과거 FCF 상세
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

        soup = naver_data.get('soup')  # main.naver soup 재사용

        current_price = get_current_price(stock_code)
        rf   = get_risk_free_rate()
        beta = get_beta_wisereport(stock_code)
        r_value = rf + beta * 0.05

        # 증권사 목표가
        tp_data = get_target_prices(stock_code, naver_soup=soup)

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
            "dcf":   calc_dcf(naver_data, r_value, current_price, stock_code=stock_code),
            "band":  calc_valuation_band(naver_data, current_price),
            "tp":    tp_data,
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
