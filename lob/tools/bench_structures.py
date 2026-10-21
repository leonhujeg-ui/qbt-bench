"""Micro-benchmark of candidate price-level index structures.

Workload (seeded): a book side with ~N live levels; operations drawn from a
realistic mix: insert a new level near the touch, delete a random level,
delete the best level (level swept), read the best.  Each candidate exposes
insert(key) / remove(key) / best() / pop_best().  Keys are ints; "best" is
the largest key (bids; asks use negated prices in the real book).
"""
import heapq
import random
import time
from bisect import bisect_left, insort


class SortedList:
    """Current choice: sorted Python list, best at the end."""
    def __init__(self):
        self.a = []

    def insert(self, k):
        insort(self.a, k)

    def remove(self, k):
        del self.a[bisect_left(self.a, k)]

    def best(self):
        return self.a[-1] if self.a else None

    def pop_best(self):
        return self.a.pop()


class LazyHeap:
    """Max-heap with lazy deletion (deleted keys skipped at the top)."""
    def __init__(self):
        self.h = []
        self.alive = set()

    def insert(self, k):
        self.alive.add(k)
        heapq.heappush(self.h, -k)

    def remove(self, k):
        self.alive.discard(k)

    def best(self):
        h = self.h
        while h and -h[0] not in self.alive:
            heapq.heappop(h)
        return -h[0] if h else None

    def pop_best(self):
        k = self.best()
        self.alive.discard(k)
        heapq.heappop(self.h)
        return k


class SkipList:
    """Pure-Python skip list keyed by int; best = max = tail walk."""
    MAX_LEVEL = 12

    def __init__(self, seed=1):
        self.rng = random.Random(seed)
        self.head = [None] * self.MAX_LEVEL      # forward pointers
        self.level = 1
        self.max_key = None

    def _random_level(self):
        lvl = 1
        while lvl < self.MAX_LEVEL and self.rng.random() < 0.5:
            lvl += 1
        return lvl

    def insert(self, k):
        update = [None] * self.MAX_LEVEL
        node = None
        fwd = self.head
        for i in range(self.level - 1, -1, -1):
            while fwd[i] is not None and fwd[i][0] < k:
                node = fwd[i]
                fwd = node[1]
            update[i] = fwd
        lvl = self._random_level()
        if lvl > self.level:
            for i in range(self.level, lvl):
                update[i] = self.head
            self.level = lvl
        new = [k, [None] * lvl]
        for i in range(lvl):
            new[1][i] = update[i][i]
            update[i][i] = new
        if self.max_key is None or k > self.max_key:
            self.max_key = k

    def remove(self, k):
        update = [None] * self.MAX_LEVEL
        fwd = self.head
        for i in range(self.level - 1, -1, -1):
            while fwd[i] is not None and fwd[i][0] < k:
                fwd = fwd[i][1]
            update[i] = fwd
        node = fwd[0]
        for i in range(self.level):
            if update[i][i] is not node:
                break
            update[i][i] = node[1][i]
        if k == self.max_key:
            self.max_key = self._find_max()

    def _find_max(self):
        fwd = self.head
        last = None
        for i in range(self.level - 1, -1, -1):
            while fwd[i] is not None:
                last = fwd[i]
                fwd = last[1]
        return None if last is None else last[0]

    def best(self):
        return self.max_key

    def pop_best(self):
        k = self.max_key
        self.remove(k)
        return k


class DictScan:
    """Naive: dict of levels, best found by max() over keys (O(n))."""
    def __init__(self):
        self.d = {}

    def insert(self, k):
        self.d[k] = 1

    def remove(self, k):
        del self.d[k]

    def best(self):
        return max(self.d) if self.d else None

    def pop_best(self):
        k = max(self.d)
        del self.d[k]
        return k


def workload(n_ops, n_levels, seed):
    rng = random.Random(seed)
    live = sorted(rng.sample(range(100000, 100000 + 4 * n_levels), n_levels))
    ops = []
    for _ in range(n_ops):
        r = rng.random()
        if r < 0.40 and live:
            k = live.pop(rng.randrange(len(live)))
            ops.append(('remove', k))
        elif r < 0.50 and live:
            k = live.pop()
            ops.append(('pop_best', k))
        elif r < 0.90:
            top = live[-1] if live else 100000
            k = top - int(rng.expovariate(0.2))
            while k in live:
                k -= 1
            insort(live, k)
            ops.append(('insert', k))
        else:
            ops.append(('best', None))
    return ops


def bench(cls, ops, n_levels, seed=5):
    s = cls()
    rng = random.Random(seed)
    for k in rng.sample(range(100000, 100000 + 4 * n_levels), n_levels):
        s.insert(k)
    t0 = time.perf_counter()
    for op, k in ops:
        if op == 'insert':
            s.insert(k)
        elif op == 'remove':
            s.remove(k)
        elif op == 'pop_best':
            s.pop_best()
        else:
            s.best()
    return (time.perf_counter() - t0) / len(ops) * 1e9


def main(n_ops=300000, levels=(50, 200, 1000), seed=5):
    out = {}
    for n_levels in levels:
        ops = workload(n_ops, n_levels, seed)
        row = {}
        for cls in (SortedList, LazyHeap, SkipList, DictScan):
            if cls is DictScan and n_levels > 200:
                row[cls.__name__] = None       # O(n) best: skipped above 200 levels
                continue
            row[cls.__name__] = round(bench(cls, ops, n_levels, seed), 1)
        out[str(n_levels)] = row
    return {'ops': n_ops, 'ns_per_op': out, 'mix': 'insert 40% / remove 40% / pop_best 10% / best 10%'}


if __name__ == '__main__':
    import json
    print(json.dumps(main(), indent=1))
