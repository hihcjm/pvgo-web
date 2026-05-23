import requests
import pandas as pd
from bs4 import BeautifulSoup
import re

class FnGuideCrawler:
    """
    FnGuide에서 재무제표(IS, BS, CF) 및 Naver에서 시세 데이터를 추출하는 모듈
    """
    def __init__(self, stock_code):
        self.stock_code = stock_code
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

    def get_financial_statements(self):
        """FnGuide에서 연간 재무제표 3종 세트 추출"""
        url = f"https://comp.fnguide.com/SVO2/ASP/SVD_Finance.asp?pGB=1&gicode=A{self.stock_code}"
        resp = requests.get(url, headers=self.headers)
        resp.encoding = 'utf-8'
        
        # pandas.read_html로 테이블 전체 파싱
        tables = pd.read_html(resp.text)
        
        # FnGuide 구조: 0: IS(연결), 2: BS(연결), 4: CF(연결)
        # (상황에 따라 인덱스가 다를 수 있으므로 계정 과목 확인 로직 권장)
        try:
            is_df = tables[0] # Income Statement
            bs_df = tables[2] # Balance Sheet
            cf_df = tables[4] # Cash Flow
            return is_df, bs_df, cf_df
        except IndexError:
            raise Exception("재무제표 테이블을 찾을 수 없습니다. 종목코드를 확인하세요.")

    def get_market_data(self):
        """Naver 금융에서 현재가 및 발행주식수 추출"""
        url = f"https://finance.naver.com/item/main.naver?code={self.stock_code}"
        resp = requests.get(url, headers=self.headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 현재가
        price_tag = soup.find('p', {'class': 'no_today'})
        price = int(price_tag.find('span', {'class': 'blind'}).text.replace(',', ''))
        
        # 발행주식수 (보통주 + 우선주)
        # FnGuide 혹은 Naver '기업분석' 섹션에서 더 정확히 가져올 수 있으나, 
        # 메인 페이지 '상장주식수' 활용 (우선주 포함 여부 주의)
        stock_table = soup.find('table', {'summary': '시가총액 정보'})
        tds = stock_table.find_all('td')
        shares = int(tds[2].text.strip().replace(',', ''))
        
        return price, shares

if __name__ == "__main__":
    # Test
    crawler = FnGuideCrawler("005930")
    is_df, bs_df, cf_df = crawler.get_financial_statements()
    price, shares = crawler.get_market_data()
    print(f"Price: {price}, Shares: {shares}")
    print(is_df.head())
