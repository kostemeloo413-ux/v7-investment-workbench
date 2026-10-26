# -*- coding: utf-8 -*-
"""
scripts/pipeline/gongbu.py
工部 · Gong Bu（统一指标计算层）

定位：全系统唯一的技术指标算法来源。SMA/EMA/MACD/RSI/KDJ/ATR/BOLL 只在这里实现，
     回测引擎、选股扫描、博主契约判断全部调用本模块——同一个指标全系统只有一种算法，
     杜绝"回测算的 20MA 和选股判断的 20MA 口径不一致"。

用法（输入均为 pandas Series/列表，输出与输入同型）：
    import gongbu
    df['ma20'] = gongbu.sma(df['close'], 20)
    df['rsi14'] = gongbu.rsi(df['close'], 14)
    df['atr14'] = gongbu.atr(df['high'], df['low'], df['close'], 14)
"""

import pandas as pd


def sma(series, period):
    """简单移动平均：SMA(N) = close.rolling(N).mean()（含当日）。"""
    s = pd.Series(series)
    return s.rolling(period).mean()


def ema(series, period):
    """指数移动平均：EMA(N)，标准 2/(N+1) 权重递推。"""
    s = pd.Series(series)
    return s.ewm(span=period, adjust=False).mean()


def macd(close, fast=12, slow=26, signal=9):
    """MACD：返回 (dif, dea, hist)，hist = (dif - dea) * 2。"""
    c = pd.Series(close)
    dif = ema(c, fast) - ema(c, slow)
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


def rsi(close, period=14):
    """RSI：Wilder 简化版。口径与 backtest_engine 原实现完全一致（rs = gain/(loss+1e-9)）。"""
    c = pd.Series(close)
    delta = c.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))


def kdj(high, low, close, n=9, k_smooth=3, d_smooth=3):
    """KDJ：RSV → K → D → J。返回 (K, D, J)。"""
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    ll = l.rolling(n).min()
    hh = h.rolling(n).max()
    rsv = (c - ll) / (hh - ll).replace(0, 1e-12) * 100
    k = rsv.ewm(alpha=1 / k_smooth, adjust=False).mean()
    d = k.ewm(alpha=1 / d_smooth, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def atr(high, low, close, period=14):
    """ATR：真实波幅均值。TR = max(h-l, |h-pc|, |l-pc|)，ATR = TR.rolling(period).mean()。"""
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def boll(close, period=20, k=2):
    """布林带：返回 (mid, upper, lower)，mid=SMA，上下轨 = mid ± k*std(N)。"""
    c = pd.Series(close)
    mid = sma(c, period)
    std = c.rolling(period).std()
    upper = mid + k * std
    lower = mid - k * std
    return mid, upper, lower


if __name__ == '__main__':
    import numpy as np
    print('=== 工部指标层自检 ===')
    np.random.seed(42)
    close = pd.Series(np.random.randn(120).cumsum() + 100)
    high = close + 1.0
    low = close - 1.0

    print(f'SMA(20) 末值: {sma(close, 20).iloc[-1]:.4f}')
    print(f'EMA(12) 末值: {ema(close, 12).iloc[-1]:.4f}')
    dif, dea, hist = macd(close)
    print(f'MACD 末值: DIF={dif.iloc[-1]:.4f} DEA={dea.iloc[-1]:.4f} HIST={hist.iloc[-1]:.4f}')
    print(f'RSI(14) 末值: {rsi(close, 14).iloc[-1]:.4f}')
    k, d, j = kdj(high, low, close)
    print(f'KDJ 末值: K={k.iloc[-1]:.2f} D={d.iloc[-1]:.2f} J={j.iloc[-1]:.2f}')
    print(f'ATR(14) 末值: {atr(high, low, close, 14).iloc[-1]:.4f}')
    mid, up, lo = boll(close)
    print(f'BOLL 末值: mid={mid.iloc[-1]:.2f} up={up.iloc[-1]:.2f} lo={lo.iloc[-1]:.2f}')
    print('全部计算成功')
