"""Start/stop/status for the explicitly requested MotionFarm review service."""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
FOLDER=ROOT/'outputs/text_motion_farm/review'
TLS_CERT=FOLDER/'tls'/'cert.pem'
TLS_KEY=FOLDER/'tls'/'key.pem'


def running_pid():
    try:
        pid=int((FOLDER/'server.pid').read_text())
        args=Path('/proc/%d/cmdline'%pid).read_bytes().split(b'\0')
        return pid if str(ROOT/'scripts/review_server.py').encode() in args else None
    except (OSError,ValueError): return None


def scheme(secure):
    return 'https' if secure else 'http'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','stop','status','access-url','generate-tls'])
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--tls-cert',type=Path)
    parser.add_argument('--tls-key',type=Path)
    parser.add_argument('--origins',default='')
    args=parser.parse_args()
    FOLDER.mkdir(parents=True,exist_ok=True)
    leftover=FOLDER/'access_token'
    leftover.unlink(missing_ok=True)
    pid=running_pid()
    if args.action=='generate-tls':
        TLS_CERT.parent.mkdir(parents=True,exist_ok=True)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-sha256','-days','825','-nodes',
            '-keyout',str(TLS_KEY),'-out',str(TLS_CERT),
            '-subj','/CN='+args.host,
            '-addext','subjectAltName=IP:'+args.host+',DNS:'+args.host+',DNS:localhost'],check=True)
        os.chmod(TLS_CERT,0o600);os.chmod(TLS_KEY,0o600)
        print(json.dumps({'cert':str(TLS_CERT),'key':str(TLS_KEY)}));return
    cert=args.tls_cert or (TLS_CERT if TLS_CERT.exists() else None)
    key=args.tls_key or (TLS_KEY if TLS_KEY.exists() else None)
    secure=bool(cert)
    if args.action=='stop':
        if pid:
            os.kill(pid,signal.SIGTERM)
            for _ in range(40):
                if running_pid() is None: break
                time.sleep(.1)
            if running_pid(): raise RuntimeError('Server has not stopped; inspect server.log')
            (FOLDER/'server.pid').unlink(missing_ok=True)
        print('Review server stopped');return
    if args.action=='start' and pid is None:
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            probe.bind((args.host,args.port))
        command=[sys.executable,'-u',str(ROOT/'scripts/review_server.py'),'--host',args.host,'--port',str(args.port)]
        origin=args.origins or '%s://%s:%s'%(scheme(secure),args.host,args.port)
        command.extend(['--origins',origin])
        if cert:
            command.extend(['--tls-cert',str(cert),'--tls-key',str(key or cert)])
        with (FOLDER/'server.log').open('ab') as log:
            child=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,
                env=dict(os.environ,OPENBLAS_NUM_THREADS='2',OMP_NUM_THREADS='2'))
        (FOLDER/'server.pid').write_text(str(child.pid))
        time.sleep(.5)
        if child.poll() is not None: raise RuntimeError('Start failed; inspect server.log')
        pid=child.pid
        (FOLDER/'address.json').write_text(json.dumps(dict(host=args.host,port=args.port,tls=secure,origins=origin)))
    address=json.loads((FOLDER/'address.json').read_text()) if (FOLDER/'address.json').exists() else dict(host=args.host,port=args.port)
    url='%s://%s:%s/'%(scheme(address.get('tls',secure)),address['host'],address['port'])
    if args.action=='access-url':
        if pid is None: raise RuntimeError('Start the review server first')
        print(url)
    else: print(json.dumps(dict(running=bool(pid),pid=pid,url=url,tls=address.get('tls',secure))))


if __name__=='__main__': main()
