"""Queue-position fill model.

Input  : our queue position at a price level (visible lots ahead), the level's
         total visible quantity, the level's distance from the touch (d = 0 at
         the best), trailing arrival / cancel / trade intensities (lots/s) on
         that side, and a horizon T.
Output : P(our order receives its first execution within T seconds) and a
         conditional time-to-fill estimate.

Calibration (chosen: binned empirical probabilities with Laplace smoothing
towards the global rate).  The bin key is (d clipped to 0..3, u-bin) where
u = ahead / (rate_trd * T + 1) is the queue position normalised by the lots
expected to trade against our side over the horizon.  A parametric
maximum-likelihood alternative p = 1 - exp(-exp(a_d) * (rate_trd*T + b_d) /
(ahead + 1)) is fitted for comparison.  Ground truth comes from
queue_collect.ProbeCollector (real orders of the replayed stream).
"""
import json
import math
import os

from lob import events as E
from lob.queue_collect import read_csv, outcome, DEFAULT_HORIZONS

U_EDGES = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
D_MAX = 3
ALPHA = 2.0


def u_bin(u):
    for i, edge in enumerate(U_EDGES):
        if u < edge:
            return i
    return len(U_EDGES)


def cell_key(d, ahead, rate_trd, horizon_s):
    u = ahead / (rate_trd * horizon_s + 1.0)
    return min(d, D_MAX) * (len(U_EDGES) + 1) + u_bin(u)


