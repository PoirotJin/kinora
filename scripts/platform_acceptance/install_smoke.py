"""Install published binaries on disposable native runners; never rebuild the app.
No UI automation: verifies native installation, process lifetime and WebView-originated HTTP.
"""
import argparse,hashlib,json,os,pathlib,platform,subprocess,threading,time,tempfile
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
P=argparse.ArgumentParser();P.add_argument('--scenario',choices=['fresh','upgrade'],required=True);P.add_argument('--version',default='0.5.0');P.add_argument('--baseline',default='0.4.18');P.add_argument('--installer-dir');A=P.parse_args()
O=pathlib.Path('acceptance-results').resolve();O.mkdir(exist_ok=True);records=[];requests=[]
windows=platform.system()=='Windows'
macos=platform.system()=='Darwin'
suffix='_x64-setup.exe' if windows else '_universal.dmg' if macos else '_amd64.deb'
def run(args,**kw):
 r=subprocess.run(args,text=True,encoding="utf-8",errors="replace",capture_output=True,**kw);(O/'commands.log').open('a',encoding='utf-8').write(str(args)+'\n'+r.stdout+r.stderr+'\n');r.check_returncode();return r.stdout.strip()
def package(version):
 name='kinora_'+version+suffix
 if version==A.version and A.installer_dir:
  files=list(pathlib.Path(A.installer_dir).rglob(name));assert len(files)==1,files
  return files[0].resolve()
 meta=json.loads(run(['gh','api','repos/PoirotJin/kinora/releases/tags/kinora-v'+version]))
 asset=next(x for x in meta['assets'] if x['name']==name);p=O/name
 run(['gh','release','download','kinora-v'+version,'--repo','PoirotJin/kinora','--pattern',name,'--dir',str(O)])
 assert p.stat().st_size==asset['size'] and 'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()==asset['digest']
 return p
class Probe(BaseHTTPRequestHandler):
 def do_GET(self):
  requests.append(dict(path=self.path,agent=self.headers.get('User-Agent','')))
  b=b'{"error":"login required"}';self.send_response(401);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
 def log_message(self,*a):pass
server=ThreadingHTTPServer(('127.0.0.1',0),Probe);threading.Thread(target=server.serve_forever,daemon=True).start()
origin='http://127.0.0.1:'+str(server.server_port)
if windows:
 data=pathlib.Path(os.environ['LOCALAPPDATA'])
 prefs=pathlib.Path(os.environ['APPDATA'])
elif macos:
 data=prefs=pathlib.Path.home()/'Library/Application Support'
else:
 data=pathlib.Path(os.environ.get('XDG_DATA_HOME',pathlib.Path.home()/'.local/share'))
 prefs=pathlib.Path(os.environ.get('XDG_CONFIG_HOME',pathlib.Path.home()/'.config'))
data=data/'com.motionfarm.desktop'
recovery=prefs/'com.motionfarm.desktop/server-preference.json'
data.mkdir(parents=True,exist_ok=True);config=data/'server.json';config.write_text(json.dumps({'origin':origin}));marker=data/'acceptance-preserve.txt';marker.write_text('preserve-across-upgrade')
install_dir=pathlib.Path(os.environ.get('RUNNER_TEMP','/tmp'))/'kinora-install-acceptance'
def install(version):
 p=package(version)
 if windows:
  r=subprocess.run([str(p),'/S','/D='+str(install_dir)],timeout=180);assert r.returncode==0,r.returncode
  binaries=[x for x in install_dir.glob('*.exe') if 'uninstall' not in x.name.lower()];assert len(binaries)==1,binaries;exe=binaries[0]
  v=run(['powershell','-NoProfile','-Command',f"(Get-Item -LiteralPath '{exe}').VersionInfo.ProductVersion"]);assert v.startswith(version),v
 elif macos:
  import plistlib
  install_dir.mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory() as mount:
   run(['hdiutil','attach','-nobrowse','-readonly','-mountpoint',mount,str(p)])
   try:
    apps=list(pathlib.Path(mount).glob('*.app'));assert len(apps)==1,apps
    app=install_dir/apps[0].name
    run(['ditto',str(apps[0]),str(app)])
   finally:
    run(['hdiutil','detach',mount])
  info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
  v=info['CFBundleShortVersionString'];assert v==version,v
  exe=app/'Contents/MacOS'/info['CFBundleExecutable']
 else:
  run(['sudo','apt-get','install','-y',str(p)],timeout=180)
  pkg=run(['dpkg-deb','-f',str(p),'Package']);v=run(['dpkg-query','-W','-f=${Version}',pkg]);assert v==version,v
  binaries=[pathlib.Path(x) for x in run(['dpkg','-L',pkg]).splitlines() if x.startswith('/usr/bin/')];assert len(binaries)==1,binaries;exe=binaries[0]
  libs=run(['ldd',str(exe)]);assert 'not found' not in libs
 assert json.loads(config.read_text())['origin']==origin and marker.read_text()=='preserve-across-upgrade'
 return exe,v

def verify_startup(exe,version,variant):
 requests.clear();log=(O/(version+'-'+variant+'-startup.log')).open('wb')
 proc=subprocess.Popen([str(exe)],stdout=log,stderr=subprocess.STDOUT)
 try:
  deadline=time.monotonic()+30
  while time.monotonic()<deadline and not requests:
   assert proc.poll() is None, 'App exited early: '+str(proc.poll());time.sleep(.5)
  assert requests,'WebView did not issue an API request to the configured server'
  assert any('MotionFarm-Desktop/'+version in r['agent'] for r in requests),requests
  time.sleep(5);assert proc.poll() is None,'App terminated after startup'
  records.append(dict(version=version,native_installer=True,installed_version=version,variant=variant,process_alive=True,webview_api_requests=list(requests),settings_preserved=True,executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest()))
 finally:
  proc.terminate()
  try:proc.wait(timeout=10)
  except subprocess.TimeoutExpired:proc.kill();proc.wait()
  log.close()
try:
 if A.scenario=='upgrade':
  baseline_exe,baseline_version=install(A.baseline)
  verify_startup(baseline_exe,baseline_version,'baseline')
 exe,version=install(A.version)
 verify_startup(exe,version,'installed')
 assert json.loads(recovery.read_text())['origin']==origin
 config.unlink()
 verify_startup(exe,version,'restored')
 assert json.loads(config.read_text())['origin']==origin
 (O/'report.json').write_text(json.dumps(dict(os=platform.platform(),scenario=A.scenario,passed=True,checks=records,scope='Native installer and startup/WebView API smoke; no interactive UI or robot playback validation'),indent=2))
 print('PASS',platform.platform(),A.scenario)
finally:
 (O/'requests.json').write_text(json.dumps(requests,indent=2));server.shutdown()
