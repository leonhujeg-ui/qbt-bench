"""五种 USDT 永续 REST 方言：每个模块一个 Dialect 子类，把各家报文翻译成 engine 的规范模型再翻回去。"""
from qdexsim.dialects import binance, bitget, bitunix, bybit, okx

ALL = {d.NAME: d for d in (binance.Binance, bybit.Bybit, okx.OKX, bitget.Bitget, bitunix.Bitunix)}


def make_all(creds=None):
    """→ {name: Dialect 实例}；creds = {name: {"api_key", "secret", "passphrase"}}，缺省用各方言的 DEFAULT_CREDS。"""
    creds = creds or {}
    return {name: cls(creds.get(name)) for name, cls in ALL.items()}
