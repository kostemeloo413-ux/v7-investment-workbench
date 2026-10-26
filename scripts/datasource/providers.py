# -*- coding: utf-8 -*-
"""
scripts/datasource/providers.py
统一行情/新闻数据源适配层（阶段一改造新增）

层级策略：
- 历史日线/财务：akshare 为主源（免费、全市场），落 SQLite（db.py）；
- 盘中实时行情：腾讯 qt.gtimg.cn 兜底主力（家庭宽带IP友好、毫秒级）；
- 7x24 快讯   ：新浪财经直播接口；
- tushare pro 预留同签名适配位：将来数据规模上量或免费源限流加剧，
  只需在 .env 配 TUSHARE_TOKEN 并把 PRIMARY_DAILY_SOURCE 换成 'tushare'，
  上层调用方零改动。

所有函数失败时返回空结构并打印明确告警——宁可诚实空白，不掺假数据。
"""

import os
import sys
import json
import time
import datetime
import urllib.request

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import runtime_config as rc
from datasource import db as history_db
from datasource.completeness import (
    collect_target_date,
    persist_universe_snapshot,
    load_universe_snapshot,
    build_universe_snapshot,
    evidence_path,
    universe_snapshot_path,
    normalize_trade_date,
    validate_target_rows,
)

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

_ak = None


def _akshare():
    global _ak
    if _ak is None:
        import akshare as ak
        _ak = ak
    return _ak


def to_tencent_symbol(code):
    """600519 -> sh600519；000001 -> sz000001；已带前缀原样返回。"""
    s = str(code).strip()
    if s.lower().startswith(('sh', 'sz', 'bj')):
        return s
    if s.startswith(('600', '601', '603', '605')):
        return f'sh{s}'
    return f'sz{s}'


def to_pure_code(symbol):
    """sh600519 -> 600519"""
    return str(symbol).replace('sh', '').replace('sz', '').replace('bj', '')


# ---------------------------------------------------------------- 实时行情
def get_realtime_quotes(symbols):
    """腾讯实时行情（与既有 fetch_market_quotes 同一解析逻辑，抽出来共用）。"""
    syms = [to_tencent_symbol(s) for s in symbols if s]
    if not syms:
        return {}
    url = ('https://qt.gtimg.cn/q=' + ','.join(syms) +
           '&_=' + str(int(datetime.datetime.now().timestamp() * 1000)))
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=6) as resp:
                content = resp.read().decode('gbk', errors='ignore')
            quotes = {}
            for line in content.strip().split(';'):
                line = line.strip()
                if not line or '=' not in line:
                    continue
                var_name = line.split('=')[0].strip()
                raw_str = line.split('"')[1]
                arr = raw_str.split('~')
                if len(arr) > 32:
                    sym = var_name.replace('v_', '')
                    quotes[sym] = {
                        'name': arr[1],
                        'code': arr[2],
                        'price': float(arr[3]) if arr[3] else 0.0,
                        'pre_close': float(arr[4]) if arr[4] else 0.0,
                        'open': float(arr[5]) if arr[5] else 0.0,
                        'change': float(arr[31]) if arr[31] else 0.0,
                        'pct': float(arr[32]) if arr[32] else 0.0,
                        'volume': float(arr[36]) if len(arr) > 36 and arr[36] else 0.0,
                        'amount': float(arr[37]) if len(arr) > 37 and arr[37] else 0.0,
                    }
            if quotes:
                return quotes
        except Exception as e:
            print(f'[providers] 腾讯行情第{attempt + 1}次尝试失败: {e}')
            time.sleep(1)
    print('[providers] 警告: 本轮实时行情全部拉取失败，返回空结果')
    return {}


