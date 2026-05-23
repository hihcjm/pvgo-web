import pandas as pd
import numpy as np
import re

class FinancialProcessor:
    """
    크롤링된 재무 데이터를 정규화하고 다모다란 모델용 변수로 매핑
    """
    def __init__(self):
        # 주요 계정과목 매핑 맵
        self.mapping = {
            'EBIT': '영업이익',
            'REVENUE': '매출액',
            'NET_INCOME': '당기순이익',
            'TAX_BEFORE': '법인세비용차감전계속사업이익',
            'TAX_EXP': '법인세비용',
            'DEPR': '감가상각비',
            'AMORT': '무형자산상각비',
            'CAPEX': '유형자산의취득',
            'CASH': '현금및현금성자산',
            'ST_INVEST': '단기금융상품', # 혹은 단기투자자산
            'TOTAL_DEBT': '총차입금', # FnGuide 재무상태표 하단에 별도 집계됨
            'TOTAL_EQUITY': '자본총계',
            'MINORITY_INTEREST': '비지배지분'
        }

    def clean_value(self, val):
        """쉼표 제거, 괄호 음수 처리, NaN 처리"""
        if pd.isna(val) or val == '-':
            return 0.0
        if isinstance(val, str):
            val = val.replace(',', '').strip()
            # (1,234) 형태의 음수 처리
            if val.startswith('(') and val.endswith(')'):
                val = '-' + val[1:-1]
        try:
            return float(val)
        except ValueError:
            return 0.0

    def process(self, is_df, bs_df, cf_df):
        """
        DataFrames에서 최신 연도(가장 오른쪽 컬럼) 데이터를 추출하여 딕셔너리로 반환
        FnGuide 데이터는 억원 단위인 경우가 많으므로 주의 (수치 보정 필요 시 적용)
        """
        data = {}
        
        # 컬럼 인덱스 설정 (보통 마지막 컬럼이 가장 최신 혹은 '2023/12' 형태)
        # FnGuide는 'IFRS(연결)' 컬럼이 첫번째고 그 뒤로 연도별 데이터
        latest_col = is_df.columns[-1]
        if '전년동기' in latest_col: # 분기 데이터 섞임 방지
            latest_col = is_df.columns[-2]

        def get_row_value(df, target_name):
            # 계정 과목 컬럼은 보통 첫번째 (0번)
            row = df[df.iloc[:, 0].str.contains(target_name, na=False)]
            if not row.empty:
                return self.clean_value(row[latest_col].values[0])
            return 0.0

        # 데이터 매핑 (단위: 억원 기준 그대로 추출)
        data['ebit'] = get_row_value(is_df, self.mapping['EBIT'])
        data['revenue'] = get_row_value(is_df, self.mapping['REVENUE'])
        data['net_income'] = get_row_value(is_df, self.mapping['NET_INCOME'])
        data['tax_before'] = get_row_value(is_df, self.mapping['TAX_BEFORE'])
        data['tax_exp'] = get_row_value(is_df, self.mapping['TAX_EXP'])
        
        data['depr_amort'] = get_row_value(cf_df, self.mapping['DEPR']) + \
                             get_row_value(cf_df, self.mapping['AMORT'])
        data['capex'] = abs(get_row_value(cf_df, self.mapping['CAPEX']))
        
        data['cash_st'] = get_row_value(bs_df, self.mapping['CASH']) + \
                          get_row_value(bs_df, self.mapping['ST_INVEST'])
        
        # 총차입금 (FnGuide는 재무상태표 하단에 계산되어 나오거나 부채항목 합산 필요)
        data['total_debt'] = get_row_value(bs_df, '총차입금') 
        data['equity'] = get_row_value(bs_df, self.mapping['TOTAL_EQUITY'])
        data['minority'] = get_row_value(bs_df, self.mapping['MINORITY_INTEREST'])

        # 유효세율 계산
        if data['tax_before'] > 0:
            data['eff_tax_rate'] = max(0.0, data['tax_exp'] / data['tax_before'])
            if data['eff_tax_rate'] > 0.5: data['eff_tax_rate'] = 0.24 # 이상치 처리
        else:
            data['eff_tax_rate'] = 0.24 # 한국 법인세율 상단 근사치

        return data
