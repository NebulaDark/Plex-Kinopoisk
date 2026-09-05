import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

DEFAULTS = {
    'language': 'ru', 'kp_key': '', 'tmdb_key': '', 'fanart_key': '', 'omdb_key': '',
    'rating_source': 'kp', 'review_source': 'kp', 'trailer_source': 'all',
    'actors_eng': False, 'poster_priority': 'localized', 'poster_limit': 5,
    'trailer_limit': 5, 'extra_all': True, 'lookup_by_kinopoisk_id': True,
    'desc_show_slogan': False, 'desc_rating_kp': True, 'desc_rating_imdb': True,
    'desc_rating_vote_count': True, 'desc_rating_newline': True,
    'content_rating': 'age', 'collections': True, 'load_staff': True,
    'load_images': True, 'load_similar': True, 'cache_hours': 24,
    'requests_per_second': 2, 'search_pages': 3, 'match_threshold': 85,
    'proxy_url': '',
}
SECRETS = {'kp_key', 'tmdb_key', 'fanart_key', 'omdb_key', 'proxy_url'}
ENUMS = {'language': {'ru','en'}, 'rating_source': {'kp','imdb','tmdb','rt'},
         'review_source': {'kp','off'}, 'trailer_source': {'kp','tmdb','all','off'},
         'poster_priority': {'localized','textless','quality'}, 'content_rating': {'age','mpaa','off'}}
RANGES = {'poster_limit': (1,30), 'trailer_limit': (0,30), 'cache_hours': (1,720),
          'requests_per_second': (0.2,10), 'search_pages': (1,10), 'match_threshold': (50,100)}

class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.path = self.directory / 'settings.json'
        self.settings = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            self.settings.update(json.loads(self.path.read_text()))
        token_path = self.directory / 'admin-token'
        if not token_path.exists():
            self.atomic(token_path, secrets.token_urlsafe(32))
        self.admin_token = token_path.read_text().strip()
        self.db = sqlite3.connect(self.directory/'cache.sqlite3', check_same_thread=False)
        self.db.executescript('CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT, expires REAL);'
                             'CREATE TABLE IF NOT EXISTS mappings (hint TEXT PRIMARY KEY, kp INTEGER);'
                             'CREATE TABLE IF NOT EXISTS events (ts REAL, source TEXT, code TEXT);')
        self.db.commit()

    @staticmethod
    def atomic(path, text):
        tmp = path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def get(self):
        with self.lock:
            return copy.deepcopy(self.settings)

    def public(self):
        conf = self.get()
        for key in SECRETS:
            conf[key + '_configured'] = bool(conf.pop(key, ''))
        return conf

    def update(self, patch):
        if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
            raise ValueError('invalid_settings')
        from urllib.parse import urlsplit
        with self.lock:
            conf = self.get()
            for k,v in patch.items():
                if k in SECRETS:
                    if not isinstance(v,str) or len(v)>2048: raise ValueError('invalid_'+k)
                    # Blank means keep; explicit null is not accepted. UI uses clear endpoint.
                    if not v: continue
                elif k in ENUMS:
                    if v not in ENUMS[k]: raise ValueError('invalid_'+k)
                elif k in RANGES:
                    lo,hi=RANGES[k]
                    if isinstance(v,bool) or not isinstance(v,(float,int)) or not lo<=v<=hi: raise ValueError('invalid_'+k)
                    if k!='requests_per_second' and int(v)!=v: raise ValueError('invalid_'+k)
                elif isinstance(DEFAULTS[k],bool) and not isinstance(v,bool):
                    raise ValueError('invalid_'+k)
                if k=='proxy_url' and v:
                    u=urlsplit(v)
                    if u.scheme not in {'http','https'} or not u.hostname: raise ValueError('invalid_proxy_url')
                conf[k]=v
            self.atomic(self.path, json.dumps(conf, ensure_ascii=False, indent=2))
            self.settings=conf
            self.db.execute('DELETE FROM cache')
            self.db.commit()

    def clear_secret(self, name):
        if name not in SECRETS: raise ValueError('invalid_secret')
        with self.lock:
            self.settings[name]=''
            self.atomic(self.path,json.dumps(self.settings,ensure_ascii=False,indent=2))
            self.db.execute('DELETE FROM cache'); self.db.commit()

    def cached(self, key):
        with self.lock:
            row=self.db.execute('SELECT value FROM cache WHERE key=? AND expires>?',(key,time.time())).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, key, value, ttl):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',(key,json.dumps(value),time.time()+ttl))
            self.db.execute('DELETE FROM cache WHERE expires<?',(time.time(),))
            self.db.commit()

    def event(self, source, code):
        with self.lock:
            self.db.execute('INSERT INTO events VALUES (?,?,?)',(time.time(),source,str(code)))
            self.db.execute('DELETE FROM events WHERE rowid NOT IN (SELECT rowid FROM events ORDER BY ts DESC LIMIT 100)')
            self.db.commit()

    def diagnostics(self):
        with self.lock:
            return {'cache_entries': self.db.execute('SELECT count(*) FROM cache').fetchone()[0],
                    'events': [dict(zip(['time','source','code'],r)) for r in self.db.execute('SELECT * FROM events ORDER BY ts DESC LIMIT 20')]}

    def mappings(self):
        with self.lock:
            return dict(self.db.execute('SELECT hint,kp FROM mappings'))

    def map(self,hint,kp):
        if not isinstance(hint,str) or not hint.strip() or len(hint)>500: raise ValueError('invalid_hint')
        with self.lock:
            if kp is None: self.db.execute('DELETE FROM mappings WHERE hint=?',(hint.strip(),))
            else:
                if isinstance(kp,bool) or not isinstance(kp,int) or not 0<kp<10**10: raise ValueError('invalid_id')
                self.db.execute('INSERT OR REPLACE INTO mappings VALUES (?,?)',(hint.strip(),kp))
            self.db.commit()
