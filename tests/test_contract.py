"""qbt/CONTRACT.md 是机器可核对的契约：每张 `<!-- contract-table: NAME -->` 表一条判据，与代码逐项对照。

表里少一行、代码里少一个字段 / 常量 / 键、值对不上，对应判据就红；TestMeta 对每张表删一行后在临时副本上重跑证明这一点。
QBT_CONTRACT_PATH=<file> 让判据读另一份副本（演示删行必红时不必改真文件）。
"""
import ast
import inspect
import json
import math
import os
import re
import tempfile
import unittest

from qbt import broker as BK
from qbt import data as DT
from qbt import engine as EN
from qbt import errors as ER
from qbt import indicators as I
from qbt import metrics as M
from qbt import optimizer as O
from qbt import report as R
from qbt.broker import Broker, BrokerConfig, Order, Fill, Position, MARKET, LIMIT, STOP, ICEBERG, TAKE_PROFIT
from qbt.data import OHLCV, GenSpec, generate
from qbt.engine import Context, Engine, Strategy, LookaheadError, run_backtest
from qbt.errors import ConfigError, DataError, QBTError
from strategies import REGISTRY
from tests import test_indicators as _ind

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CONTRACT = os.path.join(REPO, "qbt", "CONTRACT.md")
MARK = re.compile(r"<!--\s*contract-table:\s*([a-z_]+)\s*-->")
TABLES = ("strategy_class", "strategy_init", "lifecycle", "context_api", "order_fields", "fill_fields",
          "position_fields", "trade_fields", "events", "result_attrs", "summary_keys", "order_types", "status",
          "signs", "config_fields", "error_classes", "raises", "ohlcv_columns", "validation", "csv",
          "genspec_fields", "metrics_keys", "report_files", "optimizer_keys", "indicators", "boundaries")
TOL = 1e-9
FLAT = [(100.0, 101.0, 99.0, 100.0, 1000.0)] * 12


def contract_path():
    return os.environ.get("QBT_CONTRACT_PATH") or DEFAULT_CONTRACT


