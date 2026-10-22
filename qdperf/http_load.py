"""HTTP 压测与延迟分位（G4）：纯 http.client + 线程，缺省只允许打 127.0.0.1 / localhost。

    python3.12 -m qdperf.http_load ramp --base http://127.0.0.1:8080 --manifest urls.json [--cmax 32] [--seconds 3]
    python3.12 -m qdperf.http_load ab --a http://127.0.0.1:8080 --b http://127.0.0.1:8081 --manifest urls.json
                                      [--c 8] [--seconds 3] [--tol 0.10]

清单（qdperf.urls.v1）：targets = [{name, method, path, headers, body, weight, expect}]；body 为对象时按 JSON 发送。
- 每档并发 C：C 个线程各持一条 keep-alive 连接闭环发请求（按 weight 抽目标，线程内固定 seed），先预热 warmup 秒
  （不计），再计 seconds 秒；记每个请求的延迟（发出到读完响应体）与状态；状态不在 expect（缺省 2xx/3xx）或异常 = 错误。
- 每档输出每个路径与合计的 p50 / p95 / p99 / 错误率 / 吞吐；拐点 = 第一个“下一档吞吐增长 < growth（缺省 10%）”的 C。
- A/B：按 A、B、A 交替各跑一遍（同 C、同时长），A 两遍的差异给出噪声幅度；每个路径的 p50 / p99 与 A 两遍均值比，
  容差 = max(手动容差, 2 × 噪声幅度)（与 profile_gate 同口径），超出即红。
stdout 第一行：ramp → ``HTTP_LOAD knee=<C|none> max_rps=<x> at_c=<C> p99_ms=<合计 p99（最大吞吐那档）> errors=<错误率>``；
ab → ``REGRESSION <路径>.<p50|p99>_ms=<A>→<B> (+<百分比>)`` 或 ``OK <路径>.p99_ms=<A>→<B> (<±百分比>) tol=<容差>``。
退出码：ramp 0 / 1（任何路径错误率 > --max-error-rate）；ab 0 绿 / 1 红；2 输入错误（含非本机地址且没给 --allow-remote）。
"""
import argparse
import http.client
import json
import random
import sys
import threading
import time
import urllib.parse

from qdperf import common

FORMAT = "qdperf.urls.v1"
METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD")


class LoadError(ValueError):
    """输入错误；CLI 退出码 2。"""


# ---- 清单 ------------------------------------------------------------------------------
def load_manifest(src):
    if isinstance(src, str):
        try:
            m = common.read_json(src)
        except (OSError, json.JSONDecodeError) as exc:
            raise LoadError(f"manifest unreadable: {exc}") from exc
    else:
        m = json.loads(json.dumps(src))
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise LoadError(f"manifest.format must be {FORMAT!r}")
    ts = m.get("targets")
    if not isinstance(ts, list) or not ts:
        raise LoadError("manifest.targets must be a non-empty list")
    names = set()
    for k, t in enumerate(ts):
        _check_target(k, t, names)
    return m


def _check_target(k, t, names):
    if not isinstance(t, dict):
        raise LoadError(f"targets[{k}] must be an object")
    t.setdefault("method", "GET")
    if t["method"] not in METHODS:
        raise LoadError(f"targets[{k}].method must be one of {METHODS}")
    if not isinstance(t.get("path"), str) or not t["path"].startswith("/"):
        raise LoadError(f"targets[{k}].path must start with '/'")
    t.setdefault("name", t["path"])
    if t["name"] in names:
        raise LoadError(f"duplicate target name {t['name']!r}")
    names.add(t["name"])
    w = t.setdefault("weight", 1)
    if isinstance(w, bool) or not isinstance(w, (int, float)) or not w > 0 or w == float("inf"):
        raise LoadError(f"targets[{k}].weight must be a finite number > 0")
    if not isinstance(t.setdefault("headers", {}), dict):
        raise LoadError(f"targets[{k}].headers must be an object")
    exp = t.setdefault("expect", None)
    if exp is not None and not (isinstance(exp, list) and all(isinstance(x, int) for x in exp)):
        raise LoadError(f"targets[{k}].expect must be a list of status codes")


