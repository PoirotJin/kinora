"""Account-private robot imports. Imported models are not solver registrations."""
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET

MAX_UPLOAD = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
ALLOWED = {'.xml', '.urdf', '.stl', '.obj', '.msh', '.png', '.jpg', '.jpeg', '.dae', '.mtl', '.txt', '.md', '.json'}

def safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('模型包中的文件路径无效')
    p = PurePosixPath(name)
    if p.is_absolute() or any(x in ('..', '.') for x in name.split('/')):
        raise ValueError('模型包不能引用包外路径')
    return p

def unpack(raw, name, folder):
    if not raw or len(raw) > MAX_UPLOAD:
        raise ValueError('模型文件不能超过 32 MB')
    if Path(name).suffix.lower() != '.zip':
        if Path(name).suffix.lower() not in ('.urdf', '.xml'):
            raise ValueError('请选择 URDF、MJCF XML 或包含资源文件的 ZIP')
        (folder / ('model' + Path(name).suffix.lower())).write_bytes(raw)
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if len(entries) > 2000 or sum(i.file_size for i in entries) > MAX_EXPANDED:
                    raise ValueError('模型包最多 2000 个文件，解压后不能超过 128 MB')
                seen = set()
                for info in entries:
                    p = safe_name(info.filename.rstrip('/'))
                    if '__MACOSX' in p.parts or p.name.startswith('._'): continue
                    if stat.S_ISLNK(info.external_attr >> 16): raise ValueError('模型包不能包含符号链接')
                    if info.is_dir(): continue
                    if info.flag_bits & 1: raise ValueError('不支持加密 ZIP')
                    key = str(p).casefold()
                    if key in seen: raise ValueError('模型包包含重复文件路径')
                    seen.add(key)
                    if p.suffix.lower() not in ALLOWED: raise ValueError('模型包包含不支持的文件类型：' + p.suffix)
                    target = folder / str(p)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
        except (zipfile.BadZipFile, RuntimeError) as e:
            raise ValueError('ZIP 文件损坏或无法读取') from e
    models = []
    for p in sorted(folder.rglob('*')):
        if p.suffix.lower() not in ('.xml', '.urdf'): continue
        root = parse_xml(p)
        if root.tag in ('robot', 'mujoco'): models.append(p.relative_to(folder).as_posix())
    if not models: raise ValueError('未找到 URDF robot 或 MJCF mujoco 模型入口')
    return models

def parse_xml(path):
    raw = path.read_bytes()
    try: raw.decode('utf-8-sig')
    except UnicodeDecodeError as e: raise ValueError('模型 XML 请使用 UTF-8 编码') from e
    if b'\x00' in raw: raise ValueError('模型 XML 请使用 UTF-8 编码')
    if len(raw) > 4 * 1024 * 1024 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('模型 XML 过大或包含不支持的实体声明')
    try: return ET.fromstring(raw)
    except ET.ParseError as e: raise ValueError('模型 XML 格式不正确') from e

def prepare_xml(folder, entry):
    """Resolve assets inside the package before any native parser sees a file."""
    folder = folder.resolve(); visiting = set(); done = set()
    def resolve(base, name):
        if name.startswith('package://'):
            parts = safe_name(name[10:]).parts
            candidates = [folder / Path(*parts), folder / Path(*parts[1:])]
            matches = [p for p in candidates if p.is_file()]
            if not matches:
                matches = [p for p in folder.rglob(parts[-1]) if p.as_posix().endswith('/' + '/'.join(parts[1:]))]
            if len(matches) != 1: raise ValueError('package 资源缺失或不唯一：' + name[:160])
            p = matches[0]
        else:
            # Relative ../ references are common in URDF packages, but must stay inside.
            if ':' in name or '\\' in name or Path(name).is_absolute(): raise ValueError('不支持包外资源：' + name[:160])
            p = base / name
        p = p.resolve()
        if not p.is_relative_to(folder) or not p.is_file(): raise ValueError('缺少资源文件或引用超出模型包：' + name[:160])
        return p
    def visit(path):
        if path in visiting: raise ValueError('模型 include 存在循环引用')
        if path in done: return
        visiting.add(path); root = parse_xml(path)
        if len(list(root.iter())) > 30000: raise ValueError('模型结构过于复杂')
        compiler = root.find('compiler')
        meshdir = compiler.get('meshdir', compiler.get('assetdir', '')) if compiler is not None else ''
        texturedir = compiler.get('texturedir', compiler.get('assetdir', '')) if compiler is not None else ''
        for el in root.iter():
            if el.tag in ('plugin', 'extension') or 'plugin' in el.attrib:
                raise ValueError('自定义模型不支持加载插件')
            if el.tag == 'compiler':
                for key in ('meshdir', 'texturedir', 'assetdir'): el.attrib.pop(key, None)
                el.set('strippath', 'false')
            for key in ('file', 'filename'):
                if key not in el.attrib: continue
                if el.tag not in ('include', 'mesh', 'texture', 'hfield', 'skin'): raise ValueError('不支持该模型资源引用')
                name = el.get(key)
                directory = texturedir if el.tag == 'texture' else meshdir if el.tag == 'mesh' else ''
                base = path.parent / directory if root.tag != 'robot' else path.parent
                resource = resolve(base, name)
                el.set(key, str(resource))
                if el.tag == 'include': visit(resource)
        if root.tag == 'robot':
            mj = root.find('mujoco')
            if mj is None: mj = ET.SubElement(root, 'mujoco')
            comp = mj.find('compiler')
            if comp is None: comp = ET.SubElement(mj, 'compiler')
            comp.set('discardvisual', 'false'); comp.set('fusestatic', 'false'); comp.set('strippath', 'false')
        ET.ElementTree(root).write(path, encoding='utf-8', xml_declaration=True)
        visiting.remove(path); done.add(path)
    visit(folder / str(safe_name(entry)))

