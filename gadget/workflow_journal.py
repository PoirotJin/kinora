"""Bounded, private local recovery records; never stores credentials or prompts."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.parse

SOURCE_STATUSES = ('available','damaged','missing','unavailable','unverified')

RECOVERY_INSTRUCTION = ('Every MCP submission is journaled before sending. workflow_recovery defaults to OFFLINE local records; its cached identity is not current server authorization. After changing a Key use workflow_recovery(verify_identity=true) to bind this credential to its authenticated stable account. If an older v0.8 Key expired before binding, use workflow_recovery(discover=true,kind=generate|retarget|source) and follow next_after for that kind: this reads only existing owned server IDs and never submits work. Never merge other credential directories manually. Each record has an exact resume tool/arguments. Generation/retargeting use get_job with the SAME ID; imports use get_motion(server_motion_id=mimp+import_id), never the bare upload ID. Replay only original arguments and the SAME ID; a discovered record does not contain a replay recipe. Offline records and job completion do not certify ownership is still authorized or quality passed. Download requested artifacts and read quality_report separately. Poll at least 10 seconds apart. Each network call has at most 3 attempts with a 20-second socket timeout; interrupted submissions remain uncertain.')

def account_identity(value):
    if not isinstance(value,dict) or value.get('schema_version')!='gadget-account-v1':raise ValueError('Server does not provide a verified Gadget account identity; upgrade server/client together')
    if any(not isinstance(value.get(k),str) or not re.fullmatch('[a-f0-9]{32}',value[k]) for k in ('server_id','account_id')):raise ValueError('Invalid server account identity')
    if not isinstance(value.get('username'),str) or not re.fullmatch('[A-Za-z0-9_.-]{1,40}',value['username']):raise ValueError('Invalid account name')
    return {k:value[k] for k in ('schema_version','server_id','account_id','username')}

def read_json(path):
    if path.is_symlink():raise ValueError('Unsafe journal file')
    if not path.exists():return None
    if not path.is_file() or path.stat().st_size>65536:raise ValueError('Journal record exceeds limit')
    return json.loads(path.read_text())

def write_json(path,value):
    if path.is_symlink():raise ValueError('Unsafe journal file')
    raw=json.dumps(value,ensure_ascii=False).encode()
    if len(raw)>65536:raise ValueError('Journal record exceeds limit')
    fd,tmp=tempfile.mkstemp(prefix='.write-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def recovery_fields(record):
    """Derive old v0.7 import references without rewriting IDs or contacting server."""
    record=dict(record);kind=record['kind'];identity=record['id']
    if kind=='source':
        import_id=identity if re.fullmatch('[a-f0-9]{32}',identity) else identity[4:] if re.fullmatch('mimp[a-f0-9]{32}',identity) else None
        mid='mimp'+import_id if import_id else identity
        if record.get('server_motion_id',mid)!=mid:raise ValueError('Journal motion identity differs from its stable import ID')
        record['server_motion_id']=mid
        if import_id:record['import_id']=import_id
        record['resume']=dict(tool='get_motion',arguments=dict(motion_id=mid))
    else:record['resume']=dict(tool='get_job',arguments=dict(kind=kind,id=identity))
    return record

class Journal:
    def __init__(self, client):
        self.client=client;self.identity=None
        base=Path(os.environ.get('MOTIONFARM_OUTPUT_DIR','./motionfarm-output')).expanduser().resolve()
        self.credential_scope=hashlib.sha256((str(getattr(client,'url','test'))+'\0'+str(getattr(client,'key','test'))).encode()).hexdigest()[:24]
        self.parent=base/'.gadget-journal';self.directory(self.parent)
        self.bindings=self.parent/'bindings';self.directory(self.bindings)
        self.binding_path=self.bindings/(self.credential_scope+'.json')
        binding=read_json(self.binding_path)
        if binding:
            if binding.get('credential_scope')!=self.credential_scope:raise ValueError('Journal credential binding mismatch')
            self.identity=account_identity(binding['identity'])
        self.root=self.account_root(self.identity) if self.identity else self.parent/self.credential_scope
        self.directory(self.root)
    @staticmethod
    def directory(path):
        if path.is_symlink():raise ValueError('Unsafe journal directory')
        path.mkdir(parents=True,exist_ok=True,mode=0o700)
    def account_root(self,identity):
        scope=hashlib.sha256((identity['server_id']+'\0'+identity['account_id']).encode()).hexdigest()[:32]
        return self.parent/('account-'+scope)
    def legacy_scopes(self):
        if not self.identity:return []
        scopes=read_json(self.root/'.legacy-scopes') or []
        if not isinstance(scopes,list) or len(scopes)>256 or any(not isinstance(s,str) or not re.fullmatch('[a-f0-9]{24}',s) for s in scopes):raise ValueError('Invalid verified legacy journal links')
        return scopes
    def roots(self):
        roots=[self.root]+[self.parent/s for s in self.legacy_scopes()]
        for root in roots:
            if root.is_symlink():raise ValueError('Unsafe legacy journal namespace')
        return [root for root in roots if root.is_dir()]
    def verify_account(self):
        # No local namespace is changed until the authenticated response validates.
        identity=account_identity(self.client.request('identity'))
        if self.identity and identity!=self.identity:raise ValueError('Server account identity changed; cached recovery was preserved. Verify the server before using a new output directory.')
        root=self.account_root(identity);self.directory(root)
        old_root,old_identity=self.root,self.identity
        self.root,self.identity=root,identity
        try:
            with self.lock('account','legacy-association'):
                scopes=self.legacy_scopes()
                legacy=self.parent/self.credential_scope
                if legacy.is_symlink():raise ValueError('Unsafe legacy journal namespace')
                if legacy.is_dir() and self.credential_scope not in scopes:
                    if len(scopes)>=256:raise ValueError('Verified legacy directory limit reached; preserve existing logs and use server discovery')
                    scopes.append(self.credential_scope);write_json(root/'.legacy-scopes',scopes)
                write_json(self.binding_path,dict(credential_scope=self.credential_scope,identity=identity,verified_at=time.time()))
        except Exception:
            self.root,self.identity=old_root,old_identity
            raise
        return identity
    def ensure_account(self):
        if self.identity is None:self.verify_account()
        return self
    def context(self):
        return dict(identity=self.identity,identity_source='previously_verified_local_cache' if self.identity else 'unverified_credential_namespace',online_authorization_checked=False,verify_identity_required=self.identity is None)
    def path(self,kind,identity):
        key=hashlib.sha256((kind+'\0'+identity).encode()).hexdigest()
        return self.root/(key+'.json')
    def read(self,kind,identity):
        name=self.path(kind,identity).name;records=[]
        for root in self.roots():
            record=read_json(root/name)
            if record:
                if record.get('kind')!=kind or record.get('id')!=identity:raise ValueError('Journal record identity mismatch')
                records.append(record)
        if not records:return None
        digests={r['arguments_sha256'] for r in records if r.get('arguments_sha256')}
        if len(digests)>1:raise ValueError('Conflicting original arguments in verified journals; records preserved, inspect the original request')
        merged={}
        for record in sorted(records,key=lambda r:r.get('updated_at',0)):merged.update(record)
        return recovery_fields(merged)
    def write(self,record):
        record=recovery_fields(record)
        path=self.path(record['kind'],record['id'])
        write_json(path,record)
    @contextlib.contextmanager
    def lock(self,kind,identity):
        import stat
        path=self.path(kind,identity).with_suffix('.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);raise ValueError('Unsafe journal lock')
        try:
            if os.name=='nt':
                import msvcrt
                os.write(fd,b'0');os.lseek(fd,0,0);msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally:os.close(fd)
    def update(self,kind,identity,**values):
        with self.lock(kind,identity):
            record=self.read(kind,identity) or dict(kind=kind,id=identity,quality_state='unknown')
            record.update(values,updated_at=time.time());self.write(record)
            return record
    def submit(self,kind,identity,args,send):
        self.ensure_account()
        digest=hashlib.sha256(json.dumps(args,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        with self.lock(kind,identity):
            record=self.read(kind,identity) or dict(kind=kind,id=identity,quality_state='unknown')
            if record.get('arguments_sha256',digest)!=digest:raise ValueError('Stable ID already used with different arguments; recover original request')
            record.update(arguments_sha256=digest,submission='uncertain',updated_at=time.time())
            self.write(record)  # durable before any network request, including retries
            result=send()
            reference=recovery_fields(record)
            if kind=='source' and reference.get('import_id')==identity:
                if not isinstance(result,dict) or result.get('motion_id')!=reference['server_motion_id']:
                    raise ValueError('Import acknowledgement motion_id differs from stable import ID; submission remains uncertain. Query the recorded resume reference before retrying.')
                record.update(server_motion_id=result['motion_id'],resource_identity_verified=True,
                              source_status='available')
            record.update(submission='acknowledged',updated_at=time.time());self.write(record)
            return result
    def recover(self,kind=None,identity=None,limit=20,after=''):
        if kind and identity:return dict(record=self.read(kind,identity),context=self.context(),instructions=RECOVERY_INSTRUCTION)
        entries=set();scanned=0
        for root in self.roots():
            with os.scandir(root) as files:
                for item in files:
                    scanned+=1
                    if scanned>10000:raise ValueError('Journal listing limit reached; look up a specific kind and ID')
                    if re.fullmatch('[a-f0-9]{64}\\.json',item.name) and item.name>after and not item.is_symlink():entries.add(item.name)
        names=sorted(entries)[:limit];records=[]
        for name in names:
            first=next(read_json(root/name) for root in self.roots() if (root/name).exists())
            record=self.read(first['kind'],first['id'])
            if self.path(first['kind'],first['id']).name!=name:raise ValueError('Journal filename identity mismatch')
            records.append(record)
        return dict(records=records,next_after=names[-1] if len(entries)>limit else None,context=self.context(),instructions=RECOVERY_INSTRUCTION)
    def discover(self,kind,limit=20,after=''):
        identity=self.verify_account()
        result=self.client.request('recovery?'+urllib.parse.urlencode(dict(kind=kind,limit=limit,after=after)))
        if account_identity(result.get('identity'))!=identity:raise ValueError('Recovery identity differs from verified account')
        rows=result.get('records');cursor=result.get('next_after')
        if not isinstance(rows,list) or len(rows)>limit or (cursor is not None and (not isinstance(cursor,str) or len(cursor)>200)):raise ValueError('Invalid bounded server recovery response')
        # Validate the complete page before modifying any record.
        for row in rows:
            pattern='[A-Za-z0-9_-]{8,80}' if kind=='generate' else '[a-f0-9]{32}'
            if not isinstance(row,dict) or row.get('kind')!=kind or not isinstance(row.get('id'),str) or not re.fullmatch(pattern,row['id']) or not isinstance(row.get('server_status'),str) or len(row['server_status'])>80:raise ValueError('Invalid owned server recovery reference')
            if kind=='source' and row.get('source_status','unverified') not in SOURCE_STATUSES:
                raise ValueError('Invalid source availability in server recovery response')
        records=[]
        for row in rows:
            self.update(kind,row['id'],server_status=row['server_status'],server_observed=True,
                        **({'source_status':row.get('source_status','unverified')} if kind=='source' else {}))
            records.append(self.read(kind,row['id']))
        return dict(records=records,next_after=cursor,source='server_owner_index',context=dict(identity=identity,identity_source='authenticated_server',online_authorization_checked=True),instructions=RECOVERY_INSTRUCTION)
