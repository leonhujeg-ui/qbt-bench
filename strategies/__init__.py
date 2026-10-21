"""内置策略注册表：6 个策略，每个参数空间 >= 4 维。"""
from strategies.ma_cross import MACross
from strategies.bollinger import BollingerReversion
from strategies.donchian import DonchianBreakout
from strategies.rsi_reversion import RSIReversion
from strategies.grid import GridTrading
from strategies.trend_stop import TrendFollowStop

REGISTRY = {s.name: s for s in (MACross, BollingerReversion, DonchianBreakout, RSIReversion, GridTrading, TrendFollowStop)}

__all__ = ["REGISTRY", "MACross", "BollingerReversion", "DonchianBreakout", "RSIReversion", "GridTrading",
           "TrendFollowStop"]
