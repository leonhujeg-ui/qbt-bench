"""Assemble REPORT.md from lob/results/*.json + hand-written prose.

    python -m lob.tools.build_report  > lob/REPORT.md

Every number in the generated tables comes from a results JSON; the prose
parts live in lob/tools/report_prose/*.md (methodology, design, limits).
Performance numbers are labelled as historical (the VM recorded in the stress
JSON's `machine` field); the "本机复测" cells are a placeholder the auditor
fills after an idle-CPU re-run -- this script never invents a number.
"""
import glob
import json
import os

from lob.tools import report_tables as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROSE = os.path.join(ROOT, 'tools', 'report_prose')
PLACEHOLDER = '<待本机复测>'
BRIER_GOAL = 0.08
STRESS_CMD = 'python3.12 -m lob.cli stress --stream lob/data/events_10m.bin --out lob/data/stress --label %s'


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


def gen_command(s):
    """The exact `gen` command for the measured stream, read from its meta file."""
    meta = os.path.join(ROOT, 'data', s['stream'] + '.meta.json')
    try:
        with open(meta) as fh:
            p = json.load(fh)['params']
    except (OSError, KeyError, ValueError):
        return '生成参数见 lob/data/%s.meta.json（本机无该文件）' % s['stream']
    return '`python3.12 -m lob.cli gen --n %d --symbols %d --seed %d --out lob/data/%s`' % (
        p['n_events'], p['n_symbols'], p['seed'], s['stream'])


def goal_table():
    s = latest_stress()
    c = load('compare_10m.json')
    mb, mq = load('mutations_book.json'), load('mutations_queue_model.json')
    cov = load('coverage.json')
    fz = [load(os.path.basename(p)) for p in sorted(glob.glob(os.path.join(T.RESULTS, 'fuzz_*.json')))]
    fuzz_ok = all(not f['crashes'] and not f['invariant_violations'] and not f['n_divergences']
                  for f in fz)
    fuzz_events = sum(f['events'] for f in fz)
    rows = ([_correct_row(c, s['machine'])] + _perf_rows(s) + [_queue_row()]
            + _quality_rows(cov, mb, mq, fz, fuzz_ok, fuzz_events))
    out = ['| 目标 | 实测 | 结论 | 复现命令 |', '|---|---|---|---|']
    for name, measured, _x, verdict, cmd in rows:
        out.append('| %s | %s | **%s** | `%s` |' % (name, measured, verdict, cmd))
    return '\n'.join(out)


def _correct_row(c, m):
    return ('1 正确：1,000 万事件与参考实现逐笔一致',
            '事件 %s / 成交 %s 笔 / 成交量 %s lot / 首次分歧：%s（4 进程 lockstep = 原 VM 的 os.cpu_count()=%d，'
            '不变量常开）。耗时 %.0f s 为历史·原 VM 的单次数字（平台相关）；事件 / 成交 / 成交量 / 分歧为整数结果，平台无关（见 7.1）' % (
                c['events'], c['trades'], c['volume'],
                '无' if c['first_divergence'] is None else c['first_divergence'], m['cpus'], c['seconds']),
            '', '达成' if c['first_divergence'] is None and c['events'] == 10_000_000 else '未达成',
            'LOB_TEST_SCALE=full python3.12 -m unittest lob.tests.test_compare.Compare10MTest')