def load_tables(path=None):
    """name -> 数据行列表（去掉表头与分隔行），每格 strip 并去掉首尾反引号。"""
    with open(path or contract_path(), encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    tables, i = {}, 0
    while i < len(lines):
        m = MARK.search(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        rows = []
        while j < len(lines) and lines[j].strip().startswith("|"):
            cells = lines[j].strip().strip("|").split("|")
            rows.append([c.strip().strip("`").strip() for c in cells])
            j += 1
        if len(rows) < 3:
            raise ValueError(f"contract table {m.group(1)} has no data rows")
        tables[m.group(1)] = rows[2:]
        i = j
    return tables


def bars(rows, symbol="S"):
    cols = list(zip(*rows))
    return OHLCV(symbol, list(range(len(rows))), *[list(c) for c in cols], bar_seconds=1).validate()


def broker(rows, **cfg):
    d = bars(rows)
    return Broker({d.symbol: d}, BrokerConfig(**cfg)), d


def resolve_test(ref):
    """'tests.mod.Class.test_x' -> 该测试方法存在；'test_contract.Class.test_x' 也接受。"""
    parts = ref.split(".")
    if parts[0] != "tests":
        parts = ["tests"] + parts
    mod = __import__(".".join(parts[:-2]), fromlist=["_"])
    cls = getattr(mod, parts[-2])
    return getattr(cls, parts[-1])


class Probe(Strategy):
    name = "probe"
    DEFAULTS = {"k": 5, "f": 0.5, "flag": True, "s": "x"}

    def on_bar(self, ctx, i):
        pass


class TestTables(unittest.TestCase):
    def test_every_table_present_exactly_once(self):
        self.assertEqual(sorted(load_tables()), sorted(TABLES))
        with open(contract_path(), encoding="utf-8") as fh:
            src = fh.read()
        for name in TABLES:
            self.assertEqual(len(re.findall(r"contract-table:\s*%s\s*-->" % name, src)), 1, name)
        for name, rows in load_tables().items():
            self.assertEqual(len({len(r) for r in rows}), 1, f"{name}: ragged rows")


class TestStrategyInterface(unittest.TestCase):
    def test_strategy_class(self):
        rows = load_tables()["strategy_class"]
        cls_rows = {r[0]: r for r in rows if r[1] == "类"}
        inst_rows = {r[0] for r in rows if r[1] == "实例"}
        self.assertEqual(set(cls_rows), {n for n in dir(Strategy) if not n.startswith("_")})
        self.assertEqual(inst_rows, set(vars(Strategy())))
        self.assertEqual(len(rows), len(cls_rows) + len(inst_rows))
        for name, r in cls_rows.items():
            obj = getattr(Strategy, name)
            if callable(obj):
                params = list(inspect.signature(obj).parameters)[1:]
                self.assertEqual(r[2], "(" + ", ".join(params) + ")", name)
            else:
                self.assertEqual(r[2], type(obj).__name__, name)
        self.assertEqual(Strategy.name, "base")
        with self.assertRaises(NotImplementedError):
            Strategy().on_bar(None, 0)

    def test_init_rules(self):
        rows = load_tables()["strategy_init"]
        self.assertEqual(len(rows), len({r[0] for r in rows}))
        self.assertEqual(len(rows), 11)
        data = {"SYN": generate(n=5, seed=1)}
        for rule, example, expected in rows:
            kw = json.loads(example)
            if expected == "ok":
                s = Probe(**kw)
                self.assertEqual(s.params, {**Probe.DEFAULTS, **{k: v for k, v in kw.items() if k != "symbol"}}, rule)
                if "symbol" in kw:
                    s.on_start(Context(data, None))
                    self.assertEqual(s.symbol, "SYN", rule)
            elif expected == "ConfigError(on_start)":
                s = Probe(**kw)
                with self.assertRaises(ConfigError, msg=rule):
                    s.on_start(Context(data, None))
            else:
                self.assertEqual(expected, "ConfigError", rule)
                with self.assertRaises(ConfigError, msg=rule):
                    Probe(**kw)

    def test_lifecycle(self):
        rows = {r[0]: r for r in load_tables()["lifecycle"]}
        self.assertEqual(set(rows), {"on_start", "on_bar", "on_end"})
        n, seen, fills_seen, tc = 6, {}, {}, self
        d = generate(n=n, seed=2, symbol="S", halt_prob=0.0)

        class Rec(Strategy):
            name = "rec"

            def on_start(self, ctx):
                super().on_start(ctx)
                seen["on_start"] = (ctx.index, ctx.broker.bar)
                with tc.assertRaises(LookaheadError):
                    ctx.data["S"].close[0]
                ctx.order("S", 1, 1.0, tag="start")

            def on_bar(self, ctx, i):
                seen.setdefault("on_bar", []).append((ctx.index, ctx.broker.bar, i))
                ctx.data["S"].close[i]
                if i + 1 < n:
                    with tc.assertRaises(LookaheadError):
                        ctx.data["S"].close[i + 1]
                    with tc.assertRaises(LookaheadError):
                        ctx.data["S"].close[: i + 2]
                fills_seen[i] = len(ctx.broker.fills)
                ctx.order("S", 1, 1.0, tag=f"bar{i}")

            def on_end(self, ctx):
                seen["on_end"] = (ctx.index, ctx.broker.bar)
                ctx.data["S"].close[n - 1]
                ctx.order("S", 1, 1.0, tag="end")
        r = run_backtest(d, Rec(), BrokerConfig(max_participation=1.0), strict=True)
        expect = {"-1": -1, "0": 0, "n-1": n - 1}
        self.assertEqual(seen["on_start"], (expect[rows["on_start"][2]], expect[rows["on_start"][3]]))
        self.assertEqual(seen["on_end"], (expect[rows["on_end"][2]], expect[rows["on_end"][3]]))
        self.assertEqual((rows["on_bar"][2], rows["on_bar"][3]), ("i", "i"))
        self.assertEqual(seen["on_bar"], [(i, i, i) for i in range(n)])
        by_tag = {f.tag: f.bar for f in r.fills}
        self.assertEqual(by_tag["start"], 0)                               # on_start 的单在 bar 0 开盘撮合
        self.assertEqual([by_tag[f"bar{i}"] for i in range(n - 1)], list(range(1, n)))   # on_bar(i) -> i+1
        self.assertNotIn(f"bar{n - 1}", by_tag)
        self.assertNotIn("end", by_tag)                                     # on_end 的单永不成交
        self.assertEqual([o.status for o in r.orders if o.tag in ("end", f"bar{n - 1}")], ["pending", "pending"])
        self.assertEqual(fills_seen[1], 2)                                  # 策略看到的成交已包含本根撮合结果

    def test_context_api(self):
        rows = load_tables()["context_api"]
        names = {r[0] for r in rows}
        props = {n for n, v in vars(Context).items() if isinstance(v, property)}
        methods = {n for n, v in vars(Context).items() if callable(v) and not n.startswith("_")}
        self.assertEqual(names, set(Context.__slots__) | props | methods)
        self.assertEqual(len(rows), len(names))
        for name, kind, sig, _ in rows:
            if kind == "方法":
                params = list(inspect.signature(getattr(Context, name)).parameters.values())[1:]
                self.assertEqual(sig, f"{name}({', '.join(str(p) for p in params)})", name)
                self.assertIn(name, methods)
            elif kind == "property":
                self.assertIn(name, props)
            else:
                self.assertEqual(kind, "属性", name)
                self.assertIn(name, Context.__slots__)
        self.assertEqual(sorted(props), ["cash", "equity"])


class TestStructures(unittest.TestCase):
    def _slots(self, table, cls):
        rows = load_tables()[table]
        self.assertEqual([int(r[0]) for r in rows], list(range(len(rows))))
        self.assertEqual([r[1] for r in rows], list(cls.__slots__))

    def test_order_fields(self):
        self._slots("order_fields", Order)
        b, _ = broker(FLAT)
        o = b.submit("S", -1, 2.0, LIMIT, price=101.0, expires=3, reduce_only=False, tag="t")
        self.assertEqual(list(o.to_dict()), list(Order.__slots__))
        self.assertEqual(o.to_dict(), {"id": 1, "symbol": "S", "side": -1, "qty": 2.0, "remaining": 2.0, "type": "limit",
                                       "price": 101.0, "display": None, "status": "pending", "created": 0,
                                       "expires": 3, "reduce_only": False, "tag": "t", "reason": ""})

    def test_fill_fields(self):
        self._slots("fill_fields", Fill)
        b, _ = broker(FLAT, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 2.0, tag="x")
        b.on_bar(1)
        f = b.fills[0].to_dict()
        self.assertEqual(list(f), list(Fill.__slots__))
        self.assertEqual((f["order_id"], f["symbol"], f["bar"], f["side"], f["qty"], f["liquidity"], f["tag"]),
                         (1, "S", 1, 1, 2.0, "taker", "x"))

    def test_position_fields(self):
        self._slots("position_fields", Position)
        p = Position("S")
        self.assertEqual((p.qty, p.avg_price, p.realized), (0.0, 0.0, 0.0))

    def test_trade_fields(self):
        keys = {r[0] for r in load_tables()["trade_fields"]}
        fills = [Fill(1, "S", 1, 1, 2.0, 100.0, 0.1, "taker", ""), Fill(2, "S", 3, -1, 2.0, 110.0, 0.1, "taker", "")]
        t = BK.round_trips(fills)
        self.assertEqual(len(t), 1)
        self.assertEqual(set(t[0]), keys)
        self.assertEqual(t[0], {"symbol": "S", "entry_bar": 1, "exit_bar": 3, "side": 1, "qty": 2.0, "entry_price": 100.0,
                                "exit_price": 110.0, "pnl": 20.0 - 0.2, "bars": 2})

    def test_events(self):
        rows = {r[0]: r for r in load_tables()["events"]}
        with open(BK.__file__, encoding="utf-8") as fh:
            in_src = set(re.findall(r'events\.append\(\("([a-z]+)"', fh.read()))
        self.assertEqual(set(rows), in_src)
        rows_ = [(100, 101, 99, 100, 1e9)] * 2 + [(100, 101, 50, 50, 1e9)] + [(50, 51, 49, 50, 1e9)] * 2
        b, _ = broker(rows_, max_leverage=3.0, maintenance_margin=0.2, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 2500.0)
        for i in range(1, 5):
            b.on_bar(i)
        self.assertEqual([e[0] for e in b.events], ["liquidation", "bankrupt"])
        for e in b.events:
            self.assertEqual(len(e), 3)
            self.assertEqual(rows[e[0]][1], f'("{e[0]}", bar, equity)')
            self.assertEqual((e[1], e[2]), (2, b.equity))

    def test_result_attrs_and_summary(self):
        attrs = {r[0] for r in load_tables()["result_attrs"]}
        keys = [r[0] for r in load_tables()["summary_keys"]]
        r = run_backtest(generate(n=200, seed=3), REGISTRY["ma_cross"](fast=5, slow=20), BrokerConfig(max_participation=1.0))
        self.assertEqual(set(vars(r)), attrs)
        self.assertEqual(list(r.summary()), keys)
        json.dumps(r.summary())
        self.assertEqual(r.summary()["fills"], len(r.fills))
        self.assertEqual(len(keys), len(set(keys)))


class TestOrdersAndStatus(unittest.TestCase):
    def test_order_types(self):
        rows = load_tables()["order_types"]
        self.assertEqual([r[0] for r in rows], list(BK.ORDER_TYPES))
        for r in rows:
            self.assertEqual(getattr(BK, r[1]), r[0])
        liquidity = {}
        for otype in BK.ORDER_TYPES:
            b, _ = broker(FLAT, max_participation=1.0)
            b.on_bar(0)
            kw = {} if otype == MARKET else {"price": 100.5 if otype in (STOP, LIMIT) else 99.5}
            if otype == ICEBERG:
                kw = {"price": 100.5, "display": 1.0}
            side = -1 if otype == TAKE_PROFIT else 1
            if otype == LIMIT:
                side = 1                                                   # 买限价 100.5：low 99 <= 100.5 触发
            b.submit("S", side, 1.0, otype, **kw)
            b.on_bar(1)
            self.assertEqual(len(b.fills), 1, otype)
            liquidity[otype] = b.fills[0].liquidity
        self.assertEqual(liquidity, {r[0]: r[3] for r in rows})

    def test_status_and_reasons(self):
        rows = load_tables()["status"]
        statuses = {r[0] for r in rows}
        self.assertEqual(statuses, {BK.PENDING, BK.PARTIAL, BK.FILLED, BK.CANCELLED, BK.REJECTED})
        for r in rows:
            self.assertEqual(getattr(BK, r[1]), r[0])
        with open(BK.__file__, encoding="utf-8") as fh:
            src = fh.read()
        rejected = set(re.findall(r'_reject\(\w+,\s*"([^"]+)"\)', src))
        cancelled = set(re.findall(r'CANCELLED,\s*"([^"]+)"', src))
        self.assertEqual({r[2] for r in rows if r[0] == "rejected"}, rejected)
        self.assertEqual({r[2] for r in rows if r[0] == "cancelled"} - {"(空)"}, cancelled)
        self.assertEqual({r[2] for r in rows if r[0] in ("pending", "partial", "filled")}, {"(空)"})
        self.assertIn(["cancelled", "CANCELLED", "(空)"], [r[:3] for r in rows])
        self.assertEqual(len(rows), len({(r[0], r[2]) for r in rows}))
        self.assertEqual(len(rejected), 4)
        self.assertEqual(len(cancelled), 4)

    def test_config_fields(self):
        import dataclasses
        rows = load_tables()["config_fields"]
        fields = [f.name for f in dataclasses.fields(BrokerConfig)]
        self.assertEqual([r[0] for r in rows], fields)
        cfg = BrokerConfig()
        for name, default, _rng, bad in rows:
            self.assertEqual(json.loads(default), getattr(cfg, name), name)
            self.assertEqual(type(json.loads(default)), type(getattr(cfg, name)), name)
            kw = json.loads(bad)
            self.assertEqual(list(kw), [name])
            with self.assertRaises(ConfigError, msg=name):
                BrokerConfig(**kw).check()


class TestSigns(unittest.TestCase):
    KEYS = {"side", "qty", "fee", "slip_market", "slip_stop", "slip_limit", "funding", "funding_sign", "realized",
            "unrealized", "cash_negative", "liabilities", "identity"}

    def test_rows_and_references(self):
        rows = load_tables()["signs"]
        self.assertEqual({r[0] for r in rows}, self.KEYS)
        self.assertEqual(len(rows), len(self.KEYS))
        for r in rows:
            for ref in r[2].split("、"):
                resolve_test(ref.strip())

    def test_market_stop_limit_fee_and_funding(self):
        cfg = dict(slippage=0.001, taker_fee=0.0005, maker_fee=0.0002, max_participation=1.0, funding_rate=0.01,
                   funding_interval=2, impact=0.0)
        b, d = broker(FLAT, **cfg)
        b.on_bar(0)
        b.submit("S", 1, 10.0)                                               # 市价买 -> bar 1
        b.on_bar(1)
        f = b.fills[0]
        self.assertAlmostEqual(f.price, 100.0 * (1 + 0.001), delta=TOL)       # slip_market 买 +
        self.assertAlmostEqual(f.fee, 10.0 * f.price * 0.0005, delta=TOL)    # fee taker
        self.assertAlmostEqual(b.funding_paid, 10.0 * 100.0 * 0.01, delta=TOL)   # funding：多头付正费率
        self.assertAlmostEqual(b.cash, 100_000.0 - 10.0 * f.price - f.fee - b.funding_paid, delta=TOL)
        self.assertAlmostEqual(b.equity, b.cash + 10.0 * 100.0, delta=TOL)
        self.assertAlmostEqual(b.unrealized_pnl(), 10.0 * (100.0 - f.price), delta=TOL)   # unrealized
        b.submit("S", -1, 10.0)                                              # 市价卖 -> bar 2
        b.on_bar(2)
        g = b.fills[1]
        self.assertAlmostEqual(g.price, 100.0 * (1 - 0.001), delta=TOL)       # slip_market 卖 -
        self.assertAlmostEqual(b.realized_pnl(), (g.price - f.price) * 10.0, delta=TOL)   # realized 不含费
        self.assertTrue(f.qty > 0 and g.qty > 0 and (f.side, g.side) == (1, -1))          # side / qty
        b.submit("S", 1, 1.0, STOP, price=100.5)                             # 止损买 -> bar 3：high 101 >= 100.5
        b.submit("S", 1, 1.0, LIMIT, price=99.5)                             # 限价买：low 99 <= 99.5
        b.on_bar(3)
        stop, lim = b.fills[2], b.fills[3]
        self.assertAlmostEqual(stop.price, 100.5 * (1 + 0.001), delta=TOL)   # slip_stop 买 +
        self.assertEqual(stop.liquidity, "taker")
        self.assertAlmostEqual(lim.price, 99.5, delta=TOL)                   # slip_limit：无滑点
        self.assertEqual(lim.liquidity, "maker")
        self.assertAlmostEqual(lim.fee, 1.0 * 99.5 * 0.0002, delta=TOL)
        b.submit("S", -1, 1.0, STOP, price=99.5)                             # 止损卖 -> bar 4：low 99 <= 99.5
        b.on_bar(4)
        self.assertAlmostEqual(b.fills[4].price, 99.5 * (1 - 0.001), delta=TOL)   # slip_stop 卖 -
        self._check_identity(b, 4)
        funding_before = b.funding_paid
        b.submit("S", -1, 5.0)                                               # 反手成空头 -> bar 5
        b.on_bar(5)
        self.assertLess(b.positions["S"].qty, 0.0)
        self.assertLess(b.funding_paid, funding_before)                      # funding_sign：空头在正费率下收到
        self._check_identity(b, 5)

    def _check_identity(self, b, i):
        close = b.data["S"].close[i]
        pos = sum(p.qty for p in b.positions.values())
        self.assertAlmostEqual(b.equity, b.cash + pos * close, delta=1e-6)
        self.assertAlmostEqual(b.equity, b.assets() - b.liabilities(), delta=1e-6)
        self.assertAlmostEqual(b.equity, b.initial_cash + b.realized_pnl() + b.unrealized_pnl() - b.fees_paid
                               - b.funding_paid, delta=1e-6)

    def test_negative_cash_and_liabilities(self):
        deep = [(100.0, 101.0, 99.0, 100.0, 1e9)] * 12                       # 成交量足够，不被参与上限截断
        b, _ = broker(deep, max_leverage=3.0, slippage=0.0, taker_fee=0.0, max_participation=1.0)
        b.on_bar(0)
        b.submit("S", 1, 2000.0)                                             # 20 万名义 > 10 万现金：借入
        b.on_bar(1)
        self.assertLess(b.cash, 0.0)                                         # cash_negative
        self.assertAlmostEqual(b.liabilities(), -b.cash, delta=TOL)          # liabilities = 借入现金
        self.assertAlmostEqual(b.assets(), 2000.0 * 100.0, delta=TOL)
        self._check_identity(b, 1)
        b.submit("S", -1, 2500.0)                                            # 反手成空头 500
        b.on_bar(2)
        self.assertAlmostEqual(b.liabilities(), max(-b.cash, 0.0) + 500.0 * 100.0, delta=TOL)
        self._check_identity(b, 2)


class TestErrors(unittest.TestCase):
    def _classes(self):
        found = {}
        for mod in (ER, EN):
            for name, obj in vars(mod).items():
                if isinstance(obj, type) and issubclass(obj, QBTError) and obj.__module__ == mod.__name__:
                    found[name] = (mod.__name__, obj)
        return found

    def test_error_classes(self):
        rows = load_tables()["error_classes"]
        found = self._classes()
        self.assertEqual({r[0] for r in rows}, set(found))
        for name, where, base, _ in rows:
            mod, cls = found[name]
            self.assertEqual(where, mod, name)
            self.assertEqual(cls.__bases__[0].__name__, base, name)
            self.assertTrue(issubclass(cls, ValueError))
        self.assertEqual(len(found), 5)

    def test_raises_rows_resolve(self):
        rows = load_tables()["raises"]
        entries = [r[0] for r in rows]
        self.assertEqual(len(entries), len(set(entries)))
        self.assertEqual(set(entries), {
            "OHLCV.validate", "OHLCV.from_csv", "GenSpec.check / generate", "BrokerConfig.check / Broker()",
            "Broker.submit", "Broker.on_bar", "Engine() / run_backtest", "Strategy() / Strategy.on_start",
            "Context.indicator", "Context.data（strict）", "Context.target", "indicators.REGISTRY[*]",
            "metrics.var_cvar / metrics.compute", "optimizer.grid / random_search", "optimizer.walk_forward",
            "optimizer.make_task", "optimizer.evaluate / load_data"})
        known = set(self._classes()) | {"rejected", "cancelled", "FileNotFoundError", "TypeError"}
        btags = _boundary_tags()
        for r in rows:
            for token in re.split(r"[、,]\s*", r[1]):
                self.assertIn(token.strip(), known, r[0])
            for ref in r[3].split("、"):
                ref = ref.strip()
                if re.fullmatch(r"B\d\d", ref):
                    self.assertIn(ref, btags, r[0])
                else:
                    resolve_test(ref)
        used = {t for r in rows for t in re.split(r"[、,]\s*", r[1])}
        self.assertTrue(set(self._classes()) - {"QBTError"} <= used, used)   # 每个具体异常类至少出现一次


class TestDataFormat(unittest.TestCase):
    def test_ohlcv_columns(self):
        rows = load_tables()["ohlcv_columns"]
        self.assertEqual([r[0] for r in rows], [s for s in OHLCV.__slots__ if not s.startswith("_")])
        self.assertIsNone(OHLCV("X", [1], [1], [1], [1], [1], [1]).bar_seconds)
        self.assertEqual(OHLCV("X", [1, 4], [1, 1], [1, 1], [1, 1], [1, 1], [1, 1]).bar_seconds, 3)

    @staticmethod
    def _apply(d, change):
        """'col[idx]=rhs' / 'volume.pop()' / 'symbol=""'；rhs: 数字、nan、inf、None、"str"、col[idx]±数字。"""
        if change == "volume.pop()":
            d.volume.pop()
            return
        if change.startswith("symbol="):
            d.symbol = json.loads(change[len("symbol="):])
            return
        col, idx, rhs = re.fullmatch(r"(\w+)\[(\d+)\]=(.+)", change).groups()
        m = re.fullmatch(r"(\w+)\[(\d+)\]([+-].+)?", rhs)
        if m:
            val = getattr(d, m.group(1))[int(m.group(2))] + (float(m.group(3)) if m.group(3) else 0)
        elif rhs in ("nan", "inf", "-inf"):
            val = float(rhs)
        elif rhs == "None":
            val = None
        else:
            val = json.loads(rhs)
        getattr(d, col)[int(idx)] = val

    def test_validation_rules(self):
        rows = load_tables()["validation"]
        with open(DT.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        cores = set()
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in ("validate", "_check_finite")):
            for node in ast.walk(fn):
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.func.id == "DataError":
                    arg = node.exc.args[0]
                    parts = [arg.value] if isinstance(arg, ast.Constant) else [v.value for v in arg.values if isinstance(v, ast.Constant)]
                    cores.add("".join(parts).strip().lstrip(":").strip())
        self.assertEqual(len(cores), 8)
        seen = []
        for rule, changes, needle in rows:
            for change in changes.split(";"):
                d = generate(n=20, seed=1)
                self._apply(d, change.strip())
                d._validated = False
                with self.assertRaises(DataError, msg=rule) as ctx:
                    d.validate()
                self.assertIn(needle, str(ctx.exception), (rule, change))
            matched = [c for c in cores if c.startswith(needle)]
            self.assertEqual(len(matched), 1, (rule, needle))
            seen.append(matched[0])
        self.assertEqual(sorted(seen), sorted(cores))                       # 每条 raise DataError 恰好一行

    def test_csv(self):
        kv = {r[0]: r[1] for r in load_tables()["csv"]}
        self.assertEqual(set(kv), {"header", "separator", "ts_parse", "value_parse", "write_format", "bad_header",
                                   "bad_row", "missing_file", "symbol_arg"})
        with open(DT.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(f'"{kv["header"]}\\n"', src)                           # 写出的表头
        self.assertIn(f'!= "{kv["header"]}"', src)                           # 读入时校验的表头
        self.assertEqual(kv["separator"], ",")
        d = generate(n=30, seed=4, halt_prob=0.1)
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "d.csv")
            d.to_csv(p)
            with open(p, encoding="utf-8") as fh:
                lines = fh.read().split("\n")
            self.assertEqual(lines[0], kv["header"])
            self.assertEqual(lines[1], ",".join(repr(x) for x in d.to_rows()[0]))
            back = OHLCV.from_csv(p, symbol=d.symbol, bar_seconds=d.bar_seconds).validate()
            self.assertEqual(back.fingerprint(), d.fingerprint())
            self.assertEqual(OHLCV.from_csv(p).symbol, "CSV")
            self.assertEqual(kv["bad_header"], "DataError")
            self.assertTrue(kv["bad_row"].startswith("DataError"))
            self.assertEqual(kv["missing_file"], "FileNotFoundError")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("ts,open\n")
            with self.assertRaises(DataError):
                OHLCV.from_csv(p)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(kv["header"] + "\n1.0,2,3,1,2,5\n1,2\n")
            with self.assertRaises(DataError):
                OHLCV.from_csv(p)
            with self.assertRaises(FileNotFoundError):
                OHLCV.from_csv(os.path.join(tmp, "nope.csv"))
        sig = inspect.signature(OHLCV.from_csv)
        self.assertEqual(kv["symbol_arg"].split("：")[0], f"from_csv{sig}".replace('"', "'"))

    def test_genspec_fields(self):
        import dataclasses
        rows = load_tables()["genspec_fields"]
        self.assertEqual([r[0] for r in rows], [f.name for f in dataclasses.fields(GenSpec)])
        spec = GenSpec()
        for name, default, _ in rows:
            self.assertEqual(json.loads(default), getattr(spec, name), name)
            self.assertEqual(type(json.loads(default)), type(getattr(spec, name)), name)
        self.assertEqual(GenSpec.from_dict(spec.to_dict()), spec)


class TestOutputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = generate(n=300, seed=5)
        cls.r = run_backtest(cls.d, REGISTRY["ma_cross"](fast=5, slow=20), BrokerConfig(max_participation=1.0))
        cls.m = M.compute(cls.r.equity, cls.r.ts, cls.r.bar_seconds, cls.r.trades)

    def test_metrics_keys(self):
        rows = load_tables()["metrics_keys"]
        self.assertEqual({r[0] for r in rows}, set(self.m))
        self.assertEqual(len(rows), len(self.m))
        self.assertGreater(self.m["n_trades"], 0)
        self.assertEqual(set(M.compute([], [], 60)), set(self.m))           # 空输入同样的键

    def test_report_files(self):
        rows = {r[0]: r[1] for r in load_tables()["report_files"]}
        self.assertEqual(set(rows), {"txt", "svg", "json"})
        with tempfile.TemporaryDirectory() as tmp:
            paths = R.write_report(self.r, tmp, "x")
            self.assertEqual(set(paths), set(rows))
            for k, p in paths.items():
                self.assertEqual(os.path.basename(p), f"x.{k}")
            with open(paths["json"], encoding="utf-8") as fh:
                j = json.load(fh)
        self.assertEqual(set(j), {"summary", "metrics"})
        self.assertEqual(set(j["summary"]), {r[0] for r in load_tables()["summary_keys"]})
        self.assertEqual(set(j["metrics"]), {r[0] for r in load_tables()["metrics_keys"]})

    def test_optimizer_keys(self):
        rows = {r[0]: [k.strip() for k in r[1].split(",")] for r in load_tables()["optimizer_keys"]}
        self.assertEqual(set(rows), {"task", "evaluate", "evaluate.metrics", "search_json", "walk_forward", "fold",
                                     "checkpoint_table"})
        t = O.make_task("ma_cross", GenSpec(n=200, seed=1).to_dict())
        self.assertEqual(set(t), set(rows["task"]))
        O._worker_init(t["data_spec"])
        e = O.evaluate(t, {"fast": 5, "slow": 20})
        self.assertEqual(set(e), set(rows["evaluate"]))
        self.assertEqual(set(e["metrics"]), set(rows["evaluate.metrics"]))
        self.assertEqual(rows["evaluate.metrics"], list(O.RESULT_METRICS) + ["fills"])
        combos = O.grid({"fast": [5], "slow": [20, 30]})
        w = O.walk_forward(t, combos, n_folds=2)
        self.assertEqual(set(w), set(rows["walk_forward"]))
        self.assertEqual(set(w["folds"][0]), set(rows["fold"]))
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            out = os.path.join(tmp, "r.json")
            O.main(["--strategy", "ma_cross", "--n", "200", "--limit", "2", "--out", out, "--checkpoint",
                    os.path.join(tmp, "ck.db")])
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(set(json.load(fh)), set(rows["search_json"]))
            import sqlite3
            cols = [c[1] for c in sqlite3.connect(os.path.join(tmp, "ck.db")).execute("PRAGMA table_info(results)")]
            self.assertEqual(cols, rows["checkpoint_table"])

    def test_indicators(self):
        rows = load_tables()["indicators"]
        self.assertEqual([r[0] for r in rows], list(I.REGISTRY))
        for name, n_out in rows:
            res = I.REGISTRY[name](*_ind.TestGenericProperties.CALLS[name](self.d))
            got = len(res) if isinstance(res, tuple) else 1
            self.assertEqual(got, int(n_out), name)


def _boundary_tags():
    """tests/test_boundaries.py 里 `def test_x(self):  # contract: Bxx` -> {Bxx: (方法名, 是否 expectedFailure)}。"""
    from tests import test_boundaries as TB
    with open(TB.__file__, encoding="utf-8") as fh:
        src = fh.read()
    out = {}
    for meth, tag in re.findall(r"def (test_\w+)\(self\):\s*#\s*contract:\s*(B\d\d)", src):
        fn = next(getattr(c, meth) for c in vars(TB).values() if isinstance(c, type) and hasattr(c, meth))
        out[tag] = (meth, bool(getattr(fn, "__unittest_expecting_failure__", False)))
    return out


class TestBoundaryTable(unittest.TestCase):
    def test_rows_tags_and_expected_failures_match(self):
        rows = load_tables()["boundaries"]
        ids = [r[0] for r in rows]
        from tests.test_boundaries import N_BOUNDARY_ROWS
        self.assertEqual(ids, [f"B{i:02d}" for i in range(1, N_BOUNDARY_ROWS + 1)])   # 连续编号 + 行数：删任何一行都红
        tags = _boundary_tags()
        referenced = set()
        for r in rows:
            for ref in r[4].split("、"):
                ref = ref.strip()
                if re.fullmatch(r"B\d\d", ref):
                    self.assertEqual(ref, r[0], r[0])
                    referenced.add(ref)
                else:
                    resolve_test(ref)
        self.assertEqual(referenced, set(tags))
        for r in rows:
            if r[0] in tags:
                self.assertEqual("待修" in r[3], tags[r[0]][1], r[0])


class TestMeta(unittest.TestCase):
    """放水判据：每张表删掉一行（首行与末行各试一次）写到临时副本，对应判据必须红。"""
    TABLE_TESTS = {
        "strategy_class": "TestStrategyInterface.test_strategy_class", "strategy_init": "TestStrategyInterface.test_init_rules",
        "lifecycle": "TestStrategyInterface.test_lifecycle", "context_api": "TestStrategyInterface.test_context_api",
        "order_fields": "TestStructures.test_order_fields", "fill_fields": "TestStructures.test_fill_fields",
        "position_fields": "TestStructures.test_position_fields", "trade_fields": "TestStructures.test_trade_fields",
        "events": "TestStructures.test_events", "result_attrs": "TestStructures.test_result_attrs_and_summary",
        "summary_keys": "TestStructures.test_result_attrs_and_summary", "order_types": "TestOrdersAndStatus.test_order_types",
        "status": "TestOrdersAndStatus.test_status_and_reasons", "signs": "TestSigns.test_rows_and_references",
        "config_fields": "TestOrdersAndStatus.test_config_fields", "error_classes": "TestErrors.test_error_classes",
        "raises": "TestErrors.test_raises_rows_resolve", "ohlcv_columns": "TestDataFormat.test_ohlcv_columns",
        "validation": "TestDataFormat.test_validation_rules", "csv": "TestDataFormat.test_csv",
        "genspec_fields": "TestDataFormat.test_genspec_fields", "metrics_keys": "TestOutputs.test_metrics_keys",
        "report_files": "TestOutputs.test_report_files", "optimizer_keys": "TestOutputs.test_optimizer_keys",
        "indicators": "TestOutputs.test_indicators", "boundaries": "TestBoundaryTable.test_rows_tags_and_expected_failures_match",
    }

    @staticmethod
    def _without_row(src, table, which):
        """返回删掉 table 的首行（which=0）或末行（which=-1）数据行的副本。"""
        lines = src.split("\n")
        start = next(i for i, l in enumerate(lines) if re.search(r"contract-table:\s*%s\s*-->" % table, l))
        j = start + 1
        while not lines[j].strip():
            j += 1
        k = j
        while k < len(lines) and lines[k].strip().startswith("|"):
            k += 1
        data = list(range(j + 2, k))
        del lines[data[which]]
        return "\n".join(lines)

    def _run(self, name):
        suite = unittest.defaultTestLoader.loadTestsFromName(f"tests.test_contract.{name}")
        result = unittest.TestResult()
        suite.run(result)
        return result.wasSuccessful()

    def test_deleting_any_row_turns_red(self):
        self.assertEqual(set(self.TABLE_TESTS), set(TABLES))
        with open(DEFAULT_CONTRACT, encoding="utf-8") as fh:
            src = fh.read()
        old = os.environ.get("QBT_CONTRACT_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "CONTRACT.md")
                os.environ["QBT_CONTRACT_PATH"] = path
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(src)
                for table, test in self.TABLE_TESTS.items():
                    self.assertTrue(self._run(test), f"{table}: intact copy must be green")
                for table, test in self.TABLE_TESTS.items():
                    for which in (0, -1):
                        with open(path, "w", encoding="utf-8") as fh:
                            fh.write(self._without_row(src, table, which))
                        self.assertFalse(self._run(test), f"{table}: deleting row {which} must turn {test} red")
        finally:
            if old is None:
                os.environ.pop("QBT_CONTRACT_PATH", None)
            else:
                os.environ["QBT_CONTRACT_PATH"] = old


if __name__ == "__main__":
    unittest.main()
