"""Owner-private, immutable robot solver profiles. No client-selected file paths."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from custom_robots import CustomRobots

VERSION='custom-humanoid-profile-v1'
JOINTS={'Pelvis':0,'L_Hip':1,'R_Hip':2,'L_Knee':4,'R_Knee':5,'L_Ankle':7,'R_Ankle':8,
        'L_Foot':10,'R_Foot':11,'Neck':12,'L_Shoulder':16,'R_Shoulder':17,'L_Elbow':18,'R_Elbow':19,'L_Wrist':20,'R_Wrist':21}

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)

def configure(root,user,rid,value):
    if not isinstance(value,dict) or set(value)-{'mapping','neutral','locked_joints','collision_exclusions','dynamics'} or 'mapping' not in value:
        raise ValueError('配置需要 mapping，可选 neutral、locked_joints、collision_exclusions')
    robots=CustomRobots(root,user);folder=robots.path(rid)
    with robots._import_slot():
        record=json.loads((folder/'record.json').read_text())
        request=dict(schema=VERSION,robot=rid,source_sha256=record['sha256'],configuration=value,compiler_sha256=digest(Path(__file__).with_name('custom_robot_profile_compile.py')))
        version=hashlib.sha256(encoded(request).encode()).hexdigest()
        destination=folder/'profiles'/version
        if destination.exists():return read(root,user,rid,version)[0]
        destination.parent.mkdir(exist_ok=True)
        if len(list(destination.parent.iterdir()))>=30:raise ValueError('每个模型最多保存 30 个配置版本')
        with tempfile.TemporaryDirectory(prefix='.profile-',dir=folder) as tmp:
            tmp=Path(tmp);(tmp/'request.json').write_text(encoded(request))
            python=Path(__file__).resolve().parents[1]/'.retarget-venv/bin/python'
            result=subprocess.run([str(python) if python.is_file() else sys.executable,
                str(Path(__file__).with_name('custom_robot_profile_compile.py')),str(folder),str(tmp)],
                capture_output=True,timeout=60,env=dict(os.environ,OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1'))
            if result.returncode:
                message=(tmp/'error.txt').read_text() if (tmp/'error.txt').exists() else '配置编译失败'
                raise ValueError(message[:600])
            profile=json.loads((tmp/'profile.json').read_text());profile['version']=version
            (tmp/'profile.json').write_text(encoded(profile));shutil.rmtree(tmp/'work')
            os.rename(tmp,destination)
        record.update(retarget_ready=True,status='configured',latest_profile=version)
        temp=folder/'record.tmp';temp.write_text(encoded(record));os.replace(temp,folder/'record.json')
        return profile

def read(root,user,rid,version):
    if not isinstance(version,str) or not re.fullmatch('[0-9a-f]{64}',version):raise ValueError('必须指定明确的 robot_version')
    folder=CustomRobots(root,user).path(rid)/'profiles'/version
    if not (folder/'profile.json').is_file():raise ValueError('机器人配置版本不存在或不属于当前账号')
    profile=json.loads((folder/'profile.json').read_text())
    request={k:profile[k] for k in ('schema','robot','source_sha256','configuration')}
    if 'compiler_sha256' in profile:request['compiler_sha256']=profile['compiler_sha256']
    if hashlib.sha256(encoded(request).encode()).hexdigest()!=version or profile['version']!=version or profile['robot']!=rid:
        raise ValueError('机器人配置版本校验失败')
    if digest(folder/'model.mjb')!=profile['model_sha256']:raise ValueError('机器人模型版本校验失败')
    return profile,folder/'model.mjb'

def listing(root,user,rid):
    folder=CustomRobots(root,user).path(rid)/'profiles'
    return {'items':[read(root,user,rid,p.name)[0] for p in sorted(folder.iterdir()) if p.is_dir()]} if folder.exists() else {'items':[]}