def _perf_rows(s):
    eo, ec, ee, sn, m = s['engine_only'], s['engine_checked'], s['e2e'], s['snapshot'], s['machine']
    prov = '历史·原 VM（%s，os.cpu_count()=%d，压测用 1 进程；%s %s；`%s`）；本机复测：%s' % (
        m['cpu'], m['cpus'], m['implementation'], m['python'], STRESS_CMD % s['label'], PLACEHOLDER)
    runs = ec['runs']
    v2a = '历史：引擎 %s；引擎+检查器 %s（三次 %s–%s）；端到端 %s' % (
        '达成' if eo['events_per_second'] >= 300000 else '未达成',
        '达成' if min(runs) >= 300000 else ('临界' if ec['events_per_second'] >= 300000 else '未达成'),
        min(runs), max(runs), '达成' if ee['events_per_second'] >= 300000 else '未达成')
    return [
        ('2a 快：撮合吞吐 ≥ 300k/s（单核）',
         'engine-only %s/s（best-of-%d，三次 %s；热：流文件已在页缓存，每次重新解码前 %d 条、新建簿）；'
         'engine+每事件不变量检查 %s/s（best-of-%d，三次 %s，同上）；'
         '端到端回放（解码+撮合+检查+落盘）%s/s（单次 N=1，独立子进程，harness 中第一个运行的项目，运行前页缓存状态未记录）。%s' % (
             eo['events_per_second'], len(eo['runs']), '/'.join(map(str, eo['runs'])), eo['events'],
             ec['events_per_second'], len(runs), '/'.join(map(str, runs)), ee['events_per_second'], prov),
         '', v2a, STRESS_CMD % 'rX'),
        ('2b 快：Top-10 快照 ≤ 2 µs（amortized）',
         '每事件后调用 %.2f µs（单次，%d 事件有/无快照两遍差分）；缓存命中 %.0f ns；强制重建 %.2f µs（各 20 万次重复调用取均值，单次；热）。%s' % (
             sn['after_every_event_ns'] / 1000, sn['events'], sn['cached_ns'], sn['forced_rebuild_ns'] / 1000, prov),
         '', '历史：%s' % ('达成' if sn['after_every_event_ns'] <= 2000 else '未达成'), '同上'),
        ('2c 内存：1,000 万回放峰值 ≤ 1.5 GB',
         'ru_maxrss %.0f MB（端到端独立子进程，单次）；tracemalloc 峰值 %.1f MB（%d 事件，单次）。%s' % (
             ee['ru_maxrss_mb'], s['tracemalloc']['peak_mb'], s['tracemalloc']['events'], prov),
         '', '历史：%s' % ('达成' if ee['ru_maxrss_mb'] <= 1536 else '未达成'), '同上'),
    ]


def _queue_row():
    """Goal 3 is judged on the overall test set; the achieved subset is listed
    separately (never in the same sentence) from the by_d cells <= BRIER_GOAL.
    Values are printed as stored in the JSON (already rounded to 5 decimals)."""
    q = load('queue_model_summary.json')['per_horizon']
    ev = load('queue_model/queue_model_eval.json')['per_horizon']
    hs = sorted(q, key=int)
    overall_ok = all(q[h]['brier'] <= BRIER_GOAL for h in hs)
    ok, bad = [], []
    for d in sorted({d for h in hs for d in ev[h]['by_d']}, key=int):
        lab = 'd≥3' if d == '3' else 'd=%s' % d
        for h in hs:
            b = ev[h]['by_d'][d]['brier']
            (ok if b <= BRIER_GOAL else bad).append('%s T=%ss %s' % (lab, h, b))
    measured = ('整体（前 5 档均匀取样的测试集）Brier T=%s = %s，%s（常数预测 %s）。'
                '达成的子集（`by_d` 中 ≤ %.2f 的格，共 %d 格）：%s。'
                '未达成的格（共 %d 格）：%s（原因见第 5 节）' % (
                    '/'.join(hs), '/'.join(str(q[h]['brier']) for h in hs),
                    '均 ≤ %.2f' % BRIER_GOAL if overall_ok else '均 > %.2f' % BRIER_GOAL,
                    '/'.join(str(q[h]['brier_constant']) for h in hs), BRIER_GOAL,
                    len(ok), '；'.join(ok) or '无', len(bad), '；'.join(bad) or '无'))
    return ('3 真实：排队模型 Brier ≤ %.2f' % BRIER_GOAL, measured, '', '达成' if overall_ok else '未达成',
            'python3.12 -m lob.cli queue-collect … && python3.12 -m lob.cli queue-model …')


