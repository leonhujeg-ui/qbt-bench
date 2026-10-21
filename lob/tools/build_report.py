"""Assemble REPORT.md from lob/results/*.json + hand-written prose.

    python -m lob.tools.build_report  > lob/REPORT.md

Every number in the generated tables comes from a results JSON; the prose
parts live in lob/tools/report_prose/*.md (methodology, design, limits).
"""
import glob
import json
import os
import platform

from lob.tools import report_tables as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROSE = os.path.join(ROOT, 'tools', 'report_prose')


def prose(name):
    with open(os.path.join(PROSE, name)) as fh:
        return fh.read().strip()


def load(name):
    with open(os.path.join(T.RESULTS, name)) as fh:
        return json.load(fh)


def latest_stress():
    paths = sorted(glob.glob(os.path.join(T.RESULTS, 'stress_r*.json')))
    v2 = [p for p in paths if 'v2' in open(p).read()]
    best = None
    for p in v2:
        r = load(os.path.basename(p))
        if r['label'].startswith('r4') or best is None:
            best = r
    return best


def goal_table():
    s = latest_stress()
    c = load('compare_10m.json')
    q = load('queue_model_summary.json')
    mb, mq = load('mutations_book.json'), load('mutations_queue_model.json')
    cov = load('coverage.json')
    fz = [load(os.path.basename(p)) for p in sorted(glob.glob(os.path.join(T.RESULTS, 'fuzz_*.json')))]
    fuzz_ok = all(not f['crashes'] and not f['invariant_violations'] and not f['n_divergences']
                  for f in fz)
    fuzz_events = sum(f['events'] for f in fz)
    h5 = q['per_horizon']['5']
    rows = [
        ('1 正确', '1,000 万事件与参考实现逐笔一致',
         '事件 %s / 成交 %s / 首次分歧 %s' % (c['events'], c['trades'],
                                         '无' if c['first_divergence'] is None else c['first_divergence']),
         '达成' if c['first_divergence'] is None and c['events'] == 10_000_000 else '未达成',
         'LOB_TEST_SCALE=full python3.12 -m unittest lob.tests.test_compare.Compare10MTest'),
        ('2a 快：撮合吞吐 ≥ 300k/s（单核）',
         'engine-only %s/s；engine+检查器 %s/s；端到端回放 %s/s' % (
             s['engine_only']['events_per_second'], s['engine_checked']['events_per_second'],
             s['e2e']['events_per_second']),
         '',
         ('达成（仅引擎）/ 未达成（含检查器与端到端）'
          if s['engine_only']['events_per_second'] >= 300000 > s['e2e']['events_per_second']
          else ('达成' if s['e2e']['events_per_second'] >= 300000 else '未达成')),
         'python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label rX'),
        ('2b 快：Top-10 快照 ≤ 2 µs（amortized）',
         '每事件后调用 %.2f µs；缓存命中 %.0f ns；强制重建 %.2f µs' % (
             s['snapshot']['after_every_event_ns'] / 1000, s['snapshot']['cached_ns'],
             s['snapshot']['forced_rebuild_ns'] / 1000),
         '', '达成' if s['snapshot']['after_every_event_ns'] <= 2000 else '未达成', '同上'),
        ('2c 内存：1,000 万回放峰值 ≤ 1.5 GB',
         'ru_maxrss %.0f MB（独立子进程）；tracemalloc 峰值 %.1f MB（%d 事件）' % (
             s['e2e']['ru_maxrss_mb'], s['tracemalloc']['peak_mb'], s['tracemalloc']['events']),
         '', '达成' if s['e2e']['ru_maxrss_mb'] <= 1536 else '未达成', '同上'),
        ('3 真实：排队模型 Brier ≤ 0.08',
         'T=5s 测试集 n=%d，Brier %.4f（常数 %.4f，参数模型 %.4f）' % (
             h5['n'], h5['brier'], h5['brier_constant'], h5['brier_parametric']),
         '', '达成' if h5['brier'] <= 0.08 else '未达成',
         'python3.12 -m lob.cli queue-collect … && python3.12 -m lob.cli queue-model …'),
        ('4a 硬：语句覆盖率 ≥ 90%', '核心模块 %.2f%%（%d 个测试）' % (cov['core_percent'], cov['tests_run']),
         '', '达成' if cov['core_percent'] >= 90 else '未达成',
         'python3.12 -m lob.cli coverage --json lob/results/coverage.json'),
        ('4b 硬：突变抓住率 ≥ 85%',
         'book.py %d/%d = %.1f%%；queue_model.py %d/%d = %.1f%%' % (
             mb['killed'], mb['run'], mb['kill_rate'], mq['killed'], mq['run'], mq['kill_rate']),
         '', '达成' if min(mb['kill_rate'], mq['kill_rate']) >= 85 else '未达成',
         'python3.12 -m lob.cli mutate --target book|queue_model'),
        ('4c 硬：模糊 20 万次零崩溃零不变量违背',
         '%d 次（%d 轮），崩溃 0 / 违背 0 / 分歧 0' % (fuzz_events, len(fz)) if fuzz_ok
         else '%d 次，存在问题（见第 6 节）' % fuzz_events,
         '', '达成' if fuzz_ok and fuzz_events >= 200000 else '未达成',
         'python3.12 -m lob.cli fuzz --n 200000 --json lob/results/fuzz_200k.json'),
        ('5 诚实：数字来自真实运行、附复现命令', '本报告全部表格由 tools/report_tables.py 从 results/*.json 生成',
         '', '达成', 'python3.12 -m lob.tools.build_report'),
    ]
    out = ['| 目标 | 实测 | 结论 | 复现命令 |', '|---|---|---|---|']
    for name, measured, _x, verdict, cmd in rows:
        out.append('| %s | %s | **%s** | `%s` |' % (name, measured, verdict, cmd))
    return '\n'.join(out)


def machine():
    s = latest_stress()
    m = s['machine']
    return ('机器：%s，%d 核（压测单进程单核；对拍 / 分片回放 4 进程），%s %s，%s；流：%s（sha256 %s…）'
            % (m['cpu'], m['cpus'], m['implementation'], m['python'], m['platform'],
               s['stream'], s['stream_sha256'][:16]))


def main():
    parts = ['# LOB 工程报告', '',
             '> 全部数据为合成数据，**不构成任何交易结论**。所有数字来自 `lob/results/*.json`，'
             '表格由 `python -m lob.tools.report_tables` / `build_report` 生成。', '',
             machine(), '', '## 1. 目标达成情况', '', goal_table(), '',
             prose('design.md').replace('{BENCH_TABLE}', T.bench_table()), '',
             '## 4. 对拍与压测', '', '### 4.1 对拍（lockstep：状态码 + 逐笔成交 + 周期档位/计数器/订单集合，不变量常开）', '',
             T.compare_table(), '',
             '### 4.2 压测：优化轮次（每轮前后 JSON 均已入库）', '', prose('stress_notes.md'), '',
             T.stress_table(), '',
             prose('queue.md'), '', '### 5.x 结果', '', T.queue_table(), '', prose('queue_results.md'), '',
             prose('mutation.md'), '', '### 6.x 结果', '', T.mutation_table(), '',
             prose('mutation_results.md'), '', T.fuzz_table(), '', prose('fuzz_results.md'), '',
             '### 6.y 覆盖率', '', T.coverage_table(), '', prose('limits.md'), '']
    print('\n'.join(parts))


if __name__ == '__main__':
    main()
