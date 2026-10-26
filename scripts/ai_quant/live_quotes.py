"""Bind the existing Tencent batch adapter to AIQ's current-quote seam."""
from __future__ import annotations

import datetime
import math

from simulated_trading import fetch_prices, format_symbol, quote_is_usable

SHANGHAI = datetime.timezone(datetime.timedelta(hours=8))


def current_time():
    return datetime.datetime.now(SHANGHAI)


class TencentQuotesProvider:
    def __init__(self, symbols=(), *, fetcher=None, clock=None):
        self.fetcher = fetcher or fetch_prices
        self.clock = clock or current_time
        self.evidence = {}
        self.refresh(symbols)

    def refresh(self, symbols):
        keys = sorted({format_symbol(str(s)[-6:]) for s in symbols})
        if not keys:
            return
        requested = self.clock()
        fetched = self.fetcher(keys)
        received = self.clock()
        for key in keys:
            # A failed refresh invalidates an older quote; it cannot silently
            # keep yesterday's or a prior cycle's executable price.
            self.evidence.pop(key[-6:], None)
            if key not in fetched:
                continue
            q = dict(fetched[key])
            q.update(symbol=key[-6:], requested_at=requested.isoformat(),
                     received_at=received.isoformat(), price_unit='CNY/share',
                     previous_close_field='Tencent.raw[4]', suspension_status='UNKNOWN')
            try:
                fields = q['raw_payload'].split('~')
                q['previous_close'] = float(fields[4])
                q['volume_shares'] = float(fields[36]) * 100.0
                q['turnover_CNY'] = float(fields[37]) * 10000.0
                q['is_st'] = 'ST' in fields[1].upper() or '退' in fields[1]
                q['market_date'] = q['observed_at'][:10]
                q['suspension_status'] = ('TRADING_OBSERVED' if q['volume_shares'] > 0 else 'UNKNOWN_NO_TRADES')
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            self.evidence[key[-6:]] = q

    def valid_quote(self, symbol, *, now=None):
        pure = str(symbol)[-6:]
        checked = now or self.clock()
        q = self.evidence.get(pure)
        if not q or not quote_is_usable(q, format_symbol(pure), for_buy=True, now=checked):
            return None
        try:
            fields = q['raw_payload'].split('~')
            previous = q['previous_close']
            received = datetime.datetime.fromisoformat(q['received_at'])
            requested = datetime.datetime.fromisoformat(q['requested_at'])
            if (not math.isfinite(previous) or previous <= 0 or previous != float(fields[4])
                    or q['volume_shares'] <= 0 or not math.isfinite(q['volume_shares'])
                    or q['volume_shares'] != float(fields[36]) * 100.0
                    or q['turnover_CNY'] < 0 or not math.isfinite(q['turnover_CNY'])
                    or q['turnover_CNY'] != float(fields[37]) * 10000.0
                    or q['is_st'] or '\ufffd' in q['raw_payload']
                    or requested > received or received > checked
                    or (checked - received).total_seconds() > 90):
                return None
        except (ValueError, TypeError, KeyError):
            return None
        return q

    def final_guard(self, symbol, action, price, previous_close, trade_date):
        q = self.valid_quote(symbol)
        return bool(q and q['market_date'].replace('-', '') == trade_date
                    and q['price'] == price and q['previous_close'] == previous_close)

    def get(self, key, default=None):
        usable = {s:q for s in self.evidence if (q := self.valid_quote(s))}
        maps = {'quotes_map':{s:q['price'] for s,q in usable.items()},
                'yesterday_closes':{s:q['previous_close'] for s,q in usable.items()},
                'daily_bars_map':{}, 'suspended_set':[]}
        return maps.get(key, default)
