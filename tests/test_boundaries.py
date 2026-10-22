"""边界判据：每个入口 × 每类边界一条（qbt/CONTRACT.md §9 的 B01..B47）。

规则：裸异常 = 红。受控结果只有 QBTError 子类、§3.2 的 rejected / cancelled 状态、或 §9 写明的正常返回。
每条判据带 `# contract: Bxx` 标注；test_contract 核对标注与 §9 一一对应，且「待修」行恰好是 expectedFailure 的判据
（这些判据断言的是契约，当前代码抛裸异常；修好后删掉装饰器与表里的「待修」）。
已有判据覆盖的边界只在 §9 引用，不在这里重复。
"""
import json
import math
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from qbt.broker import Broker, BrokerConfig, MARKET, LIMIT, STOP, TAKE_PROFIT, ICEBERG, PENDING, REJECTED
from qbt.data import OHLCV, GenSpec, generate
from qbt.engine import Context, Engine, Strategy, run_backtest
from qbt.errors import ConfigError, DataError, OrderError
from qbt import indicators as I
from qbt import metrics as M
from qbt import optimizer as O
from qbt import report as R
from strategies import REGISTRY
from tests import test_indicators as _ind

HEADER = "ts,open,high,low,close,volume\n"
N_BOUNDARY_ROWS = 50          # CONTRACT.md §9 的行数（B01..B50）；test_contract 用它核对表没被截短
NAN, INF = math.nan, math.inf


def bars(rows, symbol="S"):
    cols = list(zip(*rows))
    return OHLCV(symbol, list(range(len(rows))), *[list(c) for c in cols], bar_seconds=1).validate()


def broker(rows, **cfg):
    d = bars(rows)
    return Broker({d.symbol: d}, BrokerConfig(**cfg)), d


def csv_file(tmp, text, name="d.csv"):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


def halted(n=300, seed=1):
    return generate(n=n, seed=seed, halt_prob=1.0)


def outputs(res):
    return list(res) if isinstance(res, tuple) else [res]


def finite_values(res):
    return [v for o in outputs(res) for v in o if v is not None]


class Noop(Strategy):
    name = "noop"

    def on_bar(self, ctx, i):
        pass


class DataBoundaries(unittest.TestCase):
    def test_single_bar_generate(self):                                 # contract: B02
        d = generate(n=1, seed=1)
        self.assertEqual((len(d), d.bar_seconds, d._validated), (1, 60, True))
        self.assertEqual(len(d.ts), 1)
        self.assertEqual(d.validate(), d)

    def test_all_halted_generate(self):                                 # contract: B03
        d = halted(50)
        self.assertEqual(set(d.volume), {0.0})
        self.assertEqual(set(d.open) | set(d.high) | set(d.low) | set(d.close), {100.0})
        self.assertTrue(d._validated)
        self.assertEqual(len(d), 50)

    def test_validate_rejects_inf_in_every_column(self):                # contract: B04
        for col in ("open", "high", "low", "close", "volume"):
            for bad in (INF, -INF, NAN):
                d = generate(n=20, seed=1)
                getattr(d, col)[7] = bad
                d._validated = False
                with self.assertRaises(DataError, msg=f"{col}={bad}"):
                    d.validate()
        d = generate(n=20, seed=1)
        d.high[7] = 0.0
        d._validated = False
        with self.assertRaises(DataError):
            d.validate()

    def test_genspec_unknown_field_is_type_error(self):                 # contract: B06
        with self.assertRaises(TypeError):
            GenSpec(bogus=1)
        with self.assertRaises(TypeError):
            GenSpec.from_dict({"n": 5, "bogus": 1})
        with self.assertRaises(TypeError):
            generate(n=5, seed=1, bogus=1)

    def test_csv_header_only_and_single_row(self):                      # contract: B07
        with tempfile.TemporaryDirectory() as tmp:
            e = OHLCV.from_csv(csv_file(tmp, HEADER)).validate()
            self.assertEqual((len(e), e.bar_seconds), (0, None))
            s = OHLCV.from_csv(csv_file(tmp, HEADER + "1600000000,1,2,0.5,1.5,3\n")).validate()
            self.assertEqual((len(s), s.bar_seconds, s.ts, s.close), (1, None, [1600000000], [1.5]))
            s2 = OHLCV.from_csv(csv_file(tmp, HEADER + "1600000000,1,2,0.5,1.5,3\n"), bar_seconds=60)
            self.assertEqual(s2.bar_seconds, 60)

    def test_csv_numeric_tokens_rejected_by_validate(self):             # contract: B09
        rows = {"nan": "1,nan,1,1,1,1\n", "inf": "1,1,inf,1,1,1\n", "-inf": "1,1,1,-inf,1,1\n", "zero": "1,0,0,0,0,1\n",
                "neg": "1,-1,-1,-1,-1,1\n", "neg_vol": "1,1,1,1,1,-1\n", "nan_vol": "1,1,1,1,1,nan\n"}
        with tempfile.TemporaryDirectory() as tmp:
            for label, row in rows.items():
                d = OHLCV.from_csv(csv_file(tmp, HEADER + row))
                self.assertEqual(len(d), 1, label)
                with self.assertRaises(DataError, msg=label):
                    d.validate()

    def test_csv_garbage_tokens_are_data_errors(self):                  # contract: B10
        with tempfile.TemporaryDirectory() as tmp:
            for row in ("1,abc,1,1,1,1\n", "nan,1,1,1,1,1\n", "1e400,1,1,1,1,1\n", "1,1,1,1,1,\n"):
                with self.assertRaises(DataError, msg=row):
                    OHLCV.from_csv(csv_file(tmp, HEADER + row))

    def test_missing_csv_is_file_not_found(self):                       # contract: B11
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "nope.csv")
            with self.assertRaises(FileNotFoundError):
                OHLCV.from_csv(missing)
            with self.assertRaises(FileNotFoundError):
                O.evaluate(O.make_task("ma_cross", {"csv": missing, "symbol": "X"}), {"fast": 5, "slow": 20})


