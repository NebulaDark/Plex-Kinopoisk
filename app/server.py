import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import parse_qs,urlsplit
from xml.etree.ElementTree import Element,SubElement,tostring
from . import __version__
from .provider import Provider,container,number
from .storage import Store
from .upstream import Sources,SourceError

STATIC=Path(__file__).parent/'static'

class AuthError(Exception):
    pass

def xml_response(data):
    def fill(node,value):
        for k,v in value.items():
            if isinstance(v,list):
                for entry in v:fill(SubElement(node,k),entry)
            elif isinstance(v,dict):fill(SubElement(node,k),v)
            elif v is not None:node.set(k,('1' if v else '0') if isinstance(v,bool) else str(v))
    name=next(iter(data));root=Element(name);fill(root,data[name])
    return tostring(root,encoding='utf-8',xml_declaration=True)

class Application:
    def __init__(self,directory):
        self.store=Store(directory);self.sources=Sources(self.store)
        self.providers={kind:Provider(self.store,self.sources,kind) for kind in ['movies','shows']}
        self.sessions={};self.auth_lock=threading.Lock();self.failures={}
        self.started=time.time()

    def login(self,token,ip):
        with self.auth_lock:
            now=time.time()
            self.failures={k:v for k,v in self.failures.items() if v[1]>now-60}
            count,ts=self.failures.get(ip,(0,now))
            if count>=10:raise AuthError('rate_limited')
            if not isinstance(token,str) or not hmac.compare_digest(token,self.store.admin_token):
                self.failures[ip]=(count+1,ts);raise AuthError('unauthorized')
            self.failures.pop(ip,None)
            self.sessions={k:v for k,v in self.sessions.items() if v>now}
            session=secrets.token_urlsafe(32);self.sessions[session]=now+8*3600
            return session

    def authorized(self,headers):
        token=headers.get('Authorization','').removeprefix('Bearer ')
        if token and hmac.compare_digest(token,self.store.admin_token):return True
        cookies=SimpleCookie()
        try:cookies.load(headers.get('Cookie',''))
        except Exception:return False
        session=cookies.get('session')
        with self.auth_lock:return bool(session and self.sessions.get(session.value,0)>time.time())

def handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version='PlexKinopois/'+__version__
        def log_message(self,fmt,*args):
            # Do not record request bodies, query strings, headers or API credentials.
            logging.info('%s %s',self.command,urlsplit(self.path).path)

        def send(self,status,data,ctype='application/json; charset=utf-8',headers=None):
            if isinstance(data,(dict,list)):data=json.dumps(data,ensure_ascii=False).encode()
            elif isinstance(data,str):data=data.encode()
            self.send_response(status)
            self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(data)))
            self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: http:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            for k,v in (headers or {}).items():self.send_header(k,v)
            self.end_headers()
            if self.command!='HEAD':self.wfile.write(data)

        def body(self):
            n=number(self.headers.get('Content-Length'))
            if n is None or not 0<n<=65536:raise ValueError('invalid_body_size')
            if self.headers.get_content_type()!='application/json':raise ValueError('json_required')
            try:value=json.loads(self.rfile.read(n))
            except (ValueError,UnicodeDecodeError):raise ValueError('invalid_json') from None
            if not isinstance(value,dict):raise ValueError('object_required')
            return value

        def do_HEAD(self):self.do_GET()
        def do_GET(self):self.dispatch()
        def do_POST(self):self.dispatch()
        def dispatch(self):
            try:self.route()
            except AuthError as e:self.send(401,{'error':str(e)})
            except ValueError as e:self.send(400,{'error':str(e)})
            except SourceError as e:
                status=404 if e.code in {'404','not_found'} else 503
                self.send(status,{'error':'source_error','source':e.source,'code':e.code})
            except (BrokenPipeError,ConnectionResetError):pass
            except Exception:
                app.store.event('service','internal_error')
                self.send(500,{'error':'internal_error'})

        def route(self):
            parsed=urlsplit(self.path);path=parsed.path.rstrip('/') or '/'
            query={k:v[-1] for k,v in parse_qs(parsed.query).items()}
            # Provider groups created with 0.1.0 may retain feature keys that
            # already contain the registered provider path. Plex then prefixes
            # that path once more. Keep those saved groups working while new
            # registrations use the corrected provider-relative keys.
            for kind in app.providers:
                duplicate='/providers/'+kind+'/providers/'+kind
                if path==duplicate or path.startswith(duplicate+'/'):
                    path='/providers/'+kind+path[len(duplicate):]
                    break
            if path=='/health' and self.command in {'GET','HEAD'}:
                return self.send(200,{'ok':True,'version':__version__})
            if path in {'/','/app.js','/style.css'} and self.command in {'GET','HEAD'}:
                name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[path]
                return self.send(200,(STATIC/name).read_bytes(),{'/':'text/html; charset=utf-8','/app.js':'text/javascript; charset=utf-8','/style.css':'text/css; charset=utf-8'}[path])
            if path.startswith('/api/'):
                origin=self.headers.get('Origin')
                if origin and urlsplit(origin).netloc!=self.headers.get('Host'):raise AuthError('origin_rejected')
                if path=='/api/login' and self.command=='POST':
                    session=app.login(self.body().get('token'),self.client_address[0])
                    return self.send(200,{'ok':True},headers={'Set-Cookie':'session='+session+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'})
                if not app.authorized(self.headers):raise AuthError('unauthorized')
                if path=='/api/logout' and self.command=='POST':
                    cookies=SimpleCookie(self.headers.get('Cookie',''))
                    if cookies.get('session'):
                        with app.auth_lock:app.sessions.pop(cookies['session'].value,None)
                    return self.send(200,{'ok':True},headers={'Set-Cookie':'session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
                if path=='/api/settings':
                    if self.command=='POST':app.store.update(self.body())
                    return self.send(200,app.store.public())
                if path=='/api/clear-secret' and self.command=='POST':
                    app.store.clear_secret(self.body().get('name'));return self.send(200,app.store.public())
                if path=='/api/status':
                    return self.send(200,{'version':__version__,'uptime':int(time.time()-app.started),
                        'configured':bool(app.store.get()['kp_key']),**app.store.diagnostics(),
                        'providers':{'movies':'/providers/movies','shows':'/providers/shows'}})
                if path=='/api/check' and self.command=='POST':
                    return self.send(200,app.sources.check(self.body().get('source')))
                if path=='/api/mappings':
                    if self.command=='POST':
                        data=self.body();app.store.map(data.get('hint'),data.get('kp'))
                    return self.send(200,app.store.mappings())
                if path=='/api/preview' and self.command=='POST':
                    data=self.body();kind=data.get('kind','movies')
                    if kind not in app.providers:raise ValueError('invalid_provider')
                    provider=app.providers[kind];lang=data.get('language',app.store.get()['language'])
                    if not isinstance(lang,str):raise ValueError('invalid_language')
                    if data.get('key'):
                        return self.send(200,{'metadata':provider.metadata(data['key'],lang,kind=='shows'),
                                              'supplements':provider.supplements(data['key'])})
                    return self.send(200,{'matches':provider.match({'type':1 if kind=='movies' else 2,'title':data.get('title'),'year':data.get('year'),'manual':1},lang)})
                if path=='/api/cache-clear' and self.command=='POST':
                    with app.store.lock:app.store.db.execute('DELETE FROM cache');app.store.db.commit()
                    return self.send(200,{'ok':True})
                return self.send(404,{'error':'not_found'})
            m=re.fullmatch(r'/providers/(movies|shows)(?:/library/metadata(?:/(.*))?)?',path)
            if not m:return self.send(404,{'error':'not_found'})
            provider=app.providers[m[1]];tail=m[2]
            lang=query.get('X-Plex-Language',self.headers.get('X-Plex-Language',app.store.get()['language']))
            if path in {'/providers/movies','/providers/shows'} and self.command in {'GET','HEAD'}:
                payload=provider.manifest()
            elif tail=='matches' and self.command=='POST':
                body=self.body();payload=container(provider.match(body,lang))
            elif tail and self.command in {'GET','HEAD'}:
                segments=tail.split('/');key=segments[0]
                start=number(query.get('X-Plex-Container-Start',self.headers.get('X-Plex-Container-Start','0')))
                size=number(query.get('X-Plex-Container-Size',self.headers.get('X-Plex-Container-Size','100')))
                if start is None or size is None or start<0 or not 0<=size<=1000:raise ValueError('invalid_pagination')
                if len(segments)==2 and segments[1] in {'children','grandchildren'}:
                    payload=container(provider.children(key,lang,segments[1]=='grandchildren'),start,size)
                elif len(segments)==2 and segments[1]=='images':
                    payload=container(provider.metadata(key,lang).get('Image',[]),start,size,'Image')
                elif len(segments)==1:
                    payload=container([provider.metadata(key,lang,query.get('includeChildren')=='1')])
                else:return self.send(404,{'error':'not_found'})
            else:return self.send(405,{'error':'method_not_allowed'})
            if 'application/json' in self.headers.get('Accept','application/json'):
                return self.send(200,payload)
            return self.send(200,xml_response(payload),'application/xml; charset=utf-8')
    return Handler

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    app=Application(os.getenv('DATA_DIR','data'))
    httpd=ThreadingHTTPServer((os.getenv('HOST','0.0.0.0'),int(os.getenv('PORT','8765'))),handler(app))
    httpd.daemon_threads=True
    logging.info('Plex-Kinopois %s ready; administrator token is in data/admin-token',__version__)
    httpd.serve_forever()

if __name__=='__main__':main()
