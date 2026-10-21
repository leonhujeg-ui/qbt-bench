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
    rows = []
    for path in sorted(glob.glob(os.path.join(RESULTS, 'stress_r*.json'))):
        r = load(os.path.basename(path))
        rows.append('| %s | %s | %s | %s | %.2f | %.1f | %.1f | %.1f | %.0f |' % (
            r['label'], r['engine_only']['events_per_second'],
            r['engine_checked']['events_per_second'], r['e2e']['events_per_second'],
            r['snapshot']['after_every_event_ns'] / 1000.0, r['latency_ns']['p50'] / 1000.0,
            r['latency_ns']['p99'] / 1000.0, r['latency_ns']['p99_9'] / 1000.0,
            r['e2e']['ru_maxrss_mb']))
    head = ('| 轮次 | engine-only ev/s | engine+checker ev/s | 端到端 ev/s | Top-10 快照 µs '
            '| p50 µs | p99 µs | p999 µs | RSS 峰值 MB |\n|---|---|---|---|---|---|---|---|---|')
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


def mutation_table():
    rows = []
    for name in ('mutations_book.json', 'mutations_queue_model.json'):
        if not os.path.exists(os.path.join(RESULTS, name)):
            continue
        r = load(name)
        rows.append('| %s | %d | %d | %d | %d | %d | %.1f%% |' % (
            r['file'], r['candidates'], r['run'], r['killed'], r['survived'], r['timeouts'],
            r['kill_rate']))
    head = '| 文件 | 候选 | 运行 | 抓住 | 漏网 | 超时 | 抓住率 |\n|---|---|---|---|---|---|---|'
    return head + '\n' + '\n'.join(rows)


def survivor_table():
    """Surviving mutants with the hand-written triage from mutation_triage.json
    (class: equivalent | killed_by_new_test | survived)."""
    triage = {}
    tp = os.path.join(RESULTS, 'mutation_triage.json')
    if os.path.exists(tp):
        triage = load('mutation_triage.json')
    rows = []
    for name in ('mutations_book.json', 'mutations_queue_model.json'):
        if not os.path.exists(os.path.join(RESULTS, name)):
            continue
        r = load(name)
        for m in r['survivors']:
            key = '%s:%d' % (r['target'], m['id'])
            t = triage.get(key, {'class': '未分类', 'note': ''})
            rows.append('| %s | %d | %s | %d | `%s` | %s | %s |' % (
                r['target'], m['id'], m['desc'], m['line'], m['snippet'].replace('|', '\\|'),
                t['class'], t['note']))
    head = ('| 文件 | id | 突变 | 行 | 代码 | 判定 | 说明 |\n|---|---|---|---|---|---|---|')
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
    r = load('coverage.json')
    rows = ['| %s | %d | %d | %.1f%% |' % (f, v['statements'], v['executed'], v['percent'])
            for f, v in sorted(r['files'].items())]
    rows.append('| **核心合计** | | | **%.2f%%** |' % r['core_percent'])
    rows.append('| 其余（tools / tests 之外）| | | %.2f%% |' % r['extras_percent'])
    return '| 文件 | 语句 | 执行 | 覆盖 |\n|---|---|---|---|\n' + '\n'.join(rows)


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