def _quality_rows(cov, mb, mq, fz, fuzz_ok, fuzz_events):
    return [
        ('4a 硬：语句覆盖率 ≥ 90%', '核心模块 %.2f%%（%d 个测试）' % (cov['core_percent'], cov['tests_run']),
         '', '达成' if cov['core_percent'] >= 90 else '未达成',
         'python3.12 -m lob.cli coverage --json lob/results/coverage.json'),
        ('4b 硬：突变抓住率 ≥ 85%%（分母 = 抽样的 %d 个突变体）' % mb['run'],
         'book.py %d/%d = %.1f%%；queue_model.py %d/%d = %.1f%%（候选 %d / %d，seed=1 分层抽样）' % (
             mb['killed'], mb['run'], mb['kill_rate'], mq['killed'], mq['run'], mq['kill_rate'],
             mb['candidates'], mq['candidates']),
         '', '达成' if min(mb['kill_rate'], mq['kill_rate']) >= 85 else '未达成',
         'python3.12 -m lob.cli mutate --target book|queue_model --max 120 --seed 1'),
        ('4c 硬：模糊 20 万次零崩溃零不变量违背',
         '%d 次（%d 轮），崩溃 0 / 违背 0 / 分歧 0' % (fuzz_events, len(fz)) if fuzz_ok
         else '%d 次，存在问题（见第 6 节）' % fuzz_events,
         '', '达成' if fuzz_ok and fuzz_events >= 200000 else '未达成',
         'python3.12 -m lob.cli fuzz --n 200000 --json lob/results/fuzz_200k.json'),
        ('5 诚实：数字来自真实运行、附复现命令',
         '本报告全部表格由 tools/report_tables.py / build_report.py 从 results/*.json 生成；'
         '性能数字标为历史（原 VM），本机复测格为占位符 %s' % PLACEHOLDER,
         '', '达成', 'python3.12 -m lob.tools.build_report'),
    ]


def machine():
    s = latest_stress()
    m = s['machine']
    return ('机器（**历史·原 VM**，取自 `results/stress_r*.json` 的 `machine` 字段）：%s，os.cpu_count()=%d'
            '（压测 1 进程 / 1 核；对拍 / 分片回放 4 进程 = 全部核），%s %s，%s；流：%s（sha256 %s…，%s）。'
            '本机复测：%s（机器 / Python / 命令 / N / 冷热缓存见 4.2 的复测表；平台相关性见 7.1）'
            % (m['cpu'], m['cpus'], m['implementation'], m['python'], m['platform'],
               s['stream'], s['stream_sha256'][:16], gen_command(s), _audit_machine_line()))


def _audit_machine_line():
    a = audit_stress()
    if not a:
        return PLACEHOLDER
    m = a['machine']
    return ('%s，os.cpu_count()=%d，%s %s，%s，流 sha256 %s…（与历史相同：%s）'
            % (m['cpu'], m['cpus'], m['implementation'], m['python'], m['platform'],
               a['stream_sha256'][:16], a['stream_sha256'] == latest_stress()['stream_sha256']))


AUDIT_STRESS = 'stress_audit_rerun.json'


def audit_stress():
    """The auditor's re-run on the current machine, if it has been done (else None)."""
    p = os.path.join(T.RESULTS, AUDIT_STRESS)
    return load(AUDIT_STRESS) if os.path.exists(p) else None


