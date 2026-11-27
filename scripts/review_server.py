"""MotionFarm LAN review server: stdlib HTTP, SQLite audit history, CPU preview."""
import argparse
from contextlib import closing
import datetime
import hashlib
import io
import ipaddress
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import ssl
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from g2h.farm_index import motion_info, read_motion, short_id, motion_number
from review_auth import Auth, TTL, AccountBlocked
from review_calibration import CalibrationConflict
from review_monitor import Monitor
from review_production import Production
from review_storage import Storage
from review_access import Forbidden,Removed,private_file,public_motion,owner_of,backfill_custom_owners

DOWNLOAD_PATHS = frozenset((
    '/api/storage/download', '/api/storage/archive', '/api/retarget/download',
    '/api/retarget/repair-download', '/api/gadget/v1/download', '/api/gadget/v1/source',
    '/api/harness/export', '/api/library/export',
))
DOWNLOAD_PATTERNS = (
    re.compile(r'/api/harness/personal-assets/snapshots/[0-9a-f]{32}/download'),
    re.compile(r'/api/harness/motions/[A-Za-z0-9_-]{1,96}/download'),
    re.compile(r'/api/harness/robot-edits/[a-f0-9]{32}/download'),
)


def download_path_allowed(path):
    return path in DOWNLOAD_PATHS or any(pattern.fullmatch(path) for pattern in DOWNLOAD_PATTERNS)


def resolve_preview_assets(project=PROJECT, preview_assets=None):
    if preview_assets is not None:
        return Path(preview_assets)
    primary = Path(project) / 'third_party/HY-Motion-1.0/scripts/gradio/static/assets/dump_wooden'
    if (primary / 'kintree.bin').is_file() and (primary / 'j_template.bin').is_file():
        return primary
    return Path(project) / 'data/human_rig'


def gadget_kit_bytes(web_root):
    zip_path = Path(web_root) / 'gadget-kit.zip'
    if zip_path.is_file():
        return zip_path.read_bytes()
    import tempfile
    import build_gadget
    with tempfile.TemporaryDirectory(prefix='gadget-kit-') as tmp:
        staged = Path(tmp) / 'gadget-kit.zip'
        build_gadget.build(staged)
        return staged.read_bytes()

ISSUES = ['direction','gait','timing','foot_slide','floor_penetration','jitter','unnatural_pose','action_mismatch','start_end_pose','support_contact','self_penetration','root_gait_mismatch','other']


class Conflict(Exception):
    pass


def validate_review(value):
    if not isinstance(value,dict) or set(value) != {'revision','status','semantic','quality','issues','corrected_text','notes','reviewer'}:
        raise ValueError('审核字段不完整')
    if type(value['revision']) is not int or value['revision']<0:
        raise ValueError('无效审核版本')
    for key,allowed in [('status',['accepted','rejected','uncertain']),('semantic',['match','partial','mismatch','unsure']),('quality',['good','issues','unsure'])]:
        if value[key] not in allowed:
            raise ValueError('无效审核选项')
    if not isinstance(value['issues'],list) or len(value['issues'])>len(ISSUES) or any(x not in ISSUES for x in value['issues']):
        raise ValueError('无效问题标签')
    for key,limit in [('corrected_text',2000),('notes',4000),('reviewer',80)]:
        if not isinstance(value[key],str) or len(value[key])>limit:
            raise ValueError('审核文本过长')
    if not value['reviewer'].strip():
        raise ValueError('请填写审核人')
    if value['corrected_text'].strip():
        import unicodedata
        if not any(c.isalpha() for c in value['corrected_text']) or any(c.isalpha() and 'LATIN' not in unicodedata.name(c,'') for c in value['corrected_text']):
            raise ValueError('修正动作描述请使用英文')
    if value['semantic']=='match' and any(x in value['issues'] for x in ['action_mismatch','start_end_pose']):
        raise ValueError('已标记动作或起止姿态不符，请调整文本匹配结果')
    if value['status']=='accepted' and (value['semantic']!='match' or value['quality']!='good'):
        raise ValueError('通过需要文本匹配且动作质量良好')
    if value['quality']=='good' and any(x in value['issues'] for x in ['foot_slide','floor_penetration','jitter','unnatural_pose','support_contact','self_penetration','root_gait_mismatch']):
        raise ValueError('已标记动作质量问题，请将动作质量设为存在问题')
    return value


