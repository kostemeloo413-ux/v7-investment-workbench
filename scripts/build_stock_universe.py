"""
A 股沪深纯正主板股票池构建与清洗器 (Mainboard Stock Universe Builder)
- 数据源：腾讯金融实时行情 (High Concurrency Multi-Batch API)
- 目标规模：2000~3000 家（实测覆盖 3100+ 家纯正主板核心资产）
- 硬性过滤规则：
  1. 剔除创业板 (300/301)
  2. 剔除科创板 (688)
  3. 剔除北交所/新三板 (8/4/920)
  4. 严格剔除 ST / *ST / 退市 / 摘牌股票
  5. 过滤停牌及无真实交易价格标的
- 输出文件：data/stock_universe_mainboard.json
"""

import sys
import os
import json
import time
import urllib.request
import concurrent.futures
from datetime import datetime

# Windows 终端编码保护
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(ROOT_DIR, 'data', 'stock_universe_mainboard.json')

def generate_candidate_codes():
    codes = []
    # 沪市主板: 600xxx, 601xxx, 603xxx, 605xxx
    for i in range(1000):
        codes.append(f'sh600{i:03d}')
    for i in range(1000):
        codes.append(f'sh601{i:03d}')
    for i in range(1000):
        codes.append(f'sh603{i:03d}')
    for i in range(600):
        codes.append(f'sh605{i:03d}')
        
    # 深市主板: 000xxx, 001xxx (001200~001399), 002xxx~003xxx (002001~003049)
    for i in range(1, 1000):
        codes.append(f'sz000{i:03d}')
    for i in range(1200, 1400):
        codes.append(f'sz00{i:04d}')
    for i in range(2001, 3050):
        codes.append(f'sz00{i:04d}')
        
    return codes

def fetch_chunk(chunk):
    url = 'https://qt.gtimg.cn/q=' + ','.join(chunk)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            content = resp.read().decode('gbk', errors='ignore')
            return content
    except Exception as e:
        return ''

def build_universe():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 🚀 启动 A 股主板大池构建与清洗引擎...")
    t0 = time.time()
    
    all_candidates = generate_candidate_codes()
    chunk_size = 80
    chunks = [all_candidates[i:i+chunk_size] for i in range(0, len(all_candidates), chunk_size)]
    
    valid_stocks = []
    st_count = 0
    delisted_count = 0
    seen_codes = set()
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(fetch_chunk, c) for c in chunks]
        for f in concurrent.futures.as_completed(futures):
            raw = f.result()
            if not raw:
                continue
            lines = raw.strip().split(';\n')
            for line in lines:
                if not line or '=\"\";' in line:
                    continue
                parts = line.split('~')
                if len(parts) > 40:
                    name = parts[1].strip()
                    code = parts[2].strip()
                    price = float(parts[3] or 0)
                    change_pct = float(parts[32] or 0)
                    vol = float(parts[6] or 0)
                    amount_wan = float(parts[37] or 0)
                    turnover = float(parts[38] or 0) if len(parts) > 38 and parts[38] else 0.0
                    pe = float(parts[39] or 0) if len(parts) > 39 and parts[39] else 0.0
                    mktcap_yi = float(parts[45] or 0) if len(parts) > 45 and parts[45] else 0.0
                    
                    if code in seen_codes:
                        continue
                    seen_codes.add(code)
                    
                    # 风险过滤：ST / *ST / 退市 / 摘牌
                    upper_name = name.upper()
                    if 'ST' in upper_name or '退' in upper_name or 'PT' in upper_name:
                        st_count += 1
                        continue
                        
                    # 过滤停牌无交易标的
                    if price <= 0:
                        delisted_count += 1
                        continue
                        
                    mkt = 'sh' if code.startswith(('600', '601', '603', '605')) else 'sz'
                    valid_stocks.append({
                        'code': code,
                        'name': name,
                        'market': mkt,
                        'symbol': f'{mkt}{code}',
                        'full_code': f'{mkt}{code}',
                        'latest_price': price,
                        'change_pct': change_pct,
                        'volume_hand': vol,
                        'amount_wan': amount_wan,
                        'turnover_ratio': turnover,
                        'pe': pe,
                        'mktcap_yi': mktcap_yi
                    })
                    
    valid_stocks.sort(key=lambda x: x['code'])
    duration = time.time() - t0
    
    payload = {
        "updated_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "total_count": len(valid_stocks),
        "filter_rules": {
            "include_mainboard_sh": ["600", "601", "603", "605"],
            "include_mainboard_sz": ["000", "001", "002", "003"],
            "exclude_chinext": ["300", "301"],
            "exclude_kcb": ["688"],
            "exclude_bse": ["8", "4", "920"],
            "exclude_st": True,
            "exclude_delisted": True
        },
        "stats": {
            "total_candidate_scanned": len(all_candidates),
            "st_filtered": st_count,
            "zero_price_filtered": delisted_count,
            "mainboard_clean": len(valid_stocks),
            "scan_duration_sec": round(duration, 2)
        },
        "stocks": valid_stocks
    }
    
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        
    print(f"==================================================")
    print(f"🎯 沪深纯正主板股票池构建完成: {OUTPUT_PATH}")
    print(f"⏱️ 耗时: {duration:.2f} 秒")
    print(f"📊 过滤统计:")
    print(f"   - 扫描候选代码: {payload['stats']['total_candidate_scanned']} 个")
    print(f"   - 🚫 过滤 ST / *ST / 退市股: {payload['stats']['st_filtered']} 家")
    print(f"   - 🚫 过滤停牌/无报价股: {payload['stats']['zero_price_filtered']} 家")
    print(f"   - ✨ 沪深纯正主板大池规模: {payload['stats']['mainboard_clean']} 家")
    print(f"==================================================")
    return payload

if __name__ == '__main__':
    build_universe()
