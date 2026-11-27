"""Optional per-farm cold disk, with explicit mount validation and no symlinks."""
import json
import os
from pathlib import Path


def cold_root(root):
    root = Path(root).resolve()
    config = root / 'storage_tiers.json'
    if not config.exists():
        return None
    value = json.loads(config.read_text())
    cold = Path(value['cold_root'])
    if not cold.is_absolute() or cold.is_symlink() or not cold.is_dir():
        raise RuntimeError('冷存储目录不可用；暂停归档')
    cold = cold.resolve()
    if cold == root or cold.is_relative_to(root) or root.is_relative_to(cold):
        raise RuntimeError('冷热目录必须独立，不能互相包含')
    mount = value.get('cold_mount')
    if mount and (not os.path.ismount(mount) or cold.stat().st_dev != Path(mount).stat().st_dev):
        raise RuntimeError('冷存储数据盘未正确挂载；暂停归档')
    if value.get('separate_devices', True) and cold.stat().st_dev == root.stat().st_dev:
        raise RuntimeError('冷热存储必须位于不同磁盘')
    return cold
