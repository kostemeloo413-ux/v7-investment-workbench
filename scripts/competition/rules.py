# -*- coding: utf-8 -*-
"""
scripts/competition/rules.py
Phase 9: A 股统一规则裁判引擎（A-Share Rules Engine）

核心职责（《指南》第 1.12 & 15 节）：
作为 7 位选手（6博主 + 1个AI量化）共同遵守的公正“裁判员”，
杜绝任何策略产生脱离 A 股物理规则的虚假阿尔法：
1. T+1 刚性锁仓检验 (can_sell_today)：
   - 当天买入的股票数量纳入 position_quantity，但 available_quantity 当天严格为 0；
   - 只有跨过当前自然日且进入下一交易日，方可解锁 available_quantity；
2. 涨跌停成交可行性裁判 (can_buy / can_sell)：
   - 一字涨停封死 (open >= limit_up 且 low >= limit_up)：买入受阻，can_buy = False；
   - 一字跌停封死 (open <= limit_down 且 high <= limit_down)：卖出受阻，can_sell = False；
   - 涨跌停限制按主板 10%、ST 5% 严格计算；
3. 停牌状态保护 (is_suspended)：
   - 停牌标的不可买入、不可卖出，价格维持挂起，严禁当成 return=0 撮合；
4. 交易单位规范 (round_lot)：
   - 买入必须是 100 股（1手）的整数倍，向下取整；
5. 交易时段裁判 (is_market_trading_window)：
   - 必须处于 A 股合法交易时段窗口。
"""

import os
import sys
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc


class AShareRules:
    """A 股统一裁判规则工具类"""

    @staticmethod
    def get_price_limits(yesterday_close, is_st=False, board='MAIN'):
        """计算涨跌停价格（主板10%，ST为5%）"""
        ratio = 0.05 if is_st else (0.20 if board in ('GEM', 'STAR') else 0.10)
        yc = float(yesterday_close)
        limit_up = round(yc * (1.0 + ratio), 2)
        limit_down = round(yc * (1.0 - ratio), 2)
        return limit_up, limit_down

    @staticmethod
    def can_buy(current_price, yesterday_close, open_price=None, low_price=None, is_st=False, board='MAIN', is_suspended=False):
        """
        买入交易可行性裁判：
        - 停牌：不能买
        - 一字涨停板：买单排队无法成交
        """
        if is_suspended:
            return False, "标的停牌，禁止买入"

        limit_up, _ = AShareRules.get_price_limits(yesterday_close, is_st=is_st, board=board)
        cur_p = float(current_price)

        # 若现价已封死涨停
        if cur_p >= limit_up:
            # 若给出了 open 和 low，且 low 也等于 limit_up，为一字死板
            if open_price is not None and low_price is not None:
                if float(open_price) >= limit_up and float(low_price) >= limit_up:
                    return False, "一字涨停封死，买单无法成交"
            return False, "现价触及涨停板，禁止追高买入"

        return True, "允许买入"

    @staticmethod
    def can_sell(current_price, yesterday_close, open_price=None, high_price=None, is_st=False, board='MAIN', is_suspended=False):
        """
        卖出交易可行性裁判：
        - 停牌：不能卖
        - 一字跌停板：无法卖出
        """
        if is_suspended:
            return False, "标的停牌，无法卖出"

        _, limit_down = AShareRules.get_price_limits(yesterday_close, is_st=is_st, board=board)
        cur_p = float(current_price)

        if cur_p <= limit_down:
            if open_price is not None and high_price is not None:
                if float(open_price) <= limit_down and float(high_price) <= limit_down:
                    return False, "一字跌停封死，卖单无法撮合"

        return True, "允许卖出"

    @staticmethod
    def validate_t1_sell(position, current_trade_date):
        """
        T+1 刚性锁仓纪律检验：
        position 必须包含 buy_date ('YYYYMMDD' 或 'YYYY-MM-DD')
        当前交易日必须严格大于买入日
        """
        buy_d = str(position.get('buy_date', '')).replace('-', '')[:8]
        cur_d = str(current_trade_date).replace('-', '')[:8]
        if not buy_d or not cur_d:
            return False, 0, "缺少交易日期信息"

        # 若买入日等于当前日期，属于当天买入，可用股份必须为 0
        if cur_d <= buy_d:
            return False, 0, f"A股T+1约束：买入日({buy_d})当日不可卖出"

        avail_shares = int(position.get('shares', 0))
        if avail_shares <= 0:
            return False, 0, "无可用持仓股份"

        return True, avail_shares, "符合T+1可卖条件"

    @staticmethod
    def calculate_lot_shares(budget, buy_price_with_slippage):
        """
        最小交易单位（1手=100股）向下取整截断
        """
        if buy_price_with_slippage <= 0 or budget <= 0:
            return 0
        raw_shares = budget / buy_price_with_slippage
        lot_shares = int(raw_shares // 100) * 100
        return max(0, lot_shares)

    @staticmethod
    def is_valid_trade_window(current_time_str=None):
        """
        裁判交易时段窗口：
        默认连续竞价时段：09:37~11:28, 13:05~14:55
        """
        now = datetime.datetime.now()
        t_str = current_time_str or now.strftime('%H:%M')
        # 周末排除
        if now.weekday() >= 5 and current_time_str is None:
            return False, "非交易日(周末)"
        
        in_morning = ('09:37' <= t_str <= '11:28')
        in_afternoon = ('13:05' <= t_str <= '14:55')
        if in_morning or in_afternoon:
            return True, "处于合法连续竞价时段"
        return False, f"处于非交易窗口({t_str})"


if __name__ == '__main__':
    print("=== 测试 AShareRules 裁判规则 ===")
    # 1. 涨跌停计算
    up, down = AShareRules.get_price_limits(10.0, is_st=False)
    print(f"10元主板股票涨跌停: 上限={up}, 下限={down}")
    
    # 2. 涨停买入裁判
    cb, msg = AShareRules.can_buy(11.0, 10.0)
    print(f"涨停价买入裁判: {cb} ({msg})")

    # 3. T+1 裁判
    pos_today = {'symbol': '000001', 'shares': 500, 'buy_date': '20260905'}
    can_s, sh, reason = AShareRules.validate_t1_sell(pos_today, '20260905')
    print(f"当天买入当天卖出: can_sell={can_s}, 可卖={sh}股 ({reason})")

    pos_yesterday = {'symbol': '000001', 'shares': 500, 'buy_date': '20260904'}
    can_s2, sh2, reason2 = AShareRules.validate_t1_sell(pos_yesterday, '20260905')
    print(f"昨日买入今日卖出: can_sell={can_s2}, 可卖={sh2}股 ({reason2})")

    # 4. 手数计算
    lots = AShareRules.calculate_lot_shares(25000.0, 11.05)
    print(f"25000元买入11.05元股票: 计算手数={lots}股 (必须整百)")
