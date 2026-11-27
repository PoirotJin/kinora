"""Bounded temporary-file accounting for one inference batch."""
from contextlib import contextmanager
from pathlib import Path


def output_paths(jobs):
    paths = set()
    for job in jobs:
        for name in ('features', 'motion'):
            path = Path(job[name])
            paths.update((path, path.with_suffix('.tmp'), Path(str(path) + '.meta.json'),
                          Path(str(path) + '.meta.json.tmp')))
    return paths


def disk_bytes(paths):
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            pass
    return total


@contextmanager
def temporary_space(root, jobs):
    if not (Path(root) / 'prompts.sqlite3').exists():
        yield
        return
    from g2h.capacity import reservation
    paths = output_paths(jobs)
    before = disk_bytes(paths)
    allowance = (32 * len(jobs) + 16 * len({j['features'] for j in jobs})) * 1024**2
    with reservation(root, allowance, 'inference temporary arrays and text features') as budget:
        try:
            yield
        finally:
            budget.commit(disk_bytes(paths) - before)
