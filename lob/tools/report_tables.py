"""Print the REPORT.md tables straight from lob/results/*.json (so every
number in the report is traceable to a run output).

    python -m lob.tools.report_tables
"""
import glob
import json
import os

RESULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')


def load(name):
    with open(os.path.join(RESULTS, name)) as fh:
        return json.load(fh)


def stress_table():
    runs = [load(os.path.basename(p)) for p in glob.glob(os.path.join(RESULTS, 'stress_r*.json'))]
    runs.sort(key=lambda r: ('v2' if r.get('harness') else 'v1', r['label']))
    rows = []
    for r in runs:
        rows.append('| %s | %s | %s | %s | %s | %.2f | %.1f | %.1f | %.1f | %.0f |' % (
            'v2' if r.get('harness') else 'v1', r['label'],
            r['engine_only']['events_per_second'], r['engine_checked']['events_per_second'],
            r['e2e']['events_per_second'], r['snapshot']['after_every_event_ns'] / 1000.0,
            r['latency_ns']['p50'] / 1000.0, r['latency_ns']['p99'] / 1000.0,
            r['latency_ns']['p99_9'] / 1000.0, r['e2e']['ru_maxrss_mb']))
    head = ('| harness | 轮次 | engine-only ev/s | engine+checker ev/s | 端到端 ev/s | Top-10 快照 µs '
            '| p50 µs | p99 µs | p999 µs | RSS 峰值 MB |\n|---|---|---|---|---|---|---|---|---|---|')
    return head + '\n' + '\n'.join(rows)


def compare_table():
    rows = []
    for name, label in (('compare_10k.json', '1 万'), ('compare_1m.json', '100 万'),
                        ('compare_10m.json', '1,000 万')):
        if not os.path.exists(os.path.join(RESULTS, name)):
            continue
        r = load(name)
        rows.append('| %s | %s | %s | %s | %s | %s | %.0f |' % (
            label, r['events'], r['trades'], r['volume'],
            '无' if r['first_divergence'] is None else r['first_divergence']['seq'],
            r['full_checks'], r['seconds']))
    head = ('| 规模 | 事件 | 成交笔数 | 成交量 (lot) | 首次分歧 | 全扫描次数 | 秒 |\n'
            '|---|---|---|---|---|---|---|')
    return head + '\n' + '\n'.join(rows)


def queue_table():
    r = load('queue_model_summary.json')
    rows = []
    for h, v in sorted(r['per_horizon'].items(), key=lambda kv: int(kv[0])):
        reg = ' / '.join('%s %.4f (n=%d)' % (k, x['brier'], x['n'])
                         for k, x in v['by_regime'].items())
        rows.append('| %s | %d | %.3f | %.4f | %.4f | %.4f | %.4f | %s |' % (
            h, v['n'], v['base_rate'], v['brier'], v['brier_constant'],
            v['brier_parametric'], v['brier_train'], reg))
    head = ('| T (s) | 测试样本 | 成交率 | Brier（分桶） | Brier（常数） | Brier（参数） '
            '| Brier（训练） | 按 regime |\n|---|---|---|---|---|---|---|---|')
    return head + '\n' + '\n'.join(rows)


MUTATION_FILES = (('历史（原 VM）', 'mutations_book.json', 'mutations_queue_model.json'),
                  ('本机复测（审核：判定集 + 新判据，queue_model 源码已改）',
                   'mutations_book_audit.json', 'mutations_queue_model_audit.json'))


def mutation_table():
    rows = []
    for label, *names in MUTATION_FILES:
        for name in names:
            if not os.path.exists(os.path.join(RESULTS, name)):
                continue
            r = load(name)
            rows.append('| %s | %s | %d | %d | %d | %d | %d | %.1f%% |' % (
                label, r['file'], r['candidates'], r['run'], r['killed'], r['survived'], r['timeouts'],
                r['kill_rate']))
    head = ('| 运行 | 文件 | 候选位点 | 抽样运行（= 抓住率分母） | 抓住 | 漏网 | 超时（计入抓住） '
            '| 抓住率 = 抓住 / 抽样 |\n|---|---|---|---|---|---|---|---|')
    return head + '\n' + '\n'.join(rows)


def mutation_mapping_table():
    """module -> candidate sites -> sampled (the denominator) -> run 1 / re-run / final.
    Survivors of run 1 were re-run after adding tests (killed mutants kept);
    `rerun.survivor_ids` is the survivor set after the first re-run."""
    rows = []
    for name in ('book', 'queue_model'):
        fin, r1 = load('mutations_%s.json' % name), load('mutations_%s_run1.json' % name)
        rr = fin.get('rerun', {}).get('survivor_ids', [])
        rows.append('| %s | %d | %d | %d / %d / %d | %d | %d / %d / %d | %d/%d = %.1f%% |' % (
            fin['file'], fin['candidates'], fin['run'], r1['killed'], r1['survived'], r1['timeouts'],
            len(rr), fin['killed'], fin['survived'], fin['timeouts'], fin['killed'], fin['run'],
            fin['kill_rate']))
    head = ('| 模块 | 候选位点 | 抽样运行（分母） | 第一轮 抓住 / 漏网 / 超时 | 第一次复跑后漏网 '
            '| 最终 抓住 / 漏网 / 超时 | 最终抓住率 |\n|---|---|---|---|---|---|---|')
    return head + '\n' + '\n'.join(rows)


