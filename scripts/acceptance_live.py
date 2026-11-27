#!/usr/bin/env python3
"""Opt-in, resumable LIVE acceptance. Creates ONE 2s motion and ONE retarget job.
Needs two distinct ordinary-user sessions and Gadget keys in environment:
MF_A_SESSION, MF_B_SESSION, MF_A_KEY, MF_B_KEY. Never stores credentials.
Use dedicated QA accounts. Records remain for audit; no delete/production controls.
Run once per poll (no long waits): python scripts/acceptance_live.py --base ...
  --state /safe/path/acceptance.json --allow-live-work
Re-run same command to resume. A completed report is API/GPU evidence; visual
browser playback and human quality still need separate inspection.
"""
import argparse,hashlib,json,os,secrets,struct,time,urllib.request,urllib.error
from pathlib import Path

class Client:
    def __init__(self,base,prefix):self.base=base.rstrip('/');self.session=os.environ[prefix+'_SESSION'];self.key=os.environ[prefix+'_KEY']
    def call(self,path,value=None,binary=False):
        headers={'Content-Type':'application/json','X-MotionFarm-Request':'1','Origin':os.environ.get('MF_ORIGIN',self.base)}
        headers.update({'Authorization':'Bearer '+self.key} if path.startswith('/api/gadget/') else {'Cookie':'mf_session='+self.session})
        request=urllib.request.Request(self.base+path,data=None if value is None else json.dumps(value).encode(),headers=headers)
        try:
            with urllib.request.urlopen(request,timeout=30) as response:return response.read() if binary else json.load(response)
        except urllib.error.HTTPError as exc:
            detail=exc.read(2048).decode('utf-8',errors='replace')
            exc.msg=f'{exc.reason}: {detail}'
            raise
    def forbidden(self,path,value=None):
        try:self.call(path,value)
        except urllib.error.HTTPError as exc:
            if exc.code==403:return
            raise
        raise AssertionError('cross-account access unexpectedly succeeded: '+path)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base',required=True);p.add_argument('--state',type=Path,required=True);p.add_argument('--allow-live-work',action='store_true');args=p.parse_args()
    if not args.allow_live_work:p.error('--allow-live-work required: creates real paid/compute work')
    a,b=Client(args.base,'MF_A'),Client(args.base,'MF_B')
    ua=a.call('/api/auth/me')['user'];ub=b.call('/api/auth/me')['user']
    assert ua['username']!=ub['username'] and ua['role']=='user' and ub['role']=='user','use two distinct ordinary QA users'
    if args.state.exists():s=json.loads(args.state.read_text());assert s['base']==args.base and s['users']==[ua['username'],ub['username']]
    else:
        uid=secrets.token_hex(8);s=dict(base=args.base,users=[ua['username'],ub['username']],run=uid,request='accept_'+uid,preset=secrets.token_hex(16),retarget=secrets.token_hex(16),checks=[],stage='generate',created=time.time())
    def save():
        args.state.parent.mkdir(parents=True,exist_ok=True);tmp=args.state.with_suffix('.tmp');tmp.write_text(json.dumps(s,ensure_ascii=False,indent=2));tmp.replace(args.state)
    def check(name):
        if name not in s['checks']:s['checks'].append(name)
        save()
    g='/api/gadget/v1/'
    save() # IDs persisted BEFORE first side effect; crash/retry reuses same jobs.
    body=dict(request_id=s['request'],description='A person walks slowly forward for two seconds with feet separated and hands clear of the torso.',n=1,duration=2,prompt_mode='exact_english',seed=731)
    a.call(g+'generate',body);a.call(g+'generate',body);check('generate_idempotent_retry')
    b.forbidden(g+'generate?id='+s['request']);b.forbidden(g+'cancel_generate',{'id':s['request']});check('generation_user_isolation')
    generated=a.call(g+'generate?id='+s['request']);s['generation_status']=generated['job']['status'];save()
    if generated['job']['status'] in ('failed','cancelled'):raise RuntimeError('generation failed; see state status and server job '+s['request'])
    if generated['job']['status']!='completed':print(json.dumps(dict(stage='waiting_generation',request=s['request'],status=s['generation_status'])));return
    assert generated['items'],'completed without motion';mid=generated['items'][0]['motion_id'];s['motion_id']=mid;check('real_generation_completed')
    resumed=Client(args.base,'MF_A').call(g+'generate?id='+s['request']);assert resumed['items'][0]['motion_id']==mid;check('reconnected_client_resumes_job')
    wire=a.call('/api/motions/'+mid+'/preview.bin',binary=True);assert wire[:8]==b'MFPRV001';n=struct.unpack('<I',wire[8:12])[0];meta=json.loads(wire[12:12+n]);assert meta['frames']>0 and len(wire)==12+n+meta['frames']*meta['joint_count']*12;check('binary_preview_valid')
    tag='accept-'+s['run'];a.call('/api/motions/'+mid+'/tags',dict(group='acceptance',tag=tag));found=a.call(g+'search?tag_group=acceptance&tag='+tag)['items'];assert mid in [x['motion_id'] for x in found];assert any(t['username']==ua['username'] for x in found for t in x['tags']);assert b.call(g+'search?tag_group=acceptance&tag='+tag)['items']==[];check('attributed_tags_search_isolation')
    try:preset=a.call('/api/personal/presets?id='+s['preset'])['preset']
    except urllib.error.HTTPError as exc:
        if exc.code!=403:raise
        preset=a.call('/api/personal/presets',dict(id=s['preset'],version=0,name='Acceptance '+s['run'],robot='unitree_g1_23dof',parameters={'smooth_weight':1},notes='Live acceptance; retained for audit'))
    b.forbidden('/api/personal/presets?id='+s['preset']);check('private_preset_isolation')
    job=dict(id=s['retarget'],motion_id=mid,robot=preset['robot'],preset_id=s['preset'],preset_version=1)
    queued=a.call(g+'retarget',job);assert json.loads(queued['execution'])['parameters']['smooth_weight']==1;a.call(g+'retarget',job);b.forbidden(g+'retarget?id='+s['retarget']);b.forbidden(g+'cancel',{'id':s['retarget']});check('retarget_snapshot_retry_isolation')
    result=a.call(g+'retarget?id='+s['retarget']);s['retarget_status']=result['status'];save()
    if result['status'] in ('failed','cancelled'):raise RuntimeError('retarget failed; inspect job '+s['retarget'])
    if result['status']!='done':print(json.dumps(dict(stage='waiting_retarget',job=s['retarget'],status=result['status'])));return
    payload=a.call(g+'download?id='+s['retarget'],binary=True);assert payload.startswith(b'PK');b.forbidden(g+'download?id='+s['retarget']);s['download_sha256']=hashlib.sha256(payload).hexdigest();check('real_retarget_download_isolation')
    cancel_id=s['request']+'_cancel';a.call(g+'generate',{**body,'request_id':cancel_id});a.call(g+'cancel_generate',{'id':cancel_id});cancelled=a.call(g+'generate?id='+cancel_id)['job'];assert cancelled['status'] in ('queued','preparing','running','cancelled');s['cancel_status']=cancelled['status'];check('cancel_requested')
    if cancelled['status']!='cancelled':save();print('Cancellation pending; rerun to verify terminal status');return
    check('cancel_terminal');s['stage']='complete';s['completed']=time.time();s['scope']='Real API/generation/retarget acceptance; browser playback and physical robot quality not certified';save();print(json.dumps(s,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
