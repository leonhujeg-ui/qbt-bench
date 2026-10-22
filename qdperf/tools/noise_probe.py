"""机器噪声探针（REPORT §2.3 的数字出处）：同一段纯 Python 空转，比较“进程内 best-of-N”与“跨进程 best-of-N”
做出来的三遍基线差多少，以及不同进程之间的“快慢”差多少。

    python3.12 -m qdperf.tools.noise_probe [--trials 5] [--n 5] [--procs 12] [--out qdperf/results/g2_noise_probe.json]

- 进程快慢：procs 个全新进程，各自进程内 best-of-n，折成 ns/次；
- 进程内 best-of-n 的三遍基线：3 个全新进程，每个进程内连跑 n 次取最快 → 3 个值的相对极差；
- 跨进程 best-of-n 的三遍基线：3 遍，每遍 n 个全新进程各跑 1 次取最快 → 3 个值的相对极差；
两种做法交替进行（同一时间窗），重复 trials 次，报每次的相对极差和中位数。
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from qdperf import common               # noqa: E402

LOOPS = 2_000_000


def spin(loops):
    x = 0
    for i in range(loops):
        x += i & 7
    return x


def _child(n, loops):
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        spin(loops)
        out.append(time.perf_counter() - t0)
    return out


def _proc(n, loops):
    """一个全新进程里跑 n 次，返回 n 个耗时（秒）。"""
    proc = subprocess.run([sys.executable, "-m", "qdperf.tools.noise_probe", "_child", str(n), str(loops)], cwd=REPO,
                          capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def probe(trials=5, n=5, procs=12, loops=LOOPS):
    ns = 1e9 / loops
    per_proc = [min(_proc(n, loops)) * ns for _ in range(procs)]
    rows = []
    for _ in range(trials):
        inproc, cross = [], []
        for _r in range(3):                                   # 两种做法交替，慢漂移同时落到两边
            inproc.append(min(_proc(n, loops)) * ns)
            cross.append(min(_proc(1, loops)[0] for _ in range(n)) * ns)
        rows.append({"inproc_ns": inproc, "cross_ns": cross, "inproc_spread": common.rel_spread(inproc),
                     "cross_spread": common.rel_spread(cross)})
    summary = {"per_process_ns_min": min(per_proc), "per_process_ns_max": max(per_proc),
               "per_process_spread": common.rel_spread(per_proc),
               "inproc_spread_median": statistics.median(r["inproc_spread"] for r in rows),
               "cross_spread_median": statistics.median(r["cross_spread"] for r in rows),
               "inproc_spread_max": max(r["inproc_spread"] for r in rows),
               "cross_spread_max": max(r["cross_spread"] for r in rows)}
    summary["cross_more_stable"] = summary["cross_spread_median"] < summary["inproc_spread_median"]
    return {"command": f"python3.12 -m qdperf.tools.noise_probe --trials {trials} --n {n} --procs {procs}",
            "machine": common.machine(), "loops": loops, "n": n, "procs": procs, "trials": trials,
            "per_process_ns": per_proc, "trials_detail": rows, "summary": summary}


def main(argv=None):
    if argv is None and len(sys.argv) > 1 and sys.argv[1] == "_child":
        print(json.dumps(_child(int(sys.argv[2]), int(sys.argv[3]))))
        return 0
    ap = argparse.ArgumentParser(prog="python3.12 -m qdperf.tools.noise_probe")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--procs", type=int, default=12)
    ap.add_argument("--out", default=os.path.join(common.RESULTS, "g2_noise_probe.json"))
    a = ap.parse_args(argv)
    res = probe(a.trials, a.n, a.procs)
    common.write_json(a.out, res)
    s = res["summary"]
    print(f"NOISE_PROBE per_process_ns={s['per_process_ns_min']:.1f}..{s['per_process_ns_max']:.1f} "
          f"(spread {s['per_process_spread'] * 100:.1f}%) inproc_best_of_{a.n}_spread_median={s['inproc_spread_median'] * 100:.1f}% "
          f"cross_best_of_{a.n}_spread_median={s['cross_spread_median'] * 100:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