class BrokerBoundaries(unittest.TestCase):
    FLAT = [(100.0, 101.0, 99.0, 100.0, 1000.0)] * 5
    HALT = [(100.0, 100.0, 100.0, 100.0, 0.0)] * 5

    def test_qty_negative_zero_and_huge(self):                          # contract: B12
        b, _ = broker(self.FLAT, max_participation=1.0)
        b.on_bar(0)
        with self.assertRaises(OrderError):
            b.submit("S", 1, -0.0)
        with self.assertRaises(OrderError):
            b.submit("S", 1, -INF)
        o = b.submit("S", 1, 1e308)                                     # 有限正数：提交通过
        self.assertEqual(o.status, PENDING)
        b.on_bar(1)
        self.assertEqual((o.status, o.reason, len(b.fills), b.equity), (REJECTED, "margin", 0, 100_000.0))

    def test_price_inf_and_bool_rejected(self):                         # contract: B13
        b, _ = broker(self.FLAT)
        b.on_bar(0)
        for otype in (LIMIT, STOP, TAKE_PROFIT):
            for price in (INF, -INF, True, False, NAN, 0, -1.0):
                with self.assertRaises(OrderError, msg=f"{otype} {price!r}"):
                    b.submit("S", 1, 1.0, otype, price=price)
        for display in (0, -1.0, NAN, INF, True):
            with self.assertRaises(OrderError, msg=repr(display)):
                b.submit("S", 1, 1.0, ICEBERG, price=100.0, display=display)
        with self.assertRaises(OrderError):
            b.submit("S", 1, 1.0, ICEBERG, price=INF, display=1.0)
        self.assertEqual(b.orders, [])

    def test_empty_dataset(self):                                       # contract: B14
        b = Broker({}, BrokerConfig())
        self.assertEqual((b.equity, b.cash, b.positions), (100_000.0, 100_000.0, {}))
        self.assertEqual(b.on_bar(0), 100_000.0)
        with self.assertRaises(OrderError):
            b.submit("S", 1, 1.0)
        self.assertEqual((b.assets(), b.liabilities(), b.gross_exposure()), (100_000.0, 0.0, 0.0))

    def test_single_bar_order_stays_pending(self):                      # contract: B15
        b, _ = broker(self.FLAT[:1], max_participation=1.0)
        self.assertEqual(b.on_bar(0), 100_000.0)
        o = b.submit("S", 1, 1.0)
        self.assertEqual((o.status, b.fills, b.pending), (PENDING, [], [o]))
        self.assertEqual(b.cancel_all(), 1)
        self.assertEqual(o.status, "cancelled")

    def test_all_halted_never_fills(self):                              # contract: B16
        b, _ = broker(self.HALT, max_participation=1.0)
        b.on_bar(0)
        orders = [b.submit("S", 1, 1.0, MARKET), b.submit("S", 1, 1.0, LIMIT, price=200.0),
                  b.submit("S", -1, 1.0, STOP, price=50.0), b.submit("S", 1, 2.0, ICEBERG, price=200.0, display=1.0),
                  b.submit("S", -1, 1.0, TAKE_PROFIT, price=50.0)]
        for i in range(1, 5):
            self.assertEqual(b.on_bar(i), 100_000.0)
        self.assertEqual([o.status for o in orders], [PENDING] * 5)
        self.assertEqual((b.fills, b.fees_paid, b.pending), ([], 0.0, orders))

    def test_max_leverage_1e6(self):                                    # contract: B17
        flat = [(100.0, 101.0, 99.0, 100.0, 1e12)] * 4
        b, _ = broker(flat, max_leverage=1e6, maintenance_margin=0.0, max_participation=1.0, slippage=0.0, taker_fee=0.0)
        b.on_bar(0)
        o = b.submit("S", 1, 1e7)                                       # 10 亿名义 <= 10 万 × 1e6
        b.on_bar(1)
        self.assertEqual((o.status, len(b.fills), b.liquidations), ("filled", 1, 0))
        self.assertLess(b.cash, 0.0)                                    # 借入现金
        self.assertAlmostEqual(b.liabilities(), -b.cash, delta=1e-6)
        self.assertAlmostEqual(b.equity, b.cash + 1e7 * 100.0, delta=1e-3)
        self.assertAlmostEqual(b.equity, b.assets() - b.liabilities(), delta=1e-3)
        self.assertAlmostEqual(b.on_bar(2), 100_000.0, delta=1e-3)
        b2, _ = broker(flat, max_leverage=1e6, max_participation=1.0, slippage=0.0, taker_fee=0.0)
        b2.on_bar(0)
        o2 = b2.submit("S", 1, 1e7)
        b2.on_bar(1)                                                    # 权益 10 万 < 5% × 10 亿 => 成交当根即强平、破产
        self.assertEqual((o2.status, b2.liquidations, b2.bankrupt), ("filled", 1, True))
        self.assertEqual([e[0] for e in b2.events], ["liquidation", "bankrupt"])
        self.assertEqual(b2.submit("S", 1, 1.0).reason, "bankrupt")
        self.assertTrue(math.isfinite(b2.on_bar(2)))
        self.assertLess(b2.equity, 0.0)

    def test_max_leverage_1e_minus_6_and_inf(self):                     # contract: B18
        b, _ = broker(self.FLAT, max_leverage=1e-6, max_participation=1.0)
        b.on_bar(0)
        big = b.submit("S", 1, 1.0)                                     # 100 名义 > 10 万 × 1e-6 = 0.1
        edge = b.submit("S", 1, 1e-3)                                   # 0.10001 名义（含滑点）> 0.1
        small = b.submit("S", 1, 9e-4)                                  # 0.090009 <= 0.1
        b.on_bar(1)
        self.assertEqual([(o.status, o.reason) for o in (big, edge)], [(REJECTED, "margin")] * 2)
        self.assertEqual((small.status, b.positions["S"].qty), ("filled", 9e-4))
        close = b.submit("S", -1, 9e-4, reduce_only=True)               # 减仓永远允许
        b.on_bar(2)
        self.assertEqual((close.status, b.positions["S"].qty), ("filled", 0.0))
        for cfg in ({"max_leverage": INF}, {"max_leverage": -1e-6}, {"max_leverage": NAN}):
            with self.assertRaises(ConfigError, msg=str(cfg)):
                BrokerConfig(**cfg).check()


