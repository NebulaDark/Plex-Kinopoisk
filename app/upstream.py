import hashlib
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler

class SourceError(Exception):
    def __init__(self, source, code):
        self.source, self.code = source,str(code)
        super().__init__(source+':'+self.code)

class Sources:
    BASES = {'kp':'https://kinopoiskapiunofficial.tech', 'tmdb':'https://api.themoviedb.org/3',
             'fanart':'https://webservice.fanart.tv/v3', 'omdb':'https://www.omdbapi.com'}
    def __init__(self,store):
        self.store=store
        self.lock=threading.Lock()
        self.last=0
        self.inflight=threading.BoundedSemaphore(4)

    def get(self,source,path,params=None,ttl=None):
        conf=self.store.get()
        key=conf[source+'_key']
        if not key: raise SourceError(source,'key_required')
        params=dict(params or {})
        if source=='tmdb':params['api_key']=key
        if source=='fanart':params['api_key']=key
        if source=='omdb':params['apikey']=key
        url=self.BASES[source]+path+('?' + urlencode(params) if params else '')
        cachekey=hashlib.sha256((url+key).encode()).hexdigest()
        data=self.store.cached(cachekey)
        if data is not None:return data
        headers={'Accept':'application/json','User-Agent':'Plex-Kinopoisk/0.1'}
        if source=='kp':headers['X-API-KEY']=key
        proxy=conf['proxy_url']
        opener=build_opener(ProxyHandler({'http':proxy,'https':proxy} if proxy else {}))
        with self.inflight:
            for attempt in range(3):
                with self.lock:
                    delay=1/conf['requests_per_second']-(time.monotonic()-self.last)
                    if delay>0:time.sleep(delay)
                    self.last=time.monotonic()
                try:
                    with opener.open(Request(url,headers=headers),timeout=15) as response:
                        raw=response.read(8*1024*1024+1)
                        if len(raw)>8*1024*1024:raise SourceError(source,'response_too_large')
                        data=json.loads(raw)
                    if source=='omdb' and data.get('Response')=='False':raise SourceError(source,'not_found')
                    self.store.put(cachekey,data,ttl if ttl is not None else conf['cache_hours']*3600)
                    return data
                except HTTPError as e:
                    if e.code in {429,500,502,503,504} and attempt<2:
                        try: delay=min(5,max(1,float(e.headers.get('Retry-After','1'))))
                        except ValueError: delay=1
                        time.sleep(delay*(attempt+1));continue
                    self.store.event(source,e.code)
                    raise SourceError(source,e.code) from None
                except (URLError,TimeoutError,OSError,json.JSONDecodeError):
                    if attempt<2:time.sleep(attempt+1);continue
                    self.store.event(source,'connection_error')
                    raise SourceError(source,'connection_error') from None

    def optional(self,source,path,params=None):
        try:return self.get(source,path,params)
        except SourceError:return {}

    def kp(self,path,params=None):return self.get('kp',path,params)

    def check(self,source):
        paths={'kp':('/api/v2.2/films/301',{}), 'tmdb':('/configuration',{}),
               'fanart':('/movies/603',{}), 'omdb':('/',{'i':'tt0133093'})}
        if source not in paths:raise ValueError('invalid_source')
        self.get(source,*paths[source],ttl=60)
        return {'ok':True,'source':source}