def parse_base(base, allow_remote=False):
    u = urllib.parse.urlsplit(base)
    if u.scheme != "http" or not u.hostname:
        raise LoadError(f"base must be http://host:port, got {base!r}")
    if not allow_remote and not common.is_loopback_url(base):
        raise LoadError(f"refusing non-loopback host {u.hostname!r} (pass --allow-remote to override)")
    return u.hostname, u.port or 80, u.path.rstrip("/")


def _encode(t):
    body = t.get("body")
    headers = dict(t["headers"])
    if body is None:
        return None, headers
    if isinstance(body, (dict, list)):
        headers.setdefault("Content-Type", "application/json")
        return json.dumps(body).encode(), headers
    return str(body).encode(), headers


def pick(targets, cum, r):
    """按累计权重抽目标（r ∈ [0, 1)）。"""
    for t, c in zip(targets, cum):
        if r < c:
            return t
    return targets[-1]


def cumulative(targets):
    total = sum(t["weight"] for t in targets)
    acc, out = 0.0, []
    for t in targets:
        acc += t["weight"] / total
        out.append(acc)
    return out


# ---- 一档并发 ----------------------------------------------------------------------------
def _ok(t, status):
    return status in t["expect"] if t["expect"] else 200 <= status < 400


def _worker(host, port, prefix, targets, cum, seed, t_start, t_end, timeout, out):
    rnd = random.Random(seed)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    prepared = [(t,) + _encode(t) for t in targets]
    while True:
        now = time.perf_counter()
        if now >= t_end:
            break
        t, body, headers = pick(prepared, cum, rnd.random())
        a = time.perf_counter()
        try:
            conn.request(t["method"], prefix + t["path"], body=body, headers=headers)
            resp = conn.getresponse()
            resp.read()
            status = resp.status
            if resp.will_close:
                conn.close()
        except (OSError, http.client.HTTPException):
            status = None
            conn.close()
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        b = time.perf_counter()
        if a >= t_start:
            out.append((t["name"], b - a, status is not None and _ok(t, status), status))
    conn.close()