class EngineBoundaries(unittest.TestCase):
    CFG = BrokerConfig(max_participation=1.0)

    def _all(self, d, cfg=None, strict=False):
        out = {}
        for name, cls in REGISTRY.items():
            out[name] = run_backtest(d, cls(), cfg or self.CFG, strict=strict)
        return out

    def test_empty_data_all_strategies(self):                           # contract: B19
        d = generate(n=0, seed=1)
        for strict in (False, True):
            for name, r in self._all(d, strict=strict).items():
                self.assertEqual((r.n, r.equity, r.final_equity, r.fills, r.orders, r.trades), (0, [], 100_000.0, [], [], []), name)
                json.dumps(r.summary())
                self.assertEqual(M.compute(r.equity, r.ts, r.bar_seconds, r.trades)["bars"], 0)
        with self.assertRaises(ConfigError):
            Engine({}, Noop())

    def test_single_bar_all_strategies(self):                           # contract: B20
        d = generate(n=1, seed=1)
        for strict in (False, True):
            for name, r in self._all(d, strict=strict).items():
                self.assertEqual((r.n, len(r.equity), r.fills, r.final_equity), (1, 1, [], 100_000.0), name)
                self.assertEqual(r.equity, [100_000.0], name)
                json.dumps(r.summary())
                self.assertEqual(M.compute(r.equity, r.ts, r.bar_seconds, r.trades)["bars"], 1)

    def test_all_halted_all_strategies(self):                           # contract: B21
        d = halted(300)
        for strict in (False, True):
            for name, r in self._all(d, strict=strict).items():
                self.assertEqual((len(r.fills), r.fees, r.funding, r.liquidations), (0, 0.0, 0.0, 0), name)
                self.assertEqual(set(r.equity), {100_000.0}, name)
                self.assertTrue(all(o.status in ("pending", "cancelled") for o in r.orders), name)

    def test_invalid_prices_rejected_at_construction(self):             # contract: B22
        base = [(100.0, 101.0, 99.0, 100.0, 1.0)] * 3
        for col, bad in ((1, 0.0), (1, -1.0), (1, NAN), (2, INF), (3, -INF), (4, 0.0), (5, NAN), (5, -1.0)):
            rows = [list(r) for r in base]
            rows[1][col - 1] = bad
            d = OHLCV("S", [0, 1, 2], *[list(c) for c in zip(*rows)], bar_seconds=1)
            with self.assertRaises(DataError, msg=f"col{col}={bad}"):
                run_backtest(d, Noop(), self.CFG)
            with self.assertRaises(DataError, msg=f"col{col}={bad}"):
                Engine({"S": d}, [Noop()], self.CFG)

    def test_extreme_leverage_all_strategies(self):                     # contract: B23
        d = generate(n=500, seed=2, halt_prob=0.0)
        low = self._all(d, BrokerConfig(max_leverage=1e-6, max_participation=1.0))
        all_reasons = set()
        for name, r in low.items():
            self.assertEqual(len(r.fills), 0, name)
            reasons = {o.reason for o in r.orders if o.status == REJECTED}
            self.assertTrue(reasons <= {"margin", "reduce_only"}, (name, reasons))      # grid 的限价单可能在触价前已被重铺撤销
            self.assertTrue(all(o.status in ("pending", "cancelled", REJECTED) for o in r.orders), name)
            self.assertEqual(set(r.equity), {100_000.0}, name)
            all_reasons |= reasons
        self.assertIn("margin", all_reasons)
        high = self._all(d, BrokerConfig(max_leverage=1e6, max_participation=1.0))
        for name, r in high.items():
            self.assertTrue(all(math.isfinite(e) for e in r.equity), name)
            self.assertTrue(all(o.status != REJECTED or o.reason in ("margin", "bankrupt", "reduce_only") for o in r.orders), name)
            self.assertAlmostEqual(r.final_equity, r.equity[-1], delta=1e-6, msg=name)

    def test_zero_or_negative_window_params(self):                      # contract: B24
        d = generate(n=50, seed=1)
        cases = [REGISTRY["ma_cross"](fast=0, slow=30), REGISTRY["rsi_reversion"](n=-1), REGISTRY["grid"](recenter_n=0),
                 REGISTRY["donchian_breakout"](entry_n=0), REGISTRY["trend_stop"](atr_n=0), REGISTRY["bollinger_reversion"](n=0),
                 REGISTRY["trend_stop"](ma_n=-5)]
        for s in cases:
            with self.assertRaises(ConfigError, msg=s.name):
                run_backtest(d, s, self.CFG)
        with self.assertRaises(ConfigError):
            Engine(d, Noop()).ctx.indicator("sma", "close", 0)

    def test_grid_levels_zero_is_config_error(self):                    # contract: B25
        d = generate(n=100, seed=1)
        for bad in ({"levels": 0}, {"levels": -1}, {"spacing": 0.0}, {"spacing": -0.01}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                run_backtest(d, REGISTRY["grid"](**bad), self.CFG)

    def test_zero_size_params_trade_nothing(self):                      # contract: B26
        d = generate(n=300, seed=1, halt_prob=0.0)
        cases = [REGISTRY["ma_cross"](size=0.0), REGISTRY["bollinger_reversion"](size=0.0), REGISTRY["rsi_reversion"](size=0.0),
                 REGISTRY["grid"](qty_frac=0.0), REGISTRY["donchian_breakout"](risk=0.0), REGISTRY["trend_stop"](size=0.0)]
        for s in cases:
            r = run_backtest(d, s, self.CFG)
            self.assertEqual((len(r.fills), len(r.orders)), (0, 0), s.name)
            self.assertEqual(set(r.equity), {100_000.0}, s.name)

    def test_unknown_symbol_on_context(self):                           # contract: B50
        ctx = Engine(generate(n=5, seed=1), Noop()).ctx
        with self.assertRaises(OrderError):
            ctx.position("NOPE")
        with self.assertRaises(OrderError):
            ctx.target("NOPE", 1.0)
        with self.assertRaises(ConfigError):
            ctx.indicator("sma", "close", 3, symbol="NOPE")
        self.assertEqual(ctx.broker.orders, [])                          # 没有订单被偷偷提交

    def test_target_nan_and_inf(self):                                  # contract: B27
        ctx = Engine(generate(n=5, seed=1), Noop()).ctx
        self.assertIsNone(ctx.target("SYN", NAN))
        self.assertEqual(ctx.broker.orders, [])
        for bad in (INF, -INF):
            with self.assertRaises(OrderError):
                ctx.target("SYN", bad)
        self.assertEqual(ctx.broker.orders, [])
        self.assertIsNone(ctx.target("SYN", 0.0))
        self.assertIsNone(ctx.target("SYN", 1e-10))                   # 差额 <= 1e-9 不下单


class MetricsBoundaries(unittest.TestCase):
    def test_single_point_equity(self):                                 # contract: B29
        m = M.compute([100.0], [1600000000], 60)
        self.assertEqual((m["bars"], m["start_equity"], m["end_equity"], m["total_return"]), (1, 100.0, 100.0, 0.0))
        self.assertEqual((m["cagr"], m["sharpe"], m["sortino"], m["var_bar"], m["cvar_bar"], m["var_daily"]), (0.0,) * 6)
        self.assertEqual((m["max_drawdown"], m["max_drawdown_bars"], m["calmar"], m["n_trades"]), (0.0, 0, None, 0))
        self.assertEqual(len(m["monthly"]), 1)
        json.dumps(m)

    def test_nan_inf_zero_negative_equity(self):                        # contract: B30
        for eq in ([100.0, NAN, 90.0], [100.0, INF, 90.0], [100.0, -INF, 90.0], [100.0, 0.0, 90.0], [100.0, -5.0, 90.0],
                   [0.0, 0.0], [-1.0, -2.0], [NAN, NAN], [100.0, 0.0]):
            m = M.compute(eq, list(range(0, 60 * len(eq), 60)), 60)
            self.assertEqual(m["bars"], len(eq), eq)
            self.assertEqual(len(m), 29, eq)
        self.assertEqual(M.compute([100.0, 0.0], [0, 60], 60)["cagr"], -1.0)
        self.assertEqual(M.compute([100.0, -5.0], [0, 60], 60)["cagr"], -1.0)
        self.assertTrue(math.isnan(M.compute([100.0, NAN], [0, 60], 60)["total_return"]))
        self.assertEqual(M.compute([0.0, 0.0], [0, 60], 60)["total_return"], 0.0)

    def test_bar_seconds_none_or_zero_is_config_error(self):            # contract: B31
        with tempfile.TemporaryDirectory() as tmp:
            d = OHLCV.from_csv(csv_file(tmp, HEADER + "1600000000,1,2,0.5,1.5,3\n"))
            r = run_backtest(d, Noop())
            self.assertIsNone(r.bar_seconds)
            with self.assertRaises(ConfigError):
                M.compute(r.equity, r.ts, r.bar_seconds)
            with self.assertRaises(ConfigError):
                R.write_report(r, tmp, "x")
            for bad in (0, -60, NAN, INF, True, "60"):
                with self.assertRaises(ConfigError, msg=repr(bad)):
                    M.compute([100.0, 101.0], [0, 60], bad)

    def test_alpha_out_of_range(self):                                  # contract: B32
        for alpha in (0, 0.0, -0.1, 1.0000001, 2, NAN, INF, True, "0.05", None):
            with self.assertRaises(ConfigError, msg=repr(alpha)):
                M.var_cvar([0.1, -0.1], alpha)
            with self.assertRaises(ConfigError, msg=repr(alpha)):
                M.compute([100.0, 101.0, 99.0], [0, 60, 120], 60, alpha=alpha)
        self.assertEqual(M.var_cvar([-0.1, 0.2], 1.0), (-0.2, -0.05))        # alpha = 1 合法：取全部样本


class OptimizerBoundaries(unittest.TestCase):
    SPACE = {"fast": [5], "slow": [20]}

    def _task(self, n, **kw):
        return O.make_task("ma_cross", GenSpec(n=n, seed=1, **kw).to_dict())

    def test_random_search_empty_space(self):                           # contract: B33
        with self.assertRaises(ConfigError):
            O.random_search({}, 3, seed=1)
        with self.assertRaises(ConfigError):
            O.random_search({"a": [], "b": [1]}, 3, seed=1)
        self.assertEqual(O.random_search(self.SPACE, 0, seed=1), [])

    def test_random_search_negative_or_non_int_n(self):                 # contract: B48
        for bad in (-1, 2.0, True, None, "3"):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                O.random_search(self.SPACE, bad, seed=1)

    def test_make_task_unknown_strategy_or_objective(self):             # contract: B49
        spec = GenSpec(n=50, seed=1).to_dict()
        with self.assertRaises(ConfigError):
            O.make_task("no_such_strategy", spec)
        with self.assertRaises(ConfigError):
            O.make_task("ma_cross", spec, objective="no_such_metric")
        self.assertEqual(O.make_task("ma_cross", spec, objective="calmar")["objective"], "calmar")

    def test_run_search_empty_combos(self):                             # contract: B34
        t = self._task(100)
        self.assertEqual(O.run_search(t, []), [])
        self.assertEqual(O.run_search(t, [], workers=2), [])
        self.assertIsNone(O.best([]))
        self.assertEqual(O.dedupe([]), [])

    def test_walk_forward_empty_combos_is_config_error(self):           # contract: B35
        with self.assertRaises(ConfigError):
            O.walk_forward(self._task(100), [], n_folds=1)

    def test_walk_forward_too_few_bars(self):                           # contract: B36
        combos = O.grid(self.SPACE)
        for n in (0, 1, 9):
            with self.assertRaises(ConfigError, msg=str(n)):
                O.walk_forward(self._task(n), combos, n_folds=1)
        with self.assertRaises(ConfigError):
            O.walk_forward(self._task(19), combos, n_folds=2)

    def test_evaluate_degenerate_data(self):                            # contract: B37
        params = {"fast": 5, "slow": 20}
        for t in (self._task(0), self._task(1), self._task(200, halt_prob=1.0)):
            O._worker_init(t["data_spec"])
            e = O.evaluate(t, params)
            self.assertEqual((e["metrics"]["n_trades"], e["metrics"]["fills"], e["objective"]), (0, 0, -1e300))
            self.assertEqual(e["raw_objective"], 0.0)                   # 无收益：夏普 0.0
            json.dumps(e)
        e = O.evaluate(dict(self._task(300), slice=[0, 0]), params)
        self.assertEqual(e["objective"], -1e300)

    def test_evaluate_extreme_leverage(self):                           # contract: B38
        params = {"fast": 5, "slow": 20}
        t_low = O.make_task("ma_cross", GenSpec(n=300, seed=1).to_dict(), broker=BrokerConfig(max_leverage=1e-6))
        O._worker_init(t_low["data_spec"])
        e = O.evaluate(t_low, params)
        self.assertEqual((e["metrics"]["fills"], e["objective"]), (0, -1e300))
        t_high = O.make_task("ma_cross", GenSpec(n=300, seed=1).to_dict(), broker=BrokerConfig(max_leverage=1e6))
        e = O.evaluate(t_high, params)
        self.assertGreater(e["metrics"]["fills"], 0)
        self.assertTrue(math.isfinite(e["objective"]))
        json.dumps(e)


class ReportBoundaries(unittest.TestCase):
    def test_svg_with_nan_inf_and_zero_equity(self):                    # contract: B40
        for eq in ([1.0, NAN, 2.0], [1.0, INF, 2.0], [1.0, -INF, 2.0], [0.0, 0.0, 0.0], [NAN, NAN], [INF, INF]):
            svg = R.equity_svg(eq, None, width=300, height=200)
            root = ET.fromstring(svg)
            self.assertEqual(root.get("width"), "300", eq)
            self.assertIsNotNone(root.find("{http://www.w3.org/2000/svg}polyline"), eq)
            self.assertEqual(len(root.find("{http://www.w3.org/2000/svg}polyline").get("points").split()), len(eq), eq)

    def test_write_report_degenerate_results(self):                     # contract: B41
        cfg = BrokerConfig(max_participation=1.0)
        with tempfile.TemporaryDirectory() as tmp:
            for label, d in (("empty", generate(n=0, seed=1)), ("single", generate(n=1, seed=1)), ("halted", halted(100))):
                r = run_backtest(d, REGISTRY["ma_cross"](), cfg)
                paths = R.write_report(r, tmp, label)
                self.assertEqual(sorted(paths), ["json", "svg", "txt"])
                with open(paths["json"], encoding="utf-8") as fh:
                    j = json.load(fh)
                self.assertEqual(set(j), {"summary", "metrics"}, label)
                self.assertEqual((j["summary"]["n"], j["summary"]["fills"], j["metrics"]["bars"]), (len(d), 0, len(d)), label)
                self.assertEqual(len(j["metrics"]), 29, label)
                ET.parse(paths["svg"])
                with open(paths["txt"], encoding="utf-8") as fh:
                    self.assertIn("期末权益", fh.read())


class IndicatorBoundaries(unittest.TestCase):
    def _run(self, d):
        return {name: I.REGISTRY[name](*_ind.TestGenericProperties.CALLS[name](d)) for name in I.REGISTRY}

    def test_all_halted_inputs(self):                                   # contract: B43
        d = halted(100)
        for name, res in self._run(d).items():
            for o in outputs(res):
                self.assertEqual(len(o), 100, name)
            self.assertTrue(all(math.isfinite(v) for v in finite_values(res)), name)

    def test_zero_and_negative_prices(self):                            # contract: B44
        n = 100
        for col in ([0.0] * n, [float(i + 1) if i != 50 else 0.0 for i in range(n)], [-1.0 - i for i in range(n)]):
            d = OHLCV("X", list(range(n)), col, col, col, col, [1.0] * n)
            for name, res in self._run(d).items():
                for o in outputs(res):
                    self.assertEqual(len(o), n, name)
                self.assertTrue(all(math.isfinite(v) for v in finite_values(res)), name)

    def test_nan_inf_propagate_without_exception(self):                 # contract: B45
        n = 100
        base = [float(i + 1) for i in range(n)]
        variants = [(base[:50] + [NAN] + base[51:], [1.0] * n), (base[:50] + [INF] + base[51:], [1.0] * n),
                    ([NAN] * n, [1.0] * n), (base, [NAN] * n), (base, [INF] * n), (base, [-1.0] * n)]
        for col, vol in variants:
            d = OHLCV("X", list(range(n)), col, col, col, col, vol)
            for name, res in self._run(d).items():
                for o in outputs(res):
                    self.assertEqual(len(o), n, name)

    def test_window_longer_than_series(self):                           # contract: B46
        d = generate(n=5, seed=1)
        for name, res in self._run(d).items():
            for o in outputs(res):
                self.assertEqual(len(o), 5, name)
        self.assertEqual(I.sma(d.close, 10), [None] * 5)
        self.assertEqual(I.donchian(d.high, d.low, 10), ([None] * 5,) * 3)
        self.assertEqual(I.rsi(d.close, 14), [None] * 5)


if __name__ == "__main__":
    unittest.main()