def mutation_operator_table():
    """Sampled mutants per operator and module: run / killed in run 1 -> killed finally."""
    data = [(n, load('mutations_%s.json' % n), load('mutations_%s_run1.json' % n))
            for n in ('book', 'queue_model')]
    ops = sorted({op for _n, fin, _r in data for op in fin['by_operator']})
    rows = []
    for op in ops:
        cells = []
        for _n, fin, r1 in data:
            f = fin['by_operator'].get(op, {'run': 0, 'killed': 0})
            a = r1['by_operator'].get(op, {'run': 0, 'killed': 0})
            cells.append('%d / %d → %d' % (f['run'], a['killed'], f['killed']))
        rows.append('| %s | %s |' % (op, ' | '.join(cells)))
    rows.append('| **合计** | %s |' % ' | '.join(
        '**%d / %d → %d**' % (fin['run'], r1['killed'], fin['killed']) for _n, fin, r1 in data))
    head = ('| 算子 | ' + ' | '.join(fin['file'] for _n, fin, _r in data) + ' |\n|---|'
            + '---|' * len(data))
    return head + '\n' + '\n'.join(rows)


def survivor_table():
    """Surviving mutants with the hand-written triage from mutation_triage.json
    (class: equivalent | killed_by_new_test | survived)."""
    triage = {}
    tp = os.path.join(RESULTS, 'mutation_triage.json')
    if os.path.exists(tp):
        triage = load('mutation_triage.json')
    rows = []
    for label, *names in MUTATION_FILES:
        for name in names:
            if not os.path.exists(os.path.join(RESULTS, name)):
                continue
            r = load(name)
            for m in r['survivors']:
                key = '%s:%d' % (r['target'], m['id'])
                t = triage.get(key, {'class': '未分类', 'note': ''})
                rows.append('| %s | %s | %d | %s | %d | `%s` | %s | %s |' % (
                    label.split('（')[0], r['target'], m['id'], m['desc'], m['line'],
                    m['snippet'].replace('|', '\\|'), t['class'], t['note']))
    head = ('| 运行 | 文件 | id | 突变 | 行 | 代码 | 判定 | 说明 |\n|---|---|---|---|---|---|---|---|')
    return head + '\n' + '\n'.join(rows)


def fuzz_table():
    rows = []
    for path in sorted(glob.glob(os.path.join(RESULTS, 'fuzz_*.json'))):
        r = load(os.path.basename(path))
        rej = sum(v for k, v in r['status_counts'].items() if k.startswith('REJ'))
        rows.append('| %s | %d | %.2f | %d | %d | %d | %d | %d | %d |' % (
            os.path.basename(path), r['events'], r['bad_rate'], rej, len(r['crashes']),
            len(r['invariant_violations']), r['n_divergences'], r['full_checks'],
            r['trades_checked']))
    head = ('| 运行 | 事件 | 畸形比例 | 拒绝数 | 崩溃 | 不变量违背 | 与参考分歧 | 全扫描 | 校验成交 |\n'
            '|---|---|---|---|---|---|---|---|---|')
    return head + '\n' + '\n'.join(rows)


def coverage_table():
    """历史（原 VM，coverage.json）与本机复测（coverage_audit.json，采集器修复后）并列；后者缺省时只有历史列。"""
    r = load('coverage.json')
    a = load('coverage_audit.json') if os.path.exists(os.path.join(RESULTS, 'coverage_audit.json')) else None

    def cell(d, f):
        v = d['files'].get(f) if d else None
        return '%d / %d / %.1f%%' % (v['statements'], v['executed'], v['percent']) if v else '—'
    files = sorted(set(r['files']) | set(a['files'] if a else []))
    rows = ['| %s | %s | %s |' % (f, cell(r, f), cell(a, f)) for f in files]
    rows.append('| **核心合计** | **%.2f%%**（%d tests） | %s |' % (
        r['core_percent'], r['tests_run'],
        '**%.2f%%**（%d tests）' % (a['core_percent'], a['tests_run']) if a else '—'))
    rows.append('| 其余（tools / tests 之外）| %.2f%% | %s |' % (
        r['extras_percent'], '%.2f%%' % a['extras_percent'] if a else '—'))
    return ('| 文件（语句 / 执行 / 覆盖） | 历史（原 VM，`coverage.json`） | 本机复测（采集器修复后，`coverage_audit.json`） |'
            '\n|---|---|---|\n' + '\n'.join(rows))


def bench_table():
    r = load('bench_structures.json')
    names = ['SortedList', 'LazyHeap', 'SkipList', 'DictScan']
    rows = ['| %s | %s |' % (lv, ' | '.join('-' if row.get(n) is None else '%.0f' % row[n]
                                              for n in names))
            for lv, row in sorted(r['ns_per_op'].items(), key=lambda kv: int(kv[0]))]
    return ('| 档数 | ' + ' | '.join(names) + ' |\n|---|' + '---|' * len(names) + '\n'
            + '\n'.join(rows) + '\n\n(ns / 操作，' + r['mix'] + ')')


def main():
    for title, fn in (('压测', stress_table), ('对拍', compare_table), ('排队模型', queue_table),
                      ('突变', mutation_table), ('漏网', survivor_table),
                      ('模糊', fuzz_table), ('覆盖率', coverage_table),
                      ('数据结构', bench_table)):
        print('### %s\n' % title)
        try:
            print(fn())
        except (OSError, KeyError) as exc:
            print('(missing: %s)' % exc)
        print()


if __name__ == '__main__':
    main()