# ---------------------------------------------------------------- 日线历史
def _daily_from_tencent(symbol, start=None, end=None, count=640):
    """腾讯日K（fqkline 前复权）· 分页版：
    接口单次最多约640根，通过带起止日期的参数逐段往回翻页，可拉满5年。
    返回按日期升序去重后的 list[dict]。"""
    sym = to_tencent_symbol(symbol)
    today = datetime.date.today()
    # 注意：该接口日期参数必须是 yyyy-mm-dd 短横线格式，否则返回空列表
    end_d = (end or today.strftime('%Y-%m-%d')).replace('_', '-')
    start_d = (start or (today - datetime.timedelta(days=365 * 5)).strftime('%Y-%m-%d')).replace('_', '-')
    rows, seen = [], set()
    page_end = end_d
    for _page in range(5):                      # 最多5页(≈3200根/约12年)保险上限
        url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
               f'?param={sym},day,{start_d},{page_end},{count},qfq')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                   'Referer': 'https://gu.qq.com/'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            j = json.loads(resp.read().decode('utf-8'))
        node = j['data'][sym]
        key = 'qfqday' if 'qfqday' in node else 'day'
        page_rows = node.get(key, [])
        if not page_rows:
            break
        prev_close = None
        for item in page_rows:
            date_s, open_s, close_s, high_s, low_s, vol_s = item[:6]
            if date_s in seen:
                continue
            seen.add(date_s)
            o, c = float(open_s), float(close_s)
            h, l = float(high_s), float(low_s)
            v = float(vol_s) * 100              # 手 → 股
            amt_est = v * (o + h + l + c) / 4
            pct = round((c / prev_close - 1) * 100, 4) if prev_close else 0.0
            rows.append({'trade_date': date_s.replace('-', ''), 'open': o, 'high': h,
                         'low': l, 'close': c, 'volume': v,
                         'amount': round(amt_est, 0), 'pct_chg': pct})
            prev_close = c
        earliest = min(item[0] for item in page_rows)   # 原始行是 [date,open,close,high,low,vol]
        if earliest <= start_d or len(page_rows) < count:
            break                               # 已到目标起点或接口无更早数据
        page_end = (datetime.datetime.strptime(earliest, '%Y-%m-%d')
                    - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    rows.sort(key=lambda r: r['trade_date'])
    return rows


def _daily_from_akshare(pure, start_d, end_d):
    """akshare 新浪日线备源（stock_zh_a_daily，走 finance.sina.com.cn）。"""
    df = _akshare().stock_zh_a_daily(
        symbol=(('sh' if pure.startswith('6') else 'sz') + pure),
        start_date=start_d.replace('-', ''), end_date=end_d.replace('-', ''),
        adjust='qfq')
    rows = []
    if df is not None and len(df):
        import pandas as pd
        for _, r in df.iterrows():
            rows.append({
                'trade_date': str(pd.Timestamp(r['date']))[:10].replace('-', ''),
                'open': round(float(r['open']), 4), 'high': round(float(r['high']), 4),
                'low': round(float(r['low']), 4), 'close': round(float(r['close']), 4),
                'volume': float(r['volume']), 'amount': float(r.get('amount', 0) or 0),
                'pct_chg': round(float(r.get('pct_chg', 0) or 0), 4),
            })
    return rows


def get_daily_history(code, start=None, end=None, adjust='qfq', return_metadata=False,
                      target_trade_date=None):
    """
    取日线并写 SQLite。数据源降级链：
      腾讯 fqkline(主源,分页可拉满5年) → akshare新浪(备源) → tushare pro(.env配token时)
    备注：东财 push2his 接口在部分家用网络被断连，不再作为默认源。
    """
    pure = to_pure_code(code)
    target_trade_date = normalize_trade_date(target_trade_date) if target_trade_date else None
    rows, source = [], None
    provider_attempts = []

    def accept_provider_rows(candidate_rows, candidate_source):
        if not target_trade_date:
            return candidate_rows
        target_rows = []
        for row in candidate_rows or []:
            try:
                if normalize_trade_date(row.get('trade_date')) == target_trade_date:
                    target_rows.append(row)
            except (AttributeError, TypeError, ValueError):
                continue
        valid, _ = validate_target_rows(pure, target_trade_date, target_rows,
                                        require_source=False)
        if not valid:
            # A provider returning non-empty historical rows without the target
            # date is a provider miss, not a success.  The caller must continue
            # through the fallback chain.
            print(f'[providers] {candidate_source} {pure} 缺少目标日 {target_trade_date}，进入备用源')
            return []
        return target_rows

    # 1) 腾讯主源（分页翻满历史）
    provider_attempts.append('tencent-qfq')
    try:
        candidate = _daily_from_tencent(pure, start=start, end=end)
        rows = accept_provider_rows(candidate, 'tencent-qfq')
        source = 'tencent-qfq' if rows else None
    except Exception as ex:
        print(f'[providers] 腾讯日线 {pure} 失败: {ex}')

    # 2) 新浪备源
    if not rows:
        end_d = end or datetime.date.today().strftime('%Y%m%d')
        start_d = start or (datetime.date.today() - datetime.timedelta(days=365 * 5)).strftime('%Y%m%d')
        provider_attempts.append('sina-qfq')
        try:
            candidate = _daily_from_akshare(pure, start_d, end_d)
            rows = accept_provider_rows(candidate, 'sina-qfq')
            if rows:
                source = 'sina-qfq'
        except Exception as ex:
            print(f'[providers] akshare新浪日线 {pure} 失败: {ex}')

    # 3) tushare 付费位
    if not rows:
        token = rc.ENV.get('TUSHARE_TOKEN', '')
        if token:
            provider_attempts.append('tushare')
            try:
                import tushare as ts
                pro = ts.pro_api(token)
                ts_code = pure + ('.SH' if pure.startswith('6') else '.SZ')
                df = pro.daily(ts_code=ts_code,
                               start_date=start or '', end_date=end or '')
                candidate = [{
                    'trade_date': str(r['trade_date']).replace('-', ''),
                    'open': float(r['open']), 'high': float(r['high']),
                    'low': float(r['low']), 'close': float(r['close']),
                    'volume': float(r['vol']) * 100,
                    'amount': float(r['amount']) * 1000,
                    'pct_chg': float(r['pct_chg']),
                } for _, r in df.iterrows()]
                rows = accept_provider_rows(candidate, 'tushare')
                if rows:
                    source = 'tushare'
            except Exception as te:
                print(f'[providers] tushare {pure} 也失败: {te}')

    if rows:
        history_db.upsert_klines(rows, pure, source=source)
    else:
        print(f'[providers] {pure} 无任何可用日线数据（诚实返回空）')
    if return_metadata:
        return {
            'rows': rows,
            'source': source,
            'provider_attempts': provider_attempts,
        }
    return rows


def sync_universe_daily(universe_codes, batch_pause_sec=0.35, max_stocks=None, resume=True,
                        target_trade_date=None, completeness_root=None,
                        universe_snapshot=None, max_attempts=2):
    """
    全池日线增量同步：为 build_stock_universe 产出的主板池补齐/更新历史日线。
    - 断点续传：sync_meta 里记录 last_synced_index，中断后从断点继续（resume=True）
    - 批次间暂停防限流；3103 家全跑一遍约 20~30 分钟，安排在收盘后的低峰任务
    返回 dict(成功数/失败数/断点)。
    """
    if target_trade_date is not None:
        if not completeness_root or not isinstance(universe_snapshot, dict):
            raise ValueError('G6 target-date sync requires completeness_root and universe_snapshot')
        snapshot_path = universe_snapshot_path(completeness_root, target_trade_date)
        if not os.path.exists(snapshot_path):
            persist_universe_snapshot(snapshot_path, universe_snapshot)
        else:
            existing = load_universe_snapshot(snapshot_path, target_trade_date)
            if existing.get('expected_codes_sha256') != universe_snapshot.get('expected_codes_sha256') or \
                    existing.get('universe_sha256') != universe_snapshot.get('universe_sha256'):
                raise ValueError('G6 frozen universe identity mismatch')
        return collect_target_date(
            universe_snapshot,
            completeness_root,
            fetch_history=lambda code, trade_date: get_daily_history(
                code, start=trade_date, end=trade_date, return_metadata=True,
                target_trade_date=trade_date),
            read_target_rows=lambda code, trade_date: history_db.get_kline(
                code, start=trade_date, end=trade_date, include_source=True),
            max_attempts=max_attempts,
            retry_delay_sec=batch_pause_sec,
            trading_day_fn=rc.is_trading_day,
        )

    done = ok_cnt = fail_cnt = 0
    start_idx = int(history_db.get_meta('universe_sync_idx', '0') or 0) if resume else 0
    total = len(universe_codes)
    for i, code in enumerate(universe_codes):
        if i < start_idx:
            continue
        if max_stocks and done >= max_stocks:
            break
        try:
            n_new = get_daily_history(code)
            if n_new:
                ok_cnt += 1
            else:
                fail_cnt += 1
        except Exception:
            fail_cnt += 1
        history_db.set_meta('universe_sync_idx', i + 1)
        done += 1
        if done % 50 == 0:
            print(f'[providers] 全池日线同步进度 {i + 1}/{total} (成功{ok_cnt}/失败{fail_cnt})')
            time.sleep(batch_pause_sec * 5)
        time.sleep(batch_pause_sec)
    finished = (start_idx + done) >= total
    if finished:
        history_db.set_meta('universe_sync_idx', '0')
        history_db.set_meta('universe_last_full_sync', rc.now_str())
    return {'done': done, 'ok': ok_cnt, 'fail': fail_cnt, 'finished': finished}


# ---------------------------------------------------------------- 新闻快讯
def get_flash_news(page_size=20, timeout=5):
    """新浪财经 7x24 快讯。只返回真实抓到的内容；失败返回空列表。"""
    import re
    items = []
    try:
        url = ('https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size='
               f'{page_size}&zhibo_id=152')
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res = json.loads(resp.read().decode('utf-8'))
        feed_list = (res.get('result', {}).get('data', {}).get('feed', {})
                     .get('list', []))
        for item in feed_list:
            rich_text = item.get('rich_text', '')
            clean_text = re.sub(r'<[^>]+>', '', rich_text).strip()
            if len(clean_text) > 15:
                items.append({
                    'title': clean_text[:80],
                    'content': clean_text,
                    'time': item.get('create_time', rc.now_str()),
                    'source': '新浪财经 7x24',
                    'url': item.get('docurl', '') or 'https://finance.sina.com.cn/7x24/',
                })
        if not items:
            print('[providers] 7x24快讯拉到0条（接口可能限流），如实返回空')
    except Exception as e:
        print(f'[providers] 7x24快讯拉取失败: {e}（如实返回空）')
    return items


def archive_news(items):
    """新闻批量入库（去重），供研判层取材与历史分析。"""
    return history_db.insert_news(items)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='数据源自测')
    ap.add_argument('--quotes', action='store_true', help='拉一次指数实时行情')
    ap.add_argument('--news', action='store_true', help='拉一次7x24快讯并入库存档')
    ap.add_argument('--kline', default='', help='拉某只股票5年日线入库, 如 --kline 600519')
    args = ap.parse_args()

    history_db.init_db()
    if args.quotes:
        q = get_realtime_quotes(['sh000001', 'sz399006'])
        print(json.dumps(q, ensure_ascii=False, indent=2)[:500])
    if args.news:
        ns = get_flash_news(page_size=10)
        total, new = archive_news([
            {'title': n['title'], 'content': n['content'], 'time': n['time'],
             'source': n['source'], 'url': n['url']} for n in ns])
        print(f'快讯抓到 {total} 条，入库 {new} 条新记录')
    if args.kline:
        rows = get_daily_history(args.kline)
        print(f'{args.kline} 入库 {len(rows)} 根日K，首末:',
              rows[0]['trade_date'] if rows else '-', '~',
              rows[-1]['trade_date'] if rows else '-')
    print('当前库覆盖:', history_db.kline_coverage())