class QueueModel:
    """Binned empirical fill-probability model, one table per horizon."""

    def __init__(self):
        self.tables = {}        # horizon_s -> {cell: [n, k, median_fill_ns]}
        self.global_rate = {}   # horizon_s -> p
        self.n_cells = (D_MAX + 1) * (len(U_EDGES) + 1)

    def fit(self, rows, horizons=DEFAULT_HORIZONS):
        for h in horizons:
            h_ns = h * E.NS
            cells = {}
            n_all = k_all = 0
            for r in rows:
                y = outcome(r, h_ns)
                if y < 0:
                    continue
                c = cells.setdefault(cell_key(r['d'], r['ahead'], r['rate_trd'], h),
                                     [0, 0, []])
                c[0] += 1
                c[1] += y
                n_all += 1
                k_all += y
                if y:
                    c[2].append(r['first_fill_ns'])
            self.global_rate[h] = k_all / n_all if n_all else 0.0
            table = {}
            for key, (n, k, fills) in cells.items():
                fills.sort()
                med = fills[len(fills) // 2] if fills else None
                table[key] = [n, k, med]
            self.tables[h] = table
        return self

    def predict(self, ahead, level_total, rate_arr, rate_cxl, rate_trd, d, horizon_s):
        """P(first fill within horizon_s).  level_total / rate_arr / rate_cxl
        are accepted for the documented interface; the binned model found
        them uninformative beyond d and the normalised position (see REPORT)."""
        h = self._horizon(horizon_s)
        g = self.global_rate[h]
        cell = self.tables[h].get(cell_key(d, ahead, rate_trd, h))
        if cell is None:
            return g
        n, k, _ = cell
        return (k + ALPHA * g) / (n + ALPHA)

    def time_to_fill(self, ahead, rate_trd, d, horizon_s):
        """Median seconds-to-first-fill among filled probes of the cell, or None."""
        h = self._horizon(horizon_s)
        cell = self.tables[h].get(cell_key(d, ahead, rate_trd, h))
        if cell is None or cell[2] is None:
            return None
        return cell[2] / E.NS

    def _horizon(self, horizon_s):
        if horizon_s in self.tables:
            return horizon_s
        return min(self.tables, key=lambda h: abs(h - horizon_s))

    def predict_row(self, r, h):
        return self.predict(r['ahead'], r['level_total'], r['rate_arr'], r['rate_cxl'],
                            r['rate_trd'], r['d'], h)

    def to_dict(self):
        return {'u_edges': U_EDGES, 'd_max': D_MAX, 'alpha': ALPHA,
                'global_rate': {str(h): g for h, g in self.global_rate.items()},
                'tables': {str(h): {str(k): v for k, v in t.items()}
                           for h, t in self.tables.items()}}

    @classmethod
    def from_dict(cls, d):
        m = cls()
        m.global_rate = {int(h): g for h, g in d['global_rate'].items()}
        m.tables = {int(h): {int(k): v for k, v in t.items()} for h, t in d['tables'].items()}
        return m


class ParametricModel:
    """p = 1 - exp(-exp(a_d) * (rate_trd*T + b_d) / (ahead + 1)), MLE by grid."""

    def __init__(self):
        self.params = {}    # (h, d) -> (a, b)

    @staticmethod
    def _p(a, b, rate_trd, ahead, h):
        x = math.exp(a) * (rate_trd * h + b) / (ahead + 1.0)
        return min(1 - 1e-6, max(1e-6, 1.0 - math.exp(-x)))

    def fit(self, rows, horizons=DEFAULT_HORIZONS):
        for h in horizons:
            h_ns = h * E.NS
            for d in range(D_MAX + 1):
                data = [(r['rate_trd'], r['ahead'], outcome(r, h_ns)) for r in rows
                        if min(r['d'], D_MAX) == d]
                data = [t for t in data if t[2] >= 0]
                self.params[(h, d)] = self._grid(data, h) if data else (0.0, 1.0)
        return self

    def _grid(self, data, h):
        best, best_ll = (0.0, 1.0), -float('inf')
        for a10 in range(-60, 61, 5):
            for b in (0.0, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0):
                ll = self._loglik(a10 / 10.0, b, data, h)
                if ll > best_ll:
                    best, best_ll = (a10 / 10.0, b), ll
        a0, b0 = best
        for a100 in range(int(a0 * 100) - 50, int(a0 * 100) + 51, 5):
            ll = self._loglik(a100 / 100.0, b0, data, h)
            if ll > best_ll:
                best, best_ll = (a100 / 100.0, b0), ll
        return best

    def _loglik(self, a, b, data, h):
        ll = 0.0
        for rate_trd, ahead, y in data:
            p = self._p(a, b, rate_trd, ahead, h)
            ll += math.log(p) if y else math.log(1.0 - p)
        return ll

    def predict_row(self, r, h):
        a, b = self.params[(h, min(r['d'], D_MAX))]
        return self._p(a, b, r['rate_trd'], r['ahead'], h)


# ----------------------------------------------------------------- evaluation
def brier(pairs):
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def reliability(pairs, bins=10):
    out = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        sel = [(p, y) for p, y in pairs if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        if sel:
            out.append({'bin': i, 'lo': lo, 'hi': hi, 'n': len(sel),
                        'mean_pred': sum(p for p, _ in sel) / len(sel),
                        'mean_obs': sum(y for _, y in sel) / len(sel)})
    return out


def split_by_time(rows, train_frac=0.7):
    rows = sorted(rows, key=lambda r: (r['t0'], r['sym'], r['oid']))
    cut = int(len(rows) * train_frac)
    return rows[:cut], rows[cut:]


def evaluate(model, rows, h, regime_key='regime'):
    h_ns = h * E.NS
    pairs, by_regime, by_d = [], {}, {}
    for r in rows:
        y = outcome(r, h_ns)
        if y < 0:
            continue
        p = model.predict_row(r, h)
        pairs.append((p, y))
        by_regime.setdefault(r[regime_key], []).append((p, y))
        by_d.setdefault(min(r['d'], D_MAX), []).append((p, y))
    base = sum(y for _, y in pairs) / len(pairs) if pairs else 0.0
    return {'n': len(pairs), 'base_rate': round(base, 4),
            'brier': round(brier(pairs), 5) if pairs else None,
            'brier_constant': round(brier([(base, y) for _, y in pairs]), 5) if pairs else None,
            'by_regime': {k: {'n': len(v), 'brier': round(brier(v), 5),
                              'base_rate': round(sum(y for _, y in v) / len(v), 4)}
                          for k, v in sorted(by_regime.items())},
            'by_d': {str(k): {'n': len(v), 'brier': round(brier(v), 5)}
                     for k, v in sorted(by_d.items())},
            'reliability': reliability(pairs)}


def reliability_svg(rel, title, path):
    """Reliability diagram (predicted vs observed) as a stand-alone SVG."""
    W, H, M = 480, 480, 56
    scale = W - 2 * M

    def X(p):
        return M + p * scale

    def Y(p):
        return H - M - p * scale
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d" font-family="sans-serif" font-size="12">' % (W, H, W, H),
             '<rect width="100%%" height="100%%" fill="#ffffff"/>',
             '<text x="%d" y="24" font-size="14" fill="#222">%s</text>' % (M, title),
             '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#999" stroke-dasharray="4 4"/>'
             % (X(0), Y(0), X(1), Y(1)),
             '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#444"/>' % (X(0), Y(0), X(1), Y(0)),
             '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#444"/>' % (X(0), Y(0), X(0), Y(1))]
    for t in range(0, 11, 2):
        v = t / 10
        parts.append('<text x="%.0f" y="%d" text-anchor="middle" fill="#444">%.1f</text>'
                     % (X(v), H - M + 18, v))
        parts.append('<text x="%d" y="%.0f" text-anchor="end" fill="#444">%.1f</text>'
                     % (M - 6, Y(v) + 4, v))
    parts.append('<text x="%d" y="%d" text-anchor="middle" fill="#222">predicted probability'
                 '</text>' % (W // 2, H - 14))
    parts.append('<text transform="translate(16,%d) rotate(-90)" text-anchor="middle" '
                 'fill="#222">observed frequency</text>' % (H // 2))
    nmax = max((b['n'] for b in rel), default=1)
    pts = []
    for b in rel:
        r = 3 + 9 * (b['n'] / nmax) ** 0.5
        pts.append((X(b['mean_pred']), Y(b['mean_obs'])))
        parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="#1f77b4" fill-opacity="0.75">'
                     '<title>n=%d pred=%.3f obs=%.3f</title></circle>'
                     % (X(b['mean_pred']), Y(b['mean_obs']), r, b['n'], b['mean_pred'],
                        b['mean_obs']))
    if len(pts) > 1:
        parts.append('<polyline fill="none" stroke="#1f77b4" stroke-width="1.5" points="%s"/>'
                     % ' '.join('%.1f,%.1f' % p for p in pts))
    parts.append('</svg>')
    with open(path, 'w') as fh:
        fh.write('\n'.join(parts))
    return path


def run(probes_csv, out_dir, horizons=DEFAULT_HORIZONS, train_frac=0.7):
    """Fit on the first train_frac of time, evaluate on the rest; write JSON + SVG."""
    rows = read_csv(probes_csv)
    train, test = split_by_time(rows, train_frac)
    model = QueueModel().fit(train, horizons)
    param = ParametricModel().fit(train, horizons)
    os.makedirs(out_dir, exist_ok=True)
    res = {'probes': len(rows), 'train': len(train), 'test': len(test),
           'horizons_s': list(horizons), 'u_edges': U_EDGES, 'd_max': D_MAX,
           'alpha': ALPHA, 'per_horizon': {}}
    for h in horizons:
        ev = evaluate(model, test, h)
        ev_train = evaluate(model, train, h)
        ev_param = evaluate(param, test, h)
        h_ns = h * E.NS
        ev['censored_test'] = sum(1 for r in test if outcome(r, h_ns) < 0)
        ev['brier_train'] = ev_train['brier']
        ev['brier_parametric'] = ev_param['brier']
        ev['parametric_params'] = {str(d): param.params[(h, d)] for d in range(D_MAX + 1)}
        ev['svg'] = reliability_svg(ev['reliability'], 'Reliability, horizon %ds (test, n=%d, '
                                    'Brier %.4f)' % (h, ev['n'], ev['brier']),
                                    os.path.join(out_dir, 'reliability_h%d.svg' % h))
        res['per_horizon'][str(h)] = ev
    with open(os.path.join(out_dir, 'queue_model.json'), 'w') as fh:
        json.dump(model.to_dict(), fh, sort_keys=True)
    with open(os.path.join(out_dir, 'queue_model_eval.json'), 'w') as fh:
        json.dump(res, fh, indent=1, sort_keys=True)
    return res, model