def run_step(base, manifest, c, seconds=3.0, warmup=0.3, seed=0, timeout=10.0, allow_remote=False):
    """一档：C 个线程闭环跑 warmup + seconds 秒。→ {c, seconds, per_target: {...}, total: {...}}。"""
    m = load_manifest(manifest)
    host, port, prefix = parse_base(base, allow_remote)
    targets = m["targets"]
    cum = cumulative(targets)
    t_start = time.perf_counter() + warmup
    t_end = t_start + seconds
    outs = [[] for _ in range(c)]
    threads = [threading.Thread(target=_worker, args=(host, port, prefix, targets, cum, seed * 1000 + k, t_start, t_end,
                                                      timeout, outs[k]), daemon=True) for k in range(c)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(seconds + warmup + timeout + 30)
    recs = [r for o in outs for r in o]
    per = {t["name"]: _stats([r for r in recs if r[0] == t["name"]], seconds) for t in targets}
    return {"c": c, "seconds": seconds, "warmup": warmup, "per_target": per, "total": _stats(recs, seconds)}


def _stats(recs, seconds):
    ok = [r[1] for r in recs if r[2]]
    lat = common.summarize(ok, 1000.0)
    statuses = {}
    for r in recs:
        key = "error" if r[3] is None else str(r[3])
        statuses[key] = statuses.get(key, 0) + 1
    n = len(recs)
    return {"requests": n, "ok": len(ok), "errors": n - len(ok), "error_rate": (n - len(ok)) / n if n else 0.0,
            "rps": n / seconds, "ok_rps": len(ok) / seconds, "latency_ms": lat, "statuses": statuses}


# ---- 爬坡 / 拐点 ----------------------------------------------------------------------------
def doubling(cmax):
    out, c = [], 1
    while c < cmax:
        out.append(c)
        c *= 2
    return out + [cmax]


def find_knee(steps, growth=0.10):
    """第一个“下一档吞吐增长 < growth”的并发数；一直在涨返回 None。"""
    for a, b in zip(steps, steps[1:]):
        if b["total"]["ok_rps"] < a["total"]["ok_rps"] * (1.0 + growth):
            return a["c"]
    return None


def ramp(base, manifest, cmax=32, seconds=3.0, steps=None, growth=0.10, warmup=0.3, seed=0, allow_remote=False):
    m = load_manifest(manifest)
    cs = list(steps) if steps else doubling(cmax)
    if not cs or any((not isinstance(c, int)) or c < 1 for c in cs) or cs != sorted(set(cs)):
        raise LoadError("concurrency steps must be increasing ints >= 1")
    res = [run_step(base, m, c, seconds, warmup, seed, allow_remote=allow_remote) for c in cs]
    best = max(res, key=lambda r: r["total"]["ok_rps"])
    return {"format": "qdperf.http_load.v1", "mode": "ramp", "base": base, "steps": res, "growth": growth,
            "knee": find_knee(res, growth), "max_rps": best["total"]["ok_rps"], "max_rps_c": best["c"],
            "machine": common.machine()}


# ---- A/B ------------------------------------------------------------------------------
GATED = ("p50", "p99")


def ab(base_a, base_b, manifest, c=8, seconds=3.0, tol=0.10, warmup=0.3, seed=0, allow_remote=False):
    """A、B、A 交替各跑一档；逐路径判定 B 是否变慢（p50 / p99）。"""
    m = load_manifest(manifest)
    a1 = run_step(base_a, m, c, seconds, warmup, seed, allow_remote=allow_remote)
    b = run_step(base_b, m, c, seconds, warmup, seed, allow_remote=allow_remote)
    a2 = run_step(base_a, m, c, seconds, warmup, seed, allow_remote=allow_remote)
    verdict = judge(a1, a2, b, tol)
    return dict(verdict, format="qdperf.http_load.v1", mode="ab", a=base_a, b=base_b, c=c, seconds=seconds,
                runs={"a1": a1, "b": b, "a2": a2}, machine=common.machine())


def judge(a1, a2, b, tol):
    """纯判定：每路径每个分位 A 均值 vs B，容差 = max(tol, 2 × |A1 - A2| / min(A1, A2))。"""
    if tol < 0:
        raise LoadError("tol must be >= 0")
    rows = []
    for name in a1["per_target"]:
        for q in GATED:
            x1, x2 = a1["per_target"][name]["latency_ms"][q], a2["per_target"][name]["latency_ms"][q]
            y = b["per_target"].get(name, {}).get("latency_ms", {}).get(q)
            if x1 is None or x2 is None or y is None:
                rows.append({"target": name, "q": q, "a": None, "b": y, "red": True, "reason": "no successful requests"})
                continue
            a = (x1 + x2) / 2.0
            noise = common.rel_spread([x1, x2])
            t = max(tol, 2.0 * noise)
            ch = common.rel_change(a, y)
            rows.append({"target": name, "q": q, "a": a, "b": y, "change": ch, "noise": noise, "tolerance": t,
                         "red": ch > t + 1e-9})
    red = [r for r in rows if r["red"]]
    if red:
        w = max(red, key=lambda r: (r.get("change", float("inf")) - r.get("tolerance", 0.0), r["target"], r["q"]))
        if w["a"] is None:
            first = f"REGRESSION {w['target']}.{w['q']}_ms=-→- ({w['reason']})"
        else:
            first = f"REGRESSION {w['target']}.{w['q']}_ms={w['a']:.2f}→{w['b']:.2f} ({common.fmt_pct(w['change'])})"
    else:
        w = max((r for r in rows if r["q"] == "p99"), key=lambda r: (r["change"], r["target"]))
        first = f"OK {w['target']}.p99_ms={w['a']:.2f}→{w['b']:.2f} ({common.fmt_pct(w['change'])}) tol={w['tolerance'] * 100:.1f}%"
    return {"verdict": "REGRESSION" if red else "OK", "first_line": first, "rows": rows}


# ---- 文本 / CLI --------------------------------------------------------------------------
def step_table(steps):
    rows = []
    for s in steps:
        for name, st in list(s["per_target"].items()) + [("合计", s["total"])]:
            lat = st["latency_ms"]
            rows.append((s["c"], name, st["requests"], f"{st['ok_rps']:.1f}", _f(lat["p50"]), _f(lat["p95"]), _f(lat["p99"]),
                         f"{st['error_rate'] * 100:.2f}%"))
    return common.text_table(["C", "路径", "请求", "成功/秒", "p50ms", "p95ms", "p99ms", "错误率"], rows)


def _f(x):
    return "-" if x is None else f"{x:.2f}"


def first_line_ramp(r):
    best = next(s for s in r["steps"] if s["c"] == r["max_rps_c"])
    err = max(s["total"]["error_rate"] for s in r["steps"])
    return (f"HTTP_LOAD knee={r['knee'] if r['knee'] is not None else 'none'} max_rps={r['max_rps']:.1f} "
            f"at_c={r['max_rps_c']} p99_ms={_f(best['total']['latency_ms']['p99'])} errors={err:.4f}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3.12 -m qdperf.http_load", description="HTTP 压测 / A/B")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("ramp")
    r.add_argument("--base", required=True)
    r.add_argument("--cmax", type=int, default=32)
    r.add_argument("--steps", default=None, help="逗号分隔的并发档，缺省 1,2,4,...,cmax")
    r.add_argument("--growth", type=float, default=0.10)
    r.add_argument("--max-error-rate", type=float, default=0.01)
    b = sub.add_parser("ab")
    b.add_argument("--a", required=True)
    b.add_argument("--b", required=True)
    b.add_argument("--c", type=int, default=8)
    b.add_argument("--tol", type=float, default=0.10)
    for p in (r, b):
        p.add_argument("--manifest", required=True)
        p.add_argument("--seconds", type=float, default=3.0)
        p.add_argument("--warmup", type=float, default=0.3)
        p.add_argument("--allow-remote", action="store_true", help="允许非本机地址（缺省拒绝：零出网）")
        p.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "ramp":
            steps = [int(x) for x in a.steps.split(",")] if a.steps else None
            res = ramp(a.base, a.manifest, a.cmax, a.seconds, steps, a.growth, a.warmup, allow_remote=a.allow_remote)
            print(first_line_ramp(res))
            print(step_table(res["steps"]))
            bad = any(st["error_rate"] > a.max_error_rate for s in res["steps"] for st in s["per_target"].values())
            rc = 1 if bad else 0
        else:
            res = ab(a.a, a.b, a.manifest, a.c, a.seconds, a.tol, a.warmup, allow_remote=a.allow_remote)
            print(res["first_line"])
            print(common.text_table(["路径", "分位", "A ms", "B ms", "变化", "容差"],
                                    [(x["target"], x["q"], _f(x["a"]), _f(x["b"]),
                                      common.fmt_pct(x["change"]) if "change" in x else "-",
                                      f"{x['tolerance'] * 100:.1f}%" if "tolerance" in x else "-") for x in res["rows"]]))
            rc = 1 if res["verdict"] == "REGRESSION" else 0
    except (LoadError, ValueError) as exc:
        print(f"HTTP_LOAD ERROR {exc}")
        return 2
    if a.json:
        common.write_json(a.json, res)
    return rc


if __name__ == "__main__":
    sys.exit(main())