class Store:
    def __init__(self,root,config,assets):
        self.root,self.config,self.assets=Path(root),Path(config),Path(assets)
        self.review_dir=self.root/'review'
        self.review_dir.mkdir(parents=True,exist_ok=True)
        from review_preview import PreviewCache
        self.preview_cache=PreviewCache(self.assets)
        self.path=private_file(self.review_dir/'reviews.sqlite3')
        with closing(self.reviews()) as db:
            db.executescript('''PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS latest(motion_id TEXT PRIMARY KEY,revision INTEGER,status TEXT,body TEXT);
              CREATE INDEX IF NOT EXISTS latest_status ON latest(status,motion_id);
              CREATE TABLE IF NOT EXISTS events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                motion_id TEXT,revision INTEGER,body TEXT,UNIQUE(motion_id,revision));
              CREATE INDEX IF NOT EXISTS events_motion ON events(motion_id,revision DESC);
            ''')
            from review_access import setup
            setup(db)
            from review_tags import setup as setup_tags
            setup_tags(db)
            from review_personal import setup as setup_personal
            setup_personal(db)
            from review_research import setup as setup_research
            setup_research(db)
            from review_collections import setup as setup_collections
            setup_collections(db)
            from personal_snapshot_jobs import setup as setup_snapshot_jobs
            setup_snapshot_jobs(db)
        try: backfill_custom_owners(self.root)
        except (OSError,sqlite3.Error): pass

    def reviews(self):
        db=sqlite3.connect(self.path,timeout=10)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def catalog(self):
        return sqlite3.connect((self.root/'prompts.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=10)

    def topics(self):
        from review_custom import visible_topics
        return visible_topics(self.config)

    def latest(self,motion_id):
        with closing(self.reviews()) as db:
            row=db.execute('SELECT body FROM latest WHERE motion_id=?',(motion_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def listing(self,topic='',status='',after=0,owner='',tag_query=None):
        from g2h import direction_index
        if after<0 or status not in direction_index.STATUSES:
            raise ValueError('无效筛选')
        if topic and topic not in {t['id'] for t in self.topics()}:
            raise ValueError('未知主题')
        if owner and (not isinstance(owner,str) or len(owner)>40):
            raise ValueError('无效用户筛选')
        with closing(self.catalog()) as db:
            db.execute('ATTACH DATABASE ? AS reviewdb',(str(self.path),))
            # Join by number via a deterministic SQL scalar function, preserving stable IDs.
            db.create_function('motion_id',1,short_id,deterministic=True)
            sql='''SELECT m.number,m.task_id,m.text_id,m.topic,m.seed,p.item,r.body,o.username,o.request_id FROM motion_links m
              JOIN prompts p ON p.id=m.text_id LEFT JOIN reviewdb.latest r ON r.motion_id=motion_id(m.number)
              LEFT JOIN reviewdb.motion_owners o ON o.motion_id=motion_id(m.number)
              WHERE m.number>? AND NOT EXISTS(SELECT 1 FROM reviewdb.removed d WHERE d.motion_id=motion_id(m.number))'''
            direction_index.attach(db,self.root)
            direction_sql,direction_args=direction_index.clause(status)
            sql+=direction_sql
            params=[after]+direction_args
            from review_tags import filter_sql
            tag_sql,tag_args=filter_sql(tag_query or {})
            sql+=tag_sql;params+=tag_args
            if topic:
                sql+=' AND m.topic=?'; params.append(topic)
            if owner:
                sql+=' AND o.username=?'; params.append(owner)
            if status=='pending': sql+=' AND r.motion_id IS NULL'
            elif status and status not in direction_index.FILTERS: sql+=' AND r.status=?'; params.append(status)
            rows=db.execute(sql+' ORDER BY m.number LIMIT 51',params).fetchall()
            result=[]
            for row in rows[:50]:
                info=motion_info(self.root,row[:5])
                info.pop('location',None)
                info.update(text=json.loads(row[5])['text'],review=json.loads(row[6]) if row[6] else None,
                            owner=row[7],request_id=row[8])
                result.append(info)
        from review_tags import attach
        attach(self,result)
        directions=direction_index.statuses(self.root,[r['motion_id'] for r in result])
        for item in result:item['direction_status']=directions.get(item['motion_id'],'not_checked')
        return dict(items=result,next_after=rows[49][0] if len(rows)>50 else None,direction_progress=direction_index.summary(self.root))

    def detail(self,motion_id):
        from review_access import check
        check(self.root,motion_id)
        number=motion_number(motion_id)
        with closing(self.catalog()) as db:
            row=db.execute('SELECT number,task_id,text_id,topic,seed FROM motion_links WHERE number=?',(number,)).fetchone()
            if not row: raise ValueError('动作不存在')
            info=motion_info(self.root,row)
            info['prompt']=json.loads(db.execute('SELECT item FROM prompts WHERE id=?',(row[2],)).fetchone()[0])
        info['review']=self.latest(motion_id)
        info.update(owner_of(self.root,motion_id))
        from g2h.agent_learning import screening
        info['screening']=screening(self.root,motion_id)
        from g2h.auto_review import detail as machine_detail
        info['machine_review']=machine_detail(self.root,motion_id)
        with closing(self.reviews()) as db:
            info['history']=[json.loads(r[0]) for r in db.execute('SELECT body FROM events WHERE motion_id=? ORDER BY revision DESC LIMIT 20',(motion_id,))]
        return info

    def preview(self,motion_id,mode='json'):
        from review_access import check
        check(self.root,motion_id)
        from g2h.farm_index import lookup_motion
        with closing(self.catalog()) as db: info=lookup_motion(db,self.root,motion_id)
        if not info['location']:raise ValueError('动作尚未生成')
        key=(motion_id,info['location']['sha256'])
        def load():
            with closing(self.catalog()) as db:record=read_motion(db,self.root,motion_id)
            from g2h.agent_learning import index_record
            try:
                index_record(self.root,record)
            except (ValueError,KeyError,OSError,sqlite3.Error):pass
            return record
        meta,world,wire=self.preview_cache.get(key,motion_id,load)
        try:
            from g2h.direction_index import save
            save(self.root,motion_id,info['location']['sha256'],meta['quality']['direction_semantics'])
        except (OSError,ValueError,KeyError,sqlite3.Error):pass
        # Warm previews also reconcile derived statistics against the current review.
        try:
            from g2h.agent_learning import reviewed
            reviewed(self.root,{'motion_id':motion_id})
        except (ValueError,KeyError,OSError,sqlite3.Error):pass
        if mode=='binary':return wire
        if mode=='thumbnail':
            result={**meta,'joints':world[len(world)//2].round(5).tolist()}
            result.pop('quality',None)
            return result
        return {**meta,'joints':world.round(5).tolist()}

    def save(self,motion_id,value):
        validate_review(value)
        detail=self.detail(motion_id)
        if not detail['location']: raise ValueError('仅可审核已生成动作')
        with closing(self.reviews()) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                current=db.execute('SELECT revision FROM latest WHERE motion_id=?',(motion_id,)).fetchone()
                revision=current[0] if current else 0
                if revision!=value['revision']: raise Conflict('其他窗口已修改审核，请刷新后重试')
                body={**value,'revision':revision+1,'motion_id':motion_id,'text_id':detail['text_id'],
                      'original_text':detail['prompt']['text'],'motion_sha256':detail['location']['sha256'],
                      'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      'schema_version':1,'issues':sorted(set(value['issues']))}
                raw=json.dumps(body,ensure_ascii=False)
                db.execute('INSERT INTO events(motion_id,revision,body) VALUES(?,?,?)',(motion_id,revision+1,raw))
                db.execute('INSERT OR REPLACE INTO latest VALUES(?,?,?,?)',(motion_id,revision+1,body['status'],raw))
                db.commit()
            except BaseException:
                db.rollback(); raise
        try:
            from g2h.agent_learning import reviewed
            reviewed(self.root,body)
        except (OSError,sqlite3.Error):
            print('Review saved; screening summary needs reconciliation',flush=True)
        return body


class DeadlineReader(io.RawIOBase):
    """Keep slow input bounded even when each individual recv makes progress."""
    def __init__(self, stream, connection, idle_timeout):
        super().__init__()
        self.stream, self.connection = stream, connection
        self.idle_timeout = idle_timeout
        self.deadline = None

    def readable(self):
        return True

    def set_deadline(self, seconds):
        self.deadline = None if seconds is None else time.monotonic() + seconds
        self.connection.settimeout(self.idle_timeout)

    def readinto(self, buffer):
        timeout = self.idle_timeout
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Request read deadline exceeded')
            timeout = min(timeout, remaining)
        self.connection.settimeout(timeout)
        return self.stream.readinto(buffer)

    def close(self):
        try:
            self.stream.close()
        finally:
            super().close()


class Handler(BaseHTTPRequestHandler):
    server_version='kinora'
    sys_version=''
    read_timeout = 15
    header_timeout = 15
    body_min_rate = 256 * 1024
    download_timeout = 3600

    def setup(self):
        super().setup()
        self.rfile.close()
        self._request_reader = DeadlineReader(self.connection.makefile('rb', buffering=0), self.connection, self.read_timeout)
        self.rfile = io.BufferedReader(self._request_reader)

    def handle_one_request(self):
        self._request_reader.set_deadline(self.header_timeout)
        try:
            super().handle_one_request()
        finally:
            self._request_reader.set_deadline(None)

    def parse_request(self):
        try:
            return super().parse_request()
        finally:
            # Only inbound reads have a total deadline; response streams keep
            # the existing socket inactivity timeout, irrespective of length.
            self._request_reader.set_deadline(None)

    def peer(self):
        address=self.client_address[0]
        if getattr(self.server,'behind_proxy',False) and ipaddress.ip_address(address).is_loopback:
            try:return str(ipaddress.ip_address(self.headers.get('X-Real-IP','')))
            except ValueError:pass
        return address

    def log_message(self,fmt,*args):
        pass # Do not log access tokens or review text.

    def send(self,status,data,content_type='application/json; charset=utf-8',cookie=None,attachment=None,content_sha256=None):
        raw=json.dumps(data,ensure_ascii=False,allow_nan=False).encode() if not isinstance(data,bytes) else data
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(raw)))
        if content_sha256 is not None:
            self.send_header('X-MotionFarm-Content-SHA256',content_sha256)
        self.send_header('Cache-Control','no-store')
        if attachment is not None:
            self.send_header('Content-Disposition','attachment; filename="'+attachment+'"')
        if cookie is not None:
            self.send_header('Set-Cookie',cookie)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        if getattr(self.server,'secure',False):
            self.send_header('Strict-Transport-Security','max-age=31536000; includeSubDomains')
        try:
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            # A write may have already sent part of the status, headers or body.
            self.close_connection = True

    def body(self,limit=16000):
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=limit: raise ValueError('请求大小不正确')
        # Allow large robot uploads proportionally more time, while small JSON
        # bodies cannot hold a worker indefinitely by sending occasional bytes.
        self._request_reader.set_deadline(max(self.read_timeout, size / self.body_min_rate))
        try:
            value=json.loads(self.rfile.read(size))
        finally:
            self._request_reader.set_deadline(None)
        if not isinstance(value,dict): raise ValueError('请求格式不正确')
        return value

    def download(self,path):
        with path.open('rb') as stream:
            return self.download_stream(stream,path.name)

    def download_stream(self,stream,name):
        size=os.fstat(stream.fileno()).st_size
        start,end=0,size-1
        requested=self.headers.get('Range')
        if requested:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
            if not match or not any(match.groups()):return self.send(416,{'error':'不支持的文件范围'})
            left,right=match.groups()
            if left:start=int(left);end=min(int(right),end) if right else end
            else:start=max(0,size-int(right))
            if start>end or start>=size:return self.send(416,{'error':'文件范围超出大小'})
        stream.seek(start)
        remaining = max(0, end - start + 1)
        self.send_response(206 if requested else 200)
        self.send_header('Content-Type','application/zip')
        self.send_header('Content-Length',str(remaining))
        self.send_header('Accept-Ranges','bytes')
        if requested:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.send_header('Content-Disposition','attachment; filename="'+name+'"')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        deadline = time.monotonic() + self.download_timeout
        try:
            self.end_headers()
            while remaining:
                chunk = stream.read(min(256 * 1024, remaining))
                seconds_left = deadline - time.monotonic()
                if not chunk or seconds_left <= 0:
                    break
                self.connection.settimeout(min(self.read_timeout, seconds_left))
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except OSError:
            # Headers may already be on the wire. Closing yields a short body that
            # the client can detect without injecting an error into the archive.
            self.close_connection = True
        if remaining:
            self.close_connection = True

    def cookie(self,token,clear=False):
        flags='mf_session='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+('0' if clear else str(TTL))
        if getattr(self.server,'secure',False): flags+='; Secure'
        return flags

    def origin_allowed(self, required=False):
        origin=self.headers.get('Origin')
        allowed=getattr(self.server,'origins',set())
        if origin:
            return origin.rstrip('/') in allowed
        return not required

    def dispatch(self,write=False):
        try:
            parsed=urlsplit(self.path)
            path=parsed.path
            if getattr(self.server,'download_only',False) and (
                write or not download_path_allowed(path)
            ):
                return self.send(404,{'error':'Not found'})
            from review_account_routes import handle_public
            if handle_public(self, path, write): return
            if not path.startswith('/api/'):
                files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
                files['/admin']=('index.html','text/html; charset=utf-8')
                files['/admin/']=('index.html','text/html; charset=utf-8')
                files['/admin-ops.js']=('admin-ops.js','text/javascript; charset=utf-8')
                files['/admin-ops.css']=('admin-ops.css','text/css; charset=utf-8')
                files['/health.js']=('health.js','text/javascript; charset=utf-8')
                files['/production.js']=('production.js','text/javascript; charset=utf-8')
                files['/custom.js']=('custom.js','text/javascript; charset=utf-8')
                files['/admin.js']=('admin.js','text/javascript; charset=utf-8')
                files['/storage.js']=('storage.js','text/javascript; charset=utf-8')
                files['/retarget.js']=('retarget.js','text/javascript; charset=utf-8')
                files['/three.min.js']=('three.min.js','text/javascript; charset=utf-8')
                files['/robot-viewer.js']=('robot-viewer.js','text/javascript; charset=utf-8')
                files['/preview-worker.js']=('preview-worker.js','text/javascript; charset=utf-8')
                files['/acceptance.js']=('acceptance.js','text/javascript; charset=utf-8')
                files['/personal.js']=('personal.js','text/javascript; charset=utf-8')
                files['/harness.js']=('harness.js','text/javascript; charset=utf-8')
                files['/tags.js']=('tags.js','text/javascript; charset=utf-8')
                files['/research.js']=('research.js','text/javascript; charset=utf-8')
                files['/operations.js']=('operations.js','text/javascript; charset=utf-8')
                files['/calibration.js']=('calibration.js','text/javascript; charset=utf-8')
                files['/costs.js']=('costs.js','text/javascript; charset=utf-8')
                files['/gadget.js']=('gadget.js','text/javascript; charset=utf-8')
                files['/gadget/agent-kit.zip']=('gadget-kit.zip','application/zip')
                if write or path not in files: return self.send(404,{'error':'Not found'})
                name,mime=files[path]
                payload=gadget_kit_bytes(self.server.web) if path=='/gadget/agent-kit.zip' else (self.server.web/name).read_bytes()
                return self.send(200,payload,mime)
            if path.startswith('/api/gadget/v1/'):
                from review_gadget import handle_api
                return handle_api(self,parsed,write)
            # Browser writes must send an allow-listed Origin plus the custom JSON header.
            if not self.origin_allowed(required=write):
                return self.send(403,{'error':'Cross-origin request denied'})
            if write and (self.headers.get('X-MotionFarm-Request')!='1' or self.headers.get('Content-Type','').split(';')[0]!='application/json'):
                return self.send(403,{'error':'请求验证失败，请刷新页面'})
            cookies=SimpleCookie()
            try: cookies.load(self.headers.get('Cookie',''))
            except Exception: pass
            token=cookies['mf_session'].value if 'mf_session' in cookies else ''
            auth=self.server.auth
            if path in ('/api/auth/login','/api/admin/login') and write:
                value=self.body()
                result=auth.login(value.get('username'),value.get('password'),self.peer())
                if result is None: return self.send(401,{'error':'用户名或密码不正确'})
                session,user=result
                if path == '/api/admin/login' and user.get('role') != 'admin':
                    auth.logout(session,peer=self.peer())
                    auth.audit('admin.login_denied',user['username'],user['username'],self.peer(),'forbidden')
                    return self.send(403,{'error':'此后台仅允许管理员登录；普通用户请使用客户端'})
                return self.send(200,{'user':user},cookie=self.cookie(session))
            user=auth.session(token)
            if not user: return self.send(401,{'error':'请登录后继续'})
            if path=='/api/auth/me' and not write: return self.send(200,{'user':user})
            if path=='/api/auth/logout' and write:
                auth.logout(token,peer=self.peer())
                return self.send(200,{'ok':True},cookie=self.cookie('',True))
            if path=='/api/auth/password' and write:
                value=self.body()
                auth.throttle(self.peer(),user['username'])
                new_token=auth.change_password(user['username'],value.get('old_password'),value.get('new_password'),peer=self.peer())
                return self.send(200,{'user':auth.session(new_token)},cookie=self.cookie(new_token))
            if user['must_change']: return self.send(403,{'error':'请先修改临时密码','must_change':True})
            from review_access import allowed,remove,require_motion_access
            if not allowed(user,path,write):raise Forbidden('当前用户组没有此操作权限')
            if path.startswith('/api/harness/'):
                from review_harness import handle
                try:
                    return handle(self,parsed,write,user)
                except KeyError as exc:
                    if path.startswith('/api/harness/video-motion/'):
                        return self.send(404,{'error':exc.args[0] if exc.args else str(exc)})
                    raise
            motion_access_match=re.fullmatch(r'/api/motions/(m[0-9a-z]{8,})(?:/(?:preview(?:\.bin)?|thumbnail|review|review-history|tags|presence))?',path)
            if motion_access_match:require_motion_access(self.server.store,motion_access_match[1],user)
            if path in ('/api/research/assessment','/api/research/active-queue') and not write:
                import review_assessment as assessment
                query={k:v[0] for k,v in parse_qs(parsed.query).items()}
                return self.send(200,assessment.detail(self.server.store,user,query.get('id','')) if path.endswith('/assessment') else assessment.queue(self.server.store,user,query))
            if path.startswith('/api/operations') or path=='/api/research/calibration':
                import motion_operations as ops
                store=self.server.store
                if path=='/api/research/calibration':
                    if write:return self.send(200,ops.label(store,user,self.body()))
                    from review_calibration import history
                    query=parse_qs(parsed.query)
                    return self.send(200,history(store,user,{key:value[0] for key,value in query.items()}))
                ops.admin(user)
                if path=='/api/operations' and not write:return self.send(200,ops.overview(store.root,user=user,**{key:parse_qs(parsed.query).get(key,[None])[0] for key in ('batches_cursor','releases_cursor')}))
                if path=='/api/operations/batch' and write:return self.send(200,ops.control_batch(store.root,user,self.body()))
                if path=='/api/operations/incremental' and write:return self.send(200,ops.configure_incremental(store.root,user,self.body()))
                if path=='/api/operations/releases/cancel' and write:return self.send(200,ops.cancel_release(store.root,user,self.body()))
                if path=='/api/operations/releases' and write:return self.send(200,ops.request_release(store.root,user,self.body()))
                if path=='/api/operations/release' and not write:
                    rid=parse_qs(parsed.query).get('id',[''])[0]
                    if not re.fullmatch('[A-Za-z0-9-]{1,80}',rid):raise ValueError('无效版本')
                    with closing(ops.connect(store.root)) as db:
                        release=db.execute("SELECT status FROM releases WHERE id=?",(rid,)).fetchone()
                    if not release or release[0]!='complete':raise ValueError('版本尚未完成')
                    return self.download(store.root/'review/releases'/rid/'dataset.zip')
                raise ValueError('无效运维接口')
            review_history_match=re.fullmatch('/api/motions/(m[0-9a-z]{8,})/review-history',path)
            if review_history_match and not write:
                from review_calibration import review_history
                query=parse_qs(parsed.query)
                return self.send(200,review_history(self.server.store,review_history_match[1],after=query.get('after',[None])[0],user=user))
            if path in ('/api/gadget/keys','/api/gadget/keys/revoke'):
                from review_gadget import handle_keys
                return handle_keys(self,path,write,user)
            if path=='/api/research/preview' and not write:
                from gadget_library import detail
                mid=parse_qs(parsed.query).get('id',[''])[0];detail(self.server.store,mid,user)
                if mid.startswith('mimp'):
                    from review_research import imported_view
                    return self.send(200,imported_view(self.server.store,mid,user,'json'))
                return self.send(200,self.server.store.preview(mid))
            if path=='/api/recovery' and not write:
                from review_recovery import status
                return self.send(200,status(self.server.store.root))
            if path=='/api/library/find' and not write:
                from library_find import find
                return self.send(200,find(self.server.store,user,parse_qs(parsed.query)))
            if path=='/api/library/search' and not write:
                from gadget_library import search
                return self.send(200,search(self.server.store,user,parse_qs(parsed.query)))
            if path=='/api/library/saved-searches':
                from review_collections import saved_searches
                return self.send(200,saved_searches(self.server.store,user,parse_qs(parsed.query),self.body() if write else None))
            if path=='/api/library/export' and not write:
                from review_collections import export_collection
                return self.send(200,export_collection(self.server.store,user,parse_qs(parsed.query)),'application/zip',attachment='motionfarm-collection.zip')
            if path=='/api/library/collections':
                from review_collections import CollectionMembershipChanged, CollectionMissing, collections, change
                store = self.server.store
                try:
                    result = change(store,user,self.body()) if write else collections(store,user,parse_qs(parsed.query))
                except (CollectionMembershipChanged, CollectionMissing) as exc:
                    return self.send(409,{'error':str(exc)})
                return self.send(200,result)
            if path=='/api/library/aliases':
                from review_collections import aliases
                return self.send(200,aliases(self.server.store,user,self.body() if write else None,parse_qs(parsed.query)))
            if path in ('/api/research/report','/api/research/retarget') and not write:
                from review_research import report,retarget_report
                query=parse_qs(parsed.query);key=query.get('id',[''])[0]
                return self.send(200,report(self.server.store,key,user) if path.endswith('/report') else retarget_report(self.server.retarget,key,user))
            if path == '/api/admin/audit' and not write:
                from review_admin_ops import audit_events
                return self.send(200,audit_events(auth,user,parse_qs(parsed.query)))
            if path == '/api/account-applications' and not write:
                from review_account_applications import Applications
                return self.send(200, Applications(auth).listing())
            application = re.fullmatch(r'/api/account-applications/([A-Za-z0-9_.-]{1,40})/(approve|reject)', path)
            if application and write:
                from review_account_applications import Applications
                value = self.body()
                return self.send(200, Applications(auth).decide(application[1], application[2], value.get('note', ''), user['username'], self.peer()))
            if path=='/api/users':
                if not write:return self.send(200,{'users':auth.users()})
                value=self.body();password=secrets.token_urlsafe(20)
                try:auth.create(value.get('username',''),password,role='user',actor=user['username'],peer=self.peer())
                except sqlite3.IntegrityError:raise ValueError('用户名已存在')
                return self.send(201,{'username':value['username'],'role':'user','temporary_password':password,'must_change':True})
            account=re.fullmatch(r'/api/users/([A-Za-z0-9_.-]{1,40})/(disable|enable|reset|kick)',path)
            if account and write:
                name,op=account[1],account[2];self.body()
                audit=dict(actor=user['username'],peer=self.peer())
                if op=='disable': return self.send(200,auth.set_disabled(name,True,**audit))
                if op=='enable': return self.send(200,auth.set_disabled(name,False,**audit))
                if op=='kick': return self.send(200,auth.kick(name,**audit))
                password=auth.reset_password(name,**audit)
                return self.send(200,{'username':name,'temporary_password':password,'must_change':True,'disabled':False})
            if path=='/api/removed' and not write:
                from review_trash import listing
                return self.send(200,listing(self.server.store,user,parse_qs(parsed.query)))
            removal=re.fullmatch('/api/motions/(m[0-9a-z]{8,})/(remove|restore)',path)
            if removal and write:
                value=self.body()
                return self.send(200,remove(self.server.store,removal[1],user,value.get('reason',''),removal[2]=='restore'))
            if path=='/api/storage' and not write:
                return self.send(200,self.server.storage.status(int(parse_qs(parsed.query).get('after',['0'])[0])))
            if path=='/api/storage/retry' and write:
                return self.send(200,self.server.storage.retry(self.body()))
            if path in ('/api/storage/package','/api/storage/settings') and write:
                result=self.server.storage.launch(self.body()) if path.endswith('/package') else self.server.storage.configure(self.body())
                return self.send(202,result)
            if path=='/api/storage/download' and not write:
                return self.download(self.server.storage.download(parse_qs(parsed.query).get('id',[''])[0]))
            if path=='/api/storage/archive' and not write:
                query=parse_qs(parsed.query);topic=query.get('topic',[''])[0];number=int(query.get('shard',['-1'])[0])
                if topic not in {t['id'] for t in self.server.store.topics()} or number<0:raise ValueError('无效分包')
                from g2h.cold_storage import archive_path
                from g2h.storage_tiers import cold_root
                try:
                    cold_root(self.server.store.root)
                except RuntimeError as exc:
                    return self.send(503,{'error':str(exc)})
                archive=archive_path(self.server.store.root/'topics'/topic,number)
                if not archive.is_file():
                    return self.send(404,{'error':'归档分包不存在'})
                try:
                    return self.download(archive)
                except FileNotFoundError:
                    return self.send(404,{'error':'归档分包不存在'})
            if path=='/api/monitor' and not write:
                return self.send(200,self.server.monitor.get())
            if path in ('/api/monitor/24h','/api/monitor/24h/report.md') and not write:
                rolling=getattr(self.server.monitor,'rolling',None)
                if rolling is None or rolling.get().get('available') is not True:
                    return self.send(503,{'error':'最近 24 小时统计尚未就绪，请稍后重试'})
                if path.endswith('/report.md'):
                    return self.send(200,rolling.export_markdown().encode('utf-8'),
                        'text/markdown; charset=utf-8',attachment='kinora-latest-24h.md')
                return self.send(200,rolling.get())
            if path=='/api/pipeline' and not write:
                from pipeline_pool import status
                return self.send(200,status(self.server.store.root))
            if path=='/api/acceptance' and not write:
                if self.server.monitor.acceptance is None:
                    return self.send(503,{'error':'验收记录尚未就绪，请稍后重试'})
                return self.send(200,self.server.monitor.acceptance.get())
            if path=='/api/acceptance/report.md' and not write:
                if self.server.monitor.acceptance is None:
                    return self.send(503,{'error':'验收记录尚未就绪，请稍后重试'})
                report=self.server.monitor.acceptance.export_markdown()
                return self.send(200,report.encode('utf-8'),'text/markdown; charset=utf-8',attachment='kinora-24h-report.md')
            if path=='/api/agent' and not write:
                from g2h.agent_learning import overview,memory
                result=overview(self.server.store.root)
                from g2h.auto_review import overview as machine_overview
                result['machine_review']=machine_overview(self.server.store.root)
                from review_access import hidden
                removed=hidden(self.server.store.root,[r['motion_id'] for r in result['priority']])
                result['priority']=[r for r in result['priority'] if r['motion_id'] not in removed]
                mq=result['machine_review']['queue']
                mh=hidden(self.server.store.root,[r['motion_id'] for r in mq])
                result['machine_review']['queue']=[r for r in mq if r['motion_id'] not in mh]
                result['coverage']={t['id']:memory(self.server.store.root,t['id']) for t in self.server.store.topics()[:30]}
                return self.send(200,result)
            if path=='/api/production' and not write:
                result=self.server.production.status()
                from review_access import hidden
                removed=hidden(self.server.store.root,[r['motion_id'] for r in result.get('latest',[])])
                if 'latest' in result:result['latest']=[r for r in result['latest'] if r['motion_id'] not in removed]
                return self.send(200,result)
            if not write and path in ('/api/retarget/model','/api/retarget/mesh-data'):
                from retarget_robots import robot_spec, DEFAULT_ROBOT
                spec=robot_spec(parse_qs(parsed.query).get('robot',[DEFAULT_ROBOT])[0])
                name=spec['asset']+('.json' if path.endswith('/model') else '.bin')
                try:
                    data=(self.server.web/'robot'/name).read_bytes()
                except FileNotFoundError:
                    return self.send(503,{'error':f"{spec['label']} 的预览资源缺失，请联系管理员恢复。"})
                return self.send(200,data,'application/json' if name.endswith('.json') else 'application/octet-stream')
            if not write and path=='/api/retarget/mesh-preview':
                from review_retarget import MeshPreviewUnavailable
                try:
                    preview=self.server.retarget.mesh_preview(parse_qs(parsed.query).get('id',[''])[0],user)
                except MeshPreviewUnavailable as exc:
                    return self.send(503,{'error':str(exc)})
                return self.send(200,preview)
            if path in ('/api/retarget','/api/retarget/cancel','/api/retarget/preview','/api/retarget/download','/api/retarget/repair-download'):
                queue=self.server.retarget
                if path=='/api/retarget':
                    if write:
                        value=self.body();mid=value.get('motion_id')
                        asset_meta=require_motion_access(self.server.store,mid,user)
                        if isinstance(mid,str) and mid.startswith('ma_'):
                            class AssetStore:
                                root=self.server.store.root
                                @staticmethod
                                def detail(_identifier):
                                    return {'location':{'sha256':asset_meta['sha256']},'text':asset_meta.get('description',''),'parent_id':asset_meta.get('parent_id')}
                            return self.send(202,queue.submit(value,user,AssetStore()))
                        return self.send(202,queue.submit(value,user,self.server.store))
                    return self.send(200,queue.status(user,parse_qs(parsed.query)))
                if path=='/api/retarget/cancel' and write:
                    return self.send(202,queue.cancel(self.body().get('id',''),user))
                if path=='/api/retarget/repair-download' and not write:
                    key=parse_qs(parsed.query).get('id',[''])[0]
                    return self.send(200,queue.repair_download(key,user),'application/octet-stream',
                                     attachment='kinora-'+key+'-needs-repair.npz')
                if not write and path in ('/api/retarget/preview','/api/retarget/download'):
                    key=parse_qs(parsed.query).get('id',[''])[0]
                    if path.endswith('/preview'):
                        file=queue.result(key,user,'preview.json')
                        return self.send(200,json.loads(file.read_text()))
                    return self.download(queue.result(key,user,'result.npz',require_dynamic=True))
                return self.send(405,{'error':'Method not allowed'})
            if path=='/api/rewrite/models' and not write:
                from rewrite_models import public_models
                return self.send(200,public_models(self.server.store.root))
            if path in ('/api/custom','/api/custom/results','/api/custom/cancel'):
                from review_custom import Custom
                custom=Custom(self.server.store.root)
                if path=='/api/custom':
                    return self.send(202,custom.submit(self.body(limit=96*1024),user['username'])) if write else self.send(200,custom.status(user,parse_qs(parsed.query)))
                if path=='/api/custom/cancel' and write:
                    return self.send(202,custom.cancel(self.body().get('request_id',''),user))
                if path=='/api/custom/results' and not write:
                    query=parse_qs(parsed.query)
                    return self.send(200,custom.results(query.get('id',[''])[0],int(query.get('after',['0'])[0]),user))
                return self.send(405,{'error':'Method not allowed'})
            if write and path in ('/api/production/start','/api/production/stop','/api/production/schedule'):
                value=self.body()
                if path.endswith('/start'): result=self.server.production.launch(value,user['username'])
                elif path.endswith('/stop'): result=self.server.production.cancel()
                else: result=self.server.production.configure(value,user['username'])
                return self.send(202,result)
            store=self.server.store
            query=parse_qs(parsed.query)
            if path in ('/api/personal/motions','/api/personal/presets'):
                from review_personal import motions,listing,history,mutate
                if path.endswith('/motions'):
                    if write:return self.send(405,{'error':'Method not allowed'})
                    return self.send(200,motions(store,user,query))
                if write:return self.send(200,mutate(store,user,self.body(limit=24576)))
                return self.send(200,history(store,user,query) if 'id' in query else listing(store,user,query))
            if path=='/api/tags' and not write:
                from review_tags import vocabulary
                return self.send(200,vocabulary(store,query))
            presence_match=re.fullmatch('/api/motions/(m[0-9a-z]{8,})/presence',path)
            if presence_match:
                from review_collaboration import presence
                return self.send(200,presence(store,presence_match[1],user,self.body() if write else None))
            tag_match=re.fullmatch('/api/motions/(m[0-9a-z]{8,})/tags',path)
            if tag_match:
                from review_tags import change,read
                result=change(store,tag_match[1],user,self.body()) if write else read(store,tag_match[1],int(query.get('after',['0'])[0]))
                return self.send(200,result)
            if write:
                match=re.fullmatch('/api/motions/(m[0-9a-z]{8,})/review',path)
                if not match: return self.send(404,{'error':'Not found'})
                value=self.body()
                value['reviewer']=user['username']
                return self.send(200,store.save(match[1],value))
            if path=='/api/topics':
                from g2h.generation_backend import current_recipe, KIMODO_MODEL
                model = current_recipe()['model']
                return self.send(200,{'topics':store.topics(), 'generation':{
                    'model':model, 'duration_max_seconds':120 if model==KIMODO_MODEL else 12}})
            if path=='/api/motions':
                owner=query.get('owner',[''])[0]
                if owner=='me': owner=user['username']
                elif owner and user['role']!='admin' and owner!=user['username']:
                    raise Forbidden('只能筛选自己的作业')
                result=store.listing(query.get('topic',[''])[0],query.get('status',[''])[0],int(query.get('after',['0'])[0]),owner,query)
                admin=user['role']=='admin'
                result['items']=[public_motion(item,admin) for item in result['items']]
                return self.send(200,result)
            if path=='/api/export':
                after=int(query.get('after',['0'])[0])
                if after<0: raise ValueError('Invalid cursor')
                with closing(store.reviews()) as db:
                    rows=db.execute('SELECT event_id,body FROM events WHERE event_id>? ORDER BY event_id LIMIT 101',(after,)).fetchall()
                return self.send(200,{'events':[dict(event_id=r[0],**json.loads(r[1])) for r in rows[:100]],'next_after':rows[99][0] if len(rows)>100 else None})
            match=re.fullmatch(r'/api/motions/(m[0-9a-z]{8,})(/preview|/preview\.bin|/thumbnail)?',path)
            if match:
                if match[1].startswith('mimp'):
                    from review_research import imported_view
                    mode={'/preview':'json','/preview.bin':'binary','/thumbnail':'thumbnail',None:'detail'}[match[2]]
                    return self.send(200,imported_view(store,match[1],user,mode),'application/vnd.motionfarm.preview' if mode=='binary' else 'application/json; charset=utf-8')
                if match[2]=='/thumbnail':
                    return self.send(200,store.preview(match[1],'thumbnail'))
                if match[2]=='/preview.bin':return self.send(200,store.preview(match[1],'binary'),'application/vnd.motionfarm.preview')
                result=store.preview(match[1]) if match[2] else store.detail(match[1])
                if not match[2] and user['role']!='admin':
                    # Non-admins see which screening flags fired, not the raw metrics.
                    result['screening']={'flags':result['screening'].get('flags',[])} if result.get('screening') else None
                    result['prompt']={'text':result['prompt']['text']}
                    result['location']=bool(result.get('location'))
                    for key in ('task_id','text_id','seed','error'): result.pop(key,None)
                return self.send(200,result)
            self.send(404,{'error':'Not found'})
        except AccountBlocked as exc:self.send(403,{'error':str(exc)})
        except Forbidden as exc:self.send(403,{'error':str(exc)})
        except Removed as exc:self.send(404,{'error':str(exc)})
        except PermissionError as exc: self.send(429,{'error':str(exc)})
        except CalibrationConflict as exc:self.send(409,{'error':str(exc),'current':getattr(exc,'current',None)})
        except Conflict as exc: self.send(409,{'error':str(exc)})
        except (ValueError,KeyError,TypeError) as exc: self.send(400,{'error':str(exc)})
        except TimeoutError:
            self.close_connection=True
            self.send(408,{'error':'请求读取超时，请重试'})
        except (OSError,sqlite3.Error): self.send(503,{'error':'读取或保存失败，请重试；持续失败时检查服务器日志和磁盘。'})
        except Exception:
            self.send(500,{'error':'服务器内部错误'})

    def do_GET(self): self.dispatch()
    def do_POST(self): self.dispatch(True)


class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(8)
        self.secure=False
        self.origins=set()
        super().__init__(*args,**kwargs)
        host,port=self.server_address[0],self.server_address[1]
        self.origins={'http://%s:%s'%(host,port),'http://127.0.0.1:%s'%port}

    def download_listener(self,port):
        downloads = Server(('127.0.0.1',port),self.RequestHandlerClass)
        downloads.slots = threading.BoundedSemaphore(4)
        for name in ('store','web','auth','secure','origins','behind_proxy','storage','retarget'):
            setattr(downloads,name,getattr(self,name))
        downloads.download_only = True
        return downloads

    def serve_forever(self, poll_interval=.5):
        store = getattr(self, 'store', None)
        if getattr(self, 'download_only', False) or not isinstance(store, Store):
            return super().serve_forever(poll_interval)
        from personal_snapshot_jobs import resume_pending
        stop = threading.Event()
        worker = threading.Thread(target=resume_pending, args=(store, stop), name='snapshot-recovery', daemon=True)
        worker.start()
        try:
            return super().serve_forever(poll_interval)
        finally:
            stop.set()
            worker.join(timeout=1)

    def process_request(self,request,address):
        if not self.slots.acquire(False):
            request.close(); return
        request.settimeout(15)
        try: super().process_request(request,address)
        except BaseException: self.slots.release(); raise
    def process_request_thread(self,request,address):
        try:
            context=getattr(self,'tls_context',None)
            if context is not None:
                request.settimeout(5)
                request=context.wrap_socket(request,server_side=True)
                request.settimeout(15)
            super().process_request_thread(request,address)
        except (OSError,ssl.SSLError):request.close()
        finally:self.slots.release()


def default_origins(host, port, secure=False):
    """Default loopback aliases refer to the same locally bound service."""
    scheme = 'https' if secure else 'http'
    hosts = ['127.0.0.1', 'localhost'] if host in ('127.0.0.1', 'localhost') else [host]
    suffix = '' if port == (443 if secure else 80) else ':' + str(port)
    return [scheme + '://' + ('[' + h + ']' if ':' in h else h) + suffix for h in hosts]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--root',type=Path,default=PROJECT/'outputs/text_motion_farm')
    parser.add_argument('--tls-cert',type=Path)
    parser.add_argument('--tls-key',type=Path)
    parser.add_argument('--origins',default='',help='Comma-separated allowed browser origins')
    parser.add_argument('--behind-proxy',action='store_true',help='TLS terminates at a trusted loopback reverse proxy')
    parser.add_argument('--download-port',type=int,default=0,help='Separate loopback listener for authenticated downloads')
    parser.add_argument('--preview-assets',type=Path,help='Override skeleton preview assets directory')
    args=parser.parse_args()
    if args.behind_proxy and (not ipaddress.ip_address(args.host).is_loopback or not args.origins or any(not x.strip().startswith('https://') for x in args.origins.split(','))):
        parser.error('--behind-proxy requires a loopback host and explicit HTTPS origins')
    store=Store(args.root,PROJECT/'data/farm/topics.json',resolve_preview_assets(PROJECT,args.preview_assets))
    try:
        from library_find import warm
        warm(store)
    except Exception as error:
        print('Search index warmup deferred: '+type(error).__name__,flush=True)
    server=Server((args.host,args.port),Handler)
    server.store,server.web,server.auth=store,PROJECT/'web/review',Auth(store.review_dir)
    server.secure=bool(args.tls_cert) or args.behind_proxy
    server.behind_proxy=args.behind_proxy
    origins=[item.strip().rstrip('/') for item in args.origins.split(',') if item.strip()]
    if not origins:
        origins=default_origins(args.host,args.port,server.secure)
    server.origins=set(origins)
    if args.tls_cert:
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version=ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key or args.tls_cert)
        server.tls_context=context
        def reload_tls(signum, frame):
            try:
                context.load_cert_chain(args.tls_cert, args.tls_key or args.tls_cert)
                print('TLS certificate reloaded', flush=True)
            except Exception as error:
                print('TLS certificate reload failed: '+type(error).__name__, flush=True)
        signal.signal(signal.SIGHUP, reload_tls)
    server.monitor=Monitor(args.root,PROJECT/'data/farm/topics.json')
    server.monitor.start()
    server.production=Production(args.root,PROJECT/'data/farm/topics.json')
    server.production.start()
    server.storage=Storage(args.root,PROJECT/'data/farm/topics.json')
    server.storage.start()
    from review_retarget import Retarget
    server.retarget=Retarget(args.root)
    # Heavy job dispatch belongs to the independent pipeline worker service.
    server.retarget.stop=threading.Event()
    downloads=None
    if args.download_port:
        downloads=server.download_listener(args.download_port)
        threading.Thread(target=downloads.serve_forever,daemon=True).start()
    print('kinora Review listening on %s://%s:%s (account login)'%('https' if server.secure else 'http',args.host,args.port),flush=True)
    try: server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt: pass
    finally:
        if downloads:downloads.shutdown();downloads.server_close()
        server.monitor.stop.set()
        server.production.stop.set()
        server.storage.stop.set()
        server.retarget.stop.set()
        server.server_close()


if __name__=='__main__': main()
