"""examples.py 的行为判据：四个 validate 的接受 / 受控拒绝 / 裸异常边界与规范化输出；toy 引擎逐笔重放记账；
noisy_engine 的噪声因子；qbt 适配器的采样口径。"""
import math
import unittest

from qdcheck import examples as E

VALID = {"symbol": "BTCUSDT", "period": 20, "side": "long", "fee_rate": 0.001, "leverage": 3, "use_stop": True,
         "symbols": ["ETH", "SOL"], "risk": {"max_drawdown": 0.2, "stop_loss": 0.05, "window": 30}}
NORMALISED = {"symbol": "BTCUSDT", "period": 20, "side": "long", "fee_rate": 0.001, "leverage": 3.0,
              "symbols": ["ETH", "SOL"], "risk": {"max_drawdown": 0.2, "stop_loss": 0.05, "window": 30}}
VALIDATES = (E.validate_good, E.validate_keyerror, E.validate_zerodiv, E.validate_recursion)


def outcome(fn, cfg):
    try:
        fn(cfg)
    except E.ValidationError:
        return "controlled"
    except Exception as exc:                 # noqa: BLE001
        return type(exc).__name__
    return "pass"


def with_(**kw):
    cfg = dict(VALID)
    for k, v in kw.items():
        if v is KeyError:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    return cfg