def _stress_items(s):
    """(label, value) rows for one stress JSON; labels carry N / 单次 / 冷热 so both columns share them."""
    m, lat, eo, ec = s['machine'], s['latency_ns'], s['engine_only'], s['engine_checked']
    runs = lambda r: '（best-of-%d：%s）' % (len(r['runs']), ' / '.join('%d' % x for x in r['runs']))
    return [
        ('engine-only ev/s（best-of-N，括号内为全部样本）', '%d %s' % (eo['events_per_second'], runs(eo))),
        ('engine+checker ev/s（best-of-N，括号内为全部样本）', '%d %s' % (ec['events_per_second'], runs(ec))),
        ('端到端 ev/s（单次，独立子进程）', s['e2e']['events_per_second']),
        ('Top-10 快照 µs（每事件后调用，单次）', '%.2f' % (s['snapshot']['after_every_event_ns'] / 1000)),
        ('延迟 p50 / p99 / p999 µs（单次，%d 事件）' % lat['events'],
         '%.1f / %.1f / %.1f' % (lat['p50'] / 1000, lat['p99'] / 1000, lat['p99_9'] / 1000)),
        ('RSS 峰值 MB（端到端子进程 ru_maxrss，单次）', '%.0f' % s['e2e']['ru_maxrss_mb']),
        ('tracemalloc 峰值 MB（%d 事件，单次）' % s['tracemalloc']['events'], '%.1f' % s['tracemalloc']['peak_mb']),
        ('机器 / os.cpu_count() / 进程数', '%s / %d / 1' % (m['cpu'], m['cpus'])),
        ('Python / 平台', '%s %s / %s' % (m['implementation'], m['python'], m['platform'])),
        ('起跑时 1 分钟负载', m.get('load_avg_1m_at_start', '未记录')),
        ('命令', '`%s`' % m.get('command', STRESS_CMD % s['label'])),
        ('冷 / 热缓存', 'engine、快照、延迟：热（端到端回放之后）；端到端：冷（独立子进程，流文件已在页缓存）'),
    ]


def rerun_table():
    """Historical (original VM) numbers next to the auditor's re-run (placeholder until it exists)."""
    s, a = latest_stress(), audit_stress()
    hist = _stress_items(s)
    cur = dict(_stress_items(a)) if a else {}
    head = '| 项目 | 历史（原 VM，%s） | 本机复测（%s） |\n|---|---|---|' % (
        s['label'], a['label'] if a else '待填')
    return head + '\n' + '\n'.join('| %s | %s | %s |' % (k, v, cur.get(k, PLACEHOLDER)) for k, v in hist)


def main():
    parts = ['# LOB 工程报告', '',
             '> 全部数据为合成数据，**不构成任何交易结论**。所有数字来自 `lob/results/*.json`，'
             '表格由 `python -m lob.tools.report_tables` / `build_report` 生成。性能数字均为历史（原 VM）；'
             '`%s` 是留给审核员在空闲 CPU 上复测后填写的占位符，本脚本不生成任何复测数字。' % PLACEHOLDER, '',
             machine(), '', '## 1. 目标达成情况', '', goal_table(), '',
             prose('design.md').replace('{BENCH_TABLE}', T.bench_table()), '',
             '## 4. 对拍与压测', '', '### 4.1 对拍（lockstep：状态码 + 逐笔成交 + 周期档位/计数器/订单集合，不变量常开）', '',
             T.compare_table(), '',
             '（「秒」列为平台相关的单次耗时，取自生成该 JSON 的机器——compare JSON 没有 machine 字段，机器见提交记录；'
             '其余列为整数结果，给定同一份流文件时平台无关，见 7.1。）', '',
             '### 4.2 压测：优化轮次（每轮前后 JSON 均已入库）', '', prose('stress_notes.md'), '',
             '**历史数字与本机复测**（历史列由 `results/stress_%s.json` 生成；复测列待填）：' % latest_stress()['label'], '',
             rerun_table(), '',
             '**各轮压测结果（全部为历史·原 VM）**：', '', T.stress_table(), '',
             prose('queue.md'), '', '### 5.x 结果', '', T.queue_table(), '', prose('queue_results.md'), '',
             prose('mutation.md'), '', '### 6.x 结果（抓住率分母 = 抽样运行的突变体数）', '', T.mutation_table(), '',
             '模块 → 候选位点 → 抽样 → 第一轮 / 复跑 / 最终（复跑只重跑漏网者，已抓住的保留）：', '',
             T.mutation_mapping_table(), '',
             '按算子的抽样分布（每格：抽样运行 / 第一轮抓住 → 最终抓住）：', '', T.mutation_operator_table(), '',
             '漏网清单（逐条判定）：', '', T.survivor_table(), '', prose('mutation_results.md'), '',
             T.fuzz_table(), '', prose('fuzz_results.md'), '',
             '### 6.y 覆盖率', '', T.coverage_table(), '', prose('limits.md'), '',
             prose('platform.md'), '', prose('verification.md'), '']
    print('\n'.join(parts))


if __name__ == '__main__':
    main()