class CustomRobots:
    def __init__(self, root, user):
        owner = user.get('username')
        if not isinstance(owner, str) or not owner: raise PermissionError('请先登录')
        self.folder = Path(root) / 'review/custom_robots' / hashlib.sha256(owner.encode()).hexdigest()
    def path(self, rid):
        if not isinstance(rid, str) or not re.fullmatch(r'robot_[0-9a-f]{32}', rid): raise ValueError('机器人编号无效')
        path = self.folder / rid
        if not path.is_dir(): raise ValueError('未找到该机器人的导入记录')
        return path
    def list(self):
        if not self.folder.exists(): return {'items': []}
        return {'items': sorted([json.loads(p.read_text()) for p in self.folder.glob('robot_*/record.json')], key=lambda x:x['created'], reverse=True)}
    @contextmanager
    def _import_slot(self):
        if self.folder.is_symlink() or self.folder.parent.is_symlink():
            raise ValueError('模型存储目录无效')
        self.folder.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.folder / '.import.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise ValueError('无法安全打开模型导入锁') from exc
        with os.fdopen(fd, 'r+b') as lock:
            info = os.fstat(lock.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('模型导入锁无效')
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('当前账号正在导入模型，请完成后重试') from exc
            yield

    def import_model(self, value):
        if set(value) - {'name', 'data', 'entry', 'label', 'request_id'}: raise ValueError('导入参数无效')
        name = value.get('name', '')
        if not isinstance(name, str) or len(name) > 240: raise ValueError('文件名无效')
        data = value.get('data', '')
        if not isinstance(data, str) or len(data) > (MAX_UPLOAD * 4 // 3 + 8): raise ValueError('模型文件不能超过 32 MB')
        try: raw = base64.b64decode(data, validate=True)
        except Exception as e: raise ValueError('模型文件编码无效') from e
        label = value.get('label') or Path(name).stem
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100: raise ValueError('名称应为 1–100 个字符')
        label = label.strip()
        entry = value.get('entry') or ''
        if not isinstance(entry, str): raise ValueError('请选择模型包中的有效入口')
        request_id = value.get('request_id')
        if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id)):
            raise ValueError('导入请求编号无效')
        digest = hashlib.sha256(raw).hexdigest()
        rid = 'robot_' + (hashlib.sha256((self.folder.name + request_id).encode()).hexdigest()[:32]
                          if request_id else uuid.uuid4().hex)
        destination = self.folder / rid
        fingerprint = hashlib.sha256(json.dumps([name, label, entry, digest]).encode()).hexdigest()
        with self._import_slot():
            if request_id and destination.exists():
                receipt = destination / 'import-request.sha256'
                if not receipt.is_file() or receipt.read_text() != fingerprint:
                    raise ValueError('导入请求编号已用于其他模型或参数，请重新选择文件')
                return {'robot': json.loads((destination / 'record.json').read_text())}
            if len(list(self.folder.glob('robot_*'))) >= 20: raise ValueError('每个账号最多导入 20 个模型')
            with tempfile.TemporaryDirectory(prefix='.import-', dir=self.folder) as tmp:
                tmp = Path(tmp); package = tmp / 'package'; package.mkdir()
                entries = unpack(raw, name, package)
                if not entry and len(entries) > 1: return {'choose_entry': entries}
                entry = entry or entries[0]
                if entry not in entries: raise ValueError('请选择模型包中的有效入口')
                # Compile a derivative only; the uploaded original is retained unchanged.
                (tmp / 'original.bin').write_bytes(raw)
                preview = tmp / 'preview.json'
                try:
                    result = subprocess.run([sys.executable, str(Path(__file__).with_name('custom_robot_compile.py')), str(package), entry, str(preview)], capture_output=True, timeout=45)
                except subprocess.TimeoutExpired as e: raise ValueError('模型解析超时，请简化模型或网格后重试') from e
                if result.returncode:
                    message = (tmp / 'error.txt').read_text() if (tmp / 'error.txt').exists() else '模型解析失败或超过资源限制'
                    raise ValueError(message[:700])
                compiled = json.loads(preview.read_text())
                record = dict(id=rid, label=label, filename=Path(name).name, entry=entry, created=time.time(), sha256=digest, status='imported', retarget_ready=False, **compiled['summary'])
                (tmp / 'record.json').write_text(json.dumps(record, ensure_ascii=False))
                if request_id: (tmp / 'import-request.sha256').write_text(fingerprint)
                os.rename(tmp, destination)
                return {'robot': record}
    def preview(self, rid):
        return json.loads((self.path(rid) / 'preview.json').read_text())