class TestValidates(unittest.TestCase):
    def test_normalised_output(self):
        for fn in VALIDATES:
            out = fn(dict(VALID))
            want = dict(NORMALISED, bars_per_period=500) if fn is E.validate_zerodiv else NORMALISED
            self.assertEqual(out, want, fn.__name__)
            self.assertIsInstance(out["leverage"], float)
            self.assertIsInstance(out["risk"]["max_drawdown"], float)
        minimal = {"symbol": "X", "period": 1, "side": "both"}
        for fn in VALIDATES:
            out = fn(dict(minimal))
            self.assertEqual({k: v for k, v in out.items() if k != "bars_per_period"}, minimal)
        self.assertEqual(E.validate_zerodiv(dict(minimal))["bars_per_period"], 10000)
        self.assertEqual(E.validate_good(with_(risk={"max_drawdown": 1}))["risk"], {"max_drawdown": 1.0})
        self.assertEqual(E.validate_good(with_(leverage=10 ** 400 // 10 ** 399))["leverage"], 10.0)

    def test_boundaries_shared_by_all(self):
        cases = [(with_(period=500), "pass"), (with_(period=501), "controlled"), (with_(period=1), "pass"),
                 (with_(period=2.0), "controlled"), (with_(period=True), "controlled"),
                 (with_(symbol="A" * 16), "pass"), (with_(symbol="A" * 17), "controlled"),
                 (with_(symbol="café"), "controlled"), (with_(symbol=7), "controlled"),
                 (with_(side="LONG"), "controlled"), (with_(side=1), "controlled"), (with_(side=KeyError), "controlled"),
                 (with_(fee_rate=0.01), "pass"), (with_(fee_rate=0.0100001), "controlled"),
                 (with_(fee_rate=-0.0), "pass"), (with_(fee_rate=math.nan), "controlled"),
                 (with_(fee_rate=math.inf), "controlled"), (with_(fee_rate="0.001"), "controlled"),
                 (with_(fee_rate=False), "controlled"), (with_(leverage=1), "pass"), (with_(leverage=0.99), "controlled"),
                 (with_(leverage=20.0), "pass"), (with_(leverage=20.5), "controlled"),
                 (with_(leverage=10 ** 400), "controlled"), (with_(use_stop=1), "controlled"),
                 (with_(use_stop=False), "pass"), (with_(symbols=[]), "pass"), (with_(symbols=["A"] * 4), "pass"),
                 (with_(symbols=["A"] * 5), "controlled"), (with_(symbols="ABC"), "controlled"),
                 (with_(symbols=["A" * 9]), "controlled"), (with_(symbols=[""]), "controlled"),
                 (with_(risk={}), "controlled"), (with_(risk={"max_drawdown": 1.5}), "controlled"),
                 (with_(risk={"max_drawdown": 0.1, "extra": 1}), "controlled"), (with_(risk=[]), "controlled"),
                 (with_(risk={"max_drawdown": 0.1, "stop_loss": 0.51}), "controlled"),
                 (with_(risk={"max_drawdown": 0.1, "window": 0}), "controlled"),
                 (with_(risk={"max_drawdown": 0.1, "window": 1000}), "pass"),
                 (with_(risk={"max_drawdown": 0.1, "window": 1001}), "controlled"),
                 (with_(Period=20), "controlled"), (dict(VALID, **{"": 1}), "controlled"), ({**VALID, 1: 2}, "controlled")]
        for cfg, want in cases:
            for fn in VALIDATES:
                self.assertEqual(outcome(fn, cfg), want, (fn.__name__, cfg))


    def test_num_keeps_huge_values_unconverted(self):
        self.assertIsInstance(E._num(1e299, 0, 1e301, "x"), float)
        self.assertIsInstance(E._num(int(1e300), 0, 10 ** 302, "x"), int)       # >= 1e300：原样返回，避免 float 溢出
        self.assertIsInstance(E._num(10 ** 301, 0, 10 ** 302, "x"), int)
        self.assertEqual(E._num(5, 0, 10, "x"), 5.0)

    def test_schema_and_validate_agree_on_boundaries(self):
        """schema 的常数（max_len 16 等）与 validate 的常数必须一致：对每个边界用例，参考判定与 validate_good 同判。"""
        from qdcheck import fuzz as F
        cases = [with_(symbol="A" * 16), with_(symbol="A" * 17), with_(period=500), with_(period=501), with_(period=0),
                 with_(fee_rate=0.01), with_(fee_rate=0.0100001), with_(leverage=20.0), with_(leverage=20.5),
                 with_(symbols=["A" * 8]), with_(symbols=["A" * 9]), with_(symbols=["A"] * 5),
                 with_(risk={"max_drawdown": 0.1, "window": 1000}), with_(risk={"max_drawdown": 0.1, "window": 1001}),
                 with_(risk={"max_drawdown": 0.1, "stop_loss": 0.5}), with_(risk={"max_drawdown": 0.1, "stop_loss": 0.51})]
        for cfg in cases:
            schema_ok = not F.schema_errors(E.BACKTEST_SCHEMA, cfg)
            self.assertEqual(schema_ok, outcome(E.validate_good, cfg) == "pass", cfg)
        for fn in (E.validate_good, E.validate_keyerror, E.validate_recursion):
            self.assertEqual(outcome(fn, with_(period=0)), "controlled", fn.__name__)

    def test_toy_engine_defaults_and_price_path(self):
        import random
        self.assertEqual(E.deterministic_engine({}), E.deterministic_engine({"n_bars": 60, "seed": 1, "cash": 1000.0}))
        self.assertNotEqual(E.deterministic_engine({}), E.deterministic_engine({"n_bars": 59}))
        syms, prices = E._toy_prices({"n_bars": 3, "seed": 2, "symbols": ["A", "B"]})
        rnd = random.Random(2)
        want_a1 = 100.0 * (1.0 + 0.02 * (rnd.random() - 0.5))
        want_b1 = 100.0 * (1.0 + 0.02 * (rnd.random() - 0.5))
        self.assertEqual((prices["A"][1], prices["B"][1]), (want_a1, want_b1))
        self.assertEqual(len(prices["A"]), 3)

    def test_the_designed_bugs_and_only_those(self):
        for bad in (None, [], "cfg", 42):
            for fn in VALIDATES:
                self.assertEqual(outcome(fn, bad), "controlled", (fn.__name__, bad))
        self.assertEqual(outcome(E.validate_keyerror, with_(period=KeyError)), "KeyError")
        self.assertEqual(outcome(E.validate_keyerror, with_(symbol=KeyError)), "KeyError")
        self.assertEqual(outcome(E.validate_good, with_(period=KeyError)), "controlled")
        self.assertEqual(outcome(E.validate_zerodiv, with_(period=0)), "ZeroDivisionError")
        self.assertEqual(outcome(E.validate_good, with_(period=0)), "controlled")
        self.assertEqual(outcome(E.validate_zerodiv, with_(period=-1)), "controlled")
        self.assertEqual(outcome(E.validate_zerodiv, {"period": 0}), "ZeroDivisionError")
        self.assertEqual(outcome(E.validate_zerodiv, {"symbol": "X"}), "controlled")
        self.assertEqual(outcome(E.validate_recursion, with_(symbol="A" * 5000)), "RecursionError")
        self.assertEqual(outcome(E.validate_good, with_(symbol="A" * 5000)), "controlled")
        self.assertEqual(outcome(E.validate_recursion, with_(symbol="A" * 500)), "controlled")
        self.assertEqual(outcome(E.validate_recursion, with_(symbol="é" * 5000)), "controlled")
        self.assertTrue(E._all_printable("") and E._all_printable("abc"))
        self.assertFalse(E._all_printable("ab\x00") or E._all_printable("é"))


def replay(task, result):
    """独立重放 toy 引擎的成交，核对每一笔与最终现金 / 权益。"""
    syms, prices = E._toy_prices(task)
    cash, held = float(task.get("cash", 1000.0)), {s: 0 for s in syms}
    for tr in result["trades"]:
        s, t, q, px = tr["symbol"], tr["bar"], tr["qty"], tr["price"]
        assert 1 <= t < task.get("n_bars", 60), t
        assert px == prices[s][t], (s, t)
        if q > 0:
            assert prices[s][t] > prices[s][t - 1]
            assert q == int(min(cash * 0.3, 400.0) // px) and q > 0
            cash -= q * px
            held[s] += q
        else:
            assert prices[s][t] < prices[s][t - 1] and -q == held[s] > 0
            cash += held[s] * px
            held[s] = 0
    assert cash == result["final_cash"]
    assert result["final_equity"] == cash + sum(held[s] * prices[s][-1] for s in syms)
    assert result["n_trades"] == len(result["trades"])
    return held


class TestToyEngines(unittest.TestCase):
    def test_trades_replay_exactly(self):
        for task in ({"n_bars": 60}, {"n_bars": 200, "seed": 3, "cash": 5000.0}, {"n_bars": 5, "symbols": ["A"]}):
            for fn in (E.deterministic_engine, E.set_order_engine):
                res = fn(task)
                replay(task, res)
            res = E.deterministic_engine(task)
            if task["n_bars"] >= 60:
                self.assertGreater(sum(1 for t in res["trades"] if t["qty"] < 0), 5)
                self.assertGreater(sum(1 for t in res["trades"] if t["qty"] > 0), 5)
        det = E.deterministic_engine({"n_bars": 60})
        bars = [t["bar"] for t in det["trades"]]
        self.assertEqual(bars, sorted(bars))
        by_bar = {}
        for t in det["trades"]:
            if t["qty"] > 0:
                by_bar.setdefault(t["bar"], []).append(t["symbol"])
        self.assertTrue(all(v == sorted(v) for v in by_bar.values()))
        syms, prices = E._toy_prices({"n_bars": 7, "seed": 2})
        self.assertEqual(syms, list(E.SYMBOLS))
        self.assertTrue(all(len(p) == 7 and p[0] == 100.0 for p in prices.values()))
        self.assertTrue(all(abs(b / a - 1) <= 0.01 for p in prices.values() for a, b in zip(p, p[1:])))

    def test_noisy_engine_factor(self):
        base = E.deterministic_engine({"n_bars": 60})
        eps = 1e-6 * (hash("noise-probe") % 1000) / 1000.0
        noisy = E.noisy_engine({"n_bars": 60, "noise": 1e-6})
        self.assertEqual(noisy["final_equity"], base["final_equity"] * (1.0 + eps))
        self.assertEqual([t["price"] for t in noisy["trades"]], [t["price"] * (1.0 + eps) for t in base["trades"]])
        self.assertEqual(noisy["final_cash"], base["final_cash"])
        self.assertEqual(E.noisy_engine({"n_bars": 60}), base)

    def test_qbt_adapter(self):
        task = {"strategy": "grid", "n": 3000, "seed": 4, "every": 37}
        out = E.qbt_backtest(task)
        from qbt.data import generate
        from qbt.engine import run_backtest
        from strategies import REGISTRY
        res = run_backtest(generate(n=3000, seed=4), REGISTRY["grid"]())
        self.assertEqual(out["equity_sampled"], res.equity[::37])
        self.assertEqual(out["final_equity"], res.final_equity)
        self.assertEqual(len(out["fills"]), len(res.fills))
        self.assertEqual(out["orders"], len(res.orders))
        self.assertEqual(out["fills"][0]["bar"], res.fills[0].bar)
        self.assertEqual(len(E.qbt_backtest({"n": 500})["equity_sampled"]), 10)


if __name__ == "__main__":
    unittest.main()
