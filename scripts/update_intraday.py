# -*- coding: utf-8 -*-
"""
scripts/update_intraday.py
盘中 5 分钟高频增量更新脚本（轻量化，仅抓取大盘指数真实分时 + 行情最新价）
不触发行人/博主/模拟盘全量重算，节约 99% 计算资源。
建议配置自动化：交易日 09:35 ~ 15:00 每 5 分钟一次。
"""
import os
import sys
import io
import json
import datetime
import urllib.request

# Windows 终端编码保护
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(BASE, 'workbench', 'data.json')

# 腾讯实时行情多包接口（一次可拉 80 只）
SYMBOLS = ['sh000001', 'sz399001', 'sz399006', 'sh000688']
QT_URL = 'https://qt.gtimg.cn/q=' + ','.join(SYMBOLS)
INTRADAY_URLS = {
    'sh000001': 'http://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001',
    'sz399001': 'http://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sz399001',
    'sz399006': 'http://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sz399006',
    'sh000688': 'http://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000688',
}

US_URLS = {
    'usIXIC': 'https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC?interval=1m&range=1d',
    'usINX':  'https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1m&range=1d',
}

HEADERS_QT = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'}
HEADERS_YAHOO = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': '*/*',
    'Origin': 'https://finance.yahoo.com',
    'Referer': 'https://finance.yahoo.com/',
}


def now_str():
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def fetch_qt_quotes():
    """拉取 4 大 A 股指数最新行情（GBK 编码）"""
    out = {}
    try:
        req = urllib.request.Request(QT_URL + '&_=' + str(int(datetime.datetime.now().timestamp()*1000)), headers=HEADERS_QT)
        with urllib.request.urlopen(req, timeout=4) as resp:
            raw = resp.read().decode('gbk', errors='ignore')
        for line in raw.strip().split(';'):
            line = line.strip()
            if not line or '="' not in line:
                continue
            var = line.split('=')[0].strip().replace('v_', '')
            payload = line.split('="', 1)[1].rstrip('"')
            arr = payload.split('~')
            if len(arr) >= 40:
                out[var] = {
                    'price': float(arr[3] or 0),
                    'change': float(arr[31] or 0),
                    'pct': float(arr[32] or 0),
                }
    except Exception as e:
        print(f'[行情警告] {e}')
    return out


def fetch_a_share_intraday():
    """拉取 4 大 A 股指数真实 1 分钟级分时"""
    out = {}
    for sym, url in INTRADAY_URLS.items():
        try:
            req = urllib.request.Request(url, headers=HEADERS_QT)
            with urllib.request.urlopen(req, timeout=4) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
            j = json.loads(raw)
            inner = j.get('data', {}).get(sym, {}).get('data', {})
            points = []
            for line in inner.get('data', []):
                parts = line.strip().split()
                if len(parts) >= 2:
                    points.append([parts[0], float(parts[1])])
            if points:
                out[sym] = {
                    'date': inner.get('date', datetime.datetime.now().strftime('%Y%m%d')),
                    'points': points,
                    'fetched_at': now_str()
                }
        except Exception:
            continue
    return out


def fetch_us_index_intraday():
    """拉取美股 1 分钟级真实分时（Yahoo Finance）"""
    out = {}
    for sym, url in US_URLS.items():
        try:
            req = urllib.request.Request(url, headers=HEADERS_YAHOO)
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
            j = json.loads(raw)
            chart = j.get('chart', {})
            if chart.get('error') or not chart.get('result'):
                continue
            r = chart['result'][0]
            ts_list = r.get('timestamp', [])
            quote = r.get('indicators', {}).get('quote', [{}])[0]
            closes = quote.get('close', [])
            if not ts_list or len(closes) != len(ts_list):
                continue
            points = []
            for ts, cp in zip(ts_list, closes):
                if cp is None:
                    continue
                bj_dt = datetime.datetime.fromtimestamp(ts, datetime.timezone(datetime.timedelta(hours=8)))
                points.append([f'{bj_dt.hour:02d}{bj_dt.minute:02d}', round(cp, 2)])
            if points:
                out[sym] = {
                    'date': datetime.datetime.now().strftime('%Y%m%d'),
                    'points': points,
                    'fetched_at': now_str(),
                    'source': 'yahoo-1m-real'
                }
        except Exception:
            continue
    return out


def embed_data_into_html(full_data):
    """将最新全量数据安全注入 workbench/index.html 和根目录 index.html 的 DEFAULT_EMBEDDED_DATA"""
    paths_to_update = [
        os.path.join(BASE, 'workbench', 'index.html'),
        os.path.join(BASE, 'index.html')
    ]
    json_str = json.dumps(full_data, ensure_ascii=False, indent=2)
    start_tag = 'const DEFAULT_EMBEDDED_DATA ='
    end_tag = 'let workbenchData = DEFAULT_EMBEDDED_DATA;'

    for path in paths_to_update:
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                html = f.read()

            start_pos = html.find(start_tag)
            end_pos = html.find(end_tag, start_pos)

            if start_pos != -1 and end_pos != -1:
                new_html = html[:start_pos] + 'const DEFAULT_EMBEDDED_DATA = ' + json_str + ';\n    ' + html[end_pos:]
                if new_html != html:
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(new_html)
        except Exception as e:
            print(f"注入 {path} 出错: {e}")
    return True


def main():
    print(f'[{now_str()}] 🚀 启动盘中 5 分钟高频增量更新（仅指数分时 + 最新价）...')
    # 1. 拉取最新指数实时行情
    qt_quotes = fetch_qt_quotes()
    print(f'  [A 股指数] 拉取 {len(qt_quotes)} 个最新价')
    # 2. 拉取真实分时数据
    a_intra = fetch_a_share_intraday()
    us_intra = fetch_us_index_intraday()
    print(f'  [分时] A股 {len(a_intra)} 个 + 美股 {len(us_intra)} 个真实 1 分钟级')
    # 3. 合并增量到 data.json
    if not os.path.exists(DATA_PATH):
        print(f'  ❌ {DATA_PATH} 不存在，请先运行 auto_update_hotspots.py 初始化')
        return
    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    # 3.1 增量更新指数分时
    if 'index_intraday' not in data:
        data['index_intraday'] = {}
    data['index_intraday'].update(a_intra)
    data['index_intraday'].update(us_intra)
    # 3.2 增量更新指数最新行情卡片（大盘 overview）
    if 'market_overview' not in data:
        data['market_overview'] = {}
    overview_map = {
        'sh000001': '上证指数',
        'sz399001': '深证成指',
        'sz399006': '创业板指',
        'sh000688': '科创50',
        'usIXIC': '纳斯达克',
        'usINX': '标普500',
    }
    code_map = {
        'sh000001': '000001.SH',
        'sz399001': '399001.SZ',
        'sz399006': '399006.SZ',
        'sh000688': '000688.SH',
        'usIXIC': 'IXIC',
        'usINX': 'INX',
    }
    # A 股最新价：使用从腾讯实时接口拿到的
    for sym, q in qt_quotes.items():
        if sym not in overview_map:
            continue
        data['market_overview'][sym] = {
            'name': overview_map[sym],
            'code': code_map[sym],
            'price': str(q['price']),
            'change': f'{q["change"]:+.2f}',
            'pct': f'{q["pct"]:+.2f}%',
            'status': 'up' if q['change'] >= 0 else 'down',
        }
    # 美股：使用 Yahoo 分时最后一点作为当前价
    for sym, intra in us_intra.items():
        if not intra['points']:
            continue
        last_price = intra['points'][-1][1]
        first_price = intra['points'][0][1] if intra['points'] else last_price
        change = last_price - first_price
        pct = (change / first_price * 100) if first_price else 0
        data['market_overview'][sym] = {
            'name': overview_map[sym],
            'code': code_map[sym],
            'price': str(last_price),
            'change': f'{change:+.2f}',
            'pct': f'{pct:+.2f}%',
            'status': 'up' if change >= 0 else 'down',
        }
    # 3.3 更新时间戳
    data['updated_at'] = now_str()
    data['picks_updated_at'] = now_str()
    # 4. 落盘
    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # 5. 同步内嵌 HTML
    embed_data_into_html(data)
    print(f'  💾 已增量更新 {DATA_PATH} 并同步注入 HTML（大盘 overview + 真实分时）')


if __name__ == '__main__':
    main()