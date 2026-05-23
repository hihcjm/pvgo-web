import sys
import os

# 현재 경로 추가 (모듈 임포트용)
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from crawler import FnGuideCrawler
from processor import FinancialProcessor
from valuation import DCFValuator

def main():
    print("="*60)
    print("  Aswath Damodaran Style K-Stock DCF Pipeline")
    print("="*60)
    
    stock_code = input("분석할 종목코드 6자리를 입력하세요 (예: 005930): ").strip()
    if not stock_code:
        stock_code = "005930" # 삼성전자 기본값
        
    try:
        # 1. 크롤링
        print(f"\n[1/4] 데이터 크롤링 중... (종목코드: {stock_code})")
        crawler = FnGuideCrawler(stock_code)
        is_df, bs_df, cf_df = crawler.get_financial_statements()
        price, shares = crawler.get_market_data()
        
        # 2. 전처리
        print("[2/4] 재무 데이터 전처리 및 K-IFRS 매핑 중...")
        processor = FinancialProcessor()
        fin_data = processor.process(is_df, bs_df, cf_df)
        
        # 시가총액 정보 추가 (억원 단위로 변환)
        market_cap_won = (price * shares) / 1e8
        fin_data['market_cap_won'] = market_cap_won
        
        # 3. 밸류에이션
        print("[3/4] 다모다란 모델 기반 가치 평가 수행 중...")
        # 기본값 설정: rf=3.5%, ERP=7.5% (Base 5% + Korea CRP 2.5%)
        valuator = DCFValuator(fin_data, rf=0.035, erp=0.075, beta=1.0)
        results = valuator.run_valuation()
        
        # 4. 결과 출력
        print("[4/4] 최종 분석 결과:")
        
        intrinsic_value_total = results['equity_value'] * 1e8 # 억원 -> 원
        intrinsic_per_share = intrinsic_value_total / shares
        
        print("-" * 40)
        print(f"현재 주가:      {price:,.0f} 원")
        print(f"적정 주가(DCF): {intrinsic_per_share:,.0f} 원")
        print(f"WACC:           {results['wacc']*100:.2f} %")
        print(f"고성장기 성장률: {results['growth_stage1']*100:.2f} %")
        print(f"영구 성장률:     {results['terminal_g']*100:.2f} %")
        print("-" * 40)
        
        upside = (intrinsic_per_share / price - 1) * 100
        status = "Undervalued (저평가)" if upside > 10 else "Overvalued (고평가)"
        if -10 <= upside <= 10: status = "Fair Value (적정)"
        
        print(f"결론: {status} (괴리율: {upside:+.2f}%)")
        print("-" * 40)

    except Exception as e:
        print(f"\n[오류 발생] {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
