"""Plex HTTP provider contract; domain IDs remain stable across refreshes."""
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import unquote, urlsplit
from .upstream import SourceError

TYPES={1:'movie',2:'show',3:'season',4:'episode'}
SHOW_TYPES={'TV_SERIES','MINI_SERIES','TV_SHOW'}

def text(value):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]','',value) if isinstance(value,str) else ''

def year(value):
    m=re.search(r'\b((?:18|19|20|21)\d{2})\b',str(value or ''))
    return int(m[1]) if m else None

def url(value):
    return value if isinstance(value,str) and urlsplit(value).scheme in {'http','https'} else None

def norm(value):
    return ' '.join(re.findall(r'\w+',unicodedata.normalize('NFKC',text(value)).casefold().replace('ё','е')))

def clean(item):
    return {k:v for k,v in item.items() if v is not None and v!='' and v!=[]}

def container(items,start=0,size=100,element='Metadata'):
    return {'MediaContainer':{'identifier':'tv.plex.provider.metadata','offset':start,
           'totalSize':len(items),'size':len(items[start:start+size]),element:items[start:start+size]}}

def number(value):
    try:return int(value)
    except (ValueError,TypeError):return None

class Provider:
    def __init__(self,store,sources,kind):
        if kind not in {'movies','shows'}:raise ValueError('invalid_provider')
        self.store,self.sources,self.kind=store,sources,kind
        self.identifier='tv.plex.agents.custom.plexkinopois.'+kind
        # Plex resolves feature and item keys against the provider URL that the
        # administrator registered. Including that URL's path prefix here makes
        # Plex append it twice (for example /providers/movies/providers/movies).
        self.base='/library/metadata'

    def manifest(self):
        return {'MediaProvider':{'identifier':self.identifier,'title':'Kinopoisk '+self.kind.title(),
                'version':'1.0.1','Types':[{'type':n,'Scheme':[{'scheme':self.identifier}]} for n in ([1] if self.kind=='movies' else [2,3,4])],
                'Feature':[{'type':'metadata','key':self.base},{'type':'match','key':self.base+'/matches'}]}}

    def guid(self,kind,key):return self.identifier+'://'+kind+'/'+key

    def base_item(self,kind,key,title,date=None):
        return clean({'ratingKey':key,'key':self.base+'/'+key,'guid':self.guid(kind,key),
                      'type':kind,'title':title,'originallyAvailableAt':date})

    def key(self,kp,season=None,episode=None):
        return 'kp-'+str(kp)+(('-s'+str(season)) if season is not None else '')+(('-e'+str(episode)) if episode is not None else '')

    def parse(self,key):
        m=re.fullmatch(r'kp-([1-9]\d{0,9})(?:-s(\d{1,4})(?:-e(\d{1,5}))?)?',key)
        if not m:raise ValueError('invalid_rating_key')
        if self.kind=='movies' and m[2] is not None:raise ValueError('invalid_rating_key')
        return tuple(int(x) if x is not None else None for x in m.groups())

    def title(self,film,lang):
        names=['nameEn','nameOriginal','nameRu'] if lang.startswith('en') else ['nameRu','nameEn','nameOriginal']
        return next((text(film.get(n)) for n in names if film.get(n)),str(film.get('kinopoiskId') or film.get('filmId') or 'Unknown'))

    def compatible(self,film):
        return (film.get('type') in SHOW_TYPES)==(self.kind=='shows')

    def film(self,kp):
        data=self.sources.kp('/api/v2.2/films/'+str(kp))
        if not isinstance(data,dict) or not self.compatible(data):raise SourceError('kp','not_found')
        return data

    def basic(self,film,lang):
        kp=film.get('kinopoiskId') or film.get('filmId')
        if not number(kp):raise SourceError('kp','invalid_response')
        release_year=year(film.get('year'))
        item=self.base_item('movie' if self.kind=='movies' else 'show',self.key(kp),self.title(film,lang),
                            str(release_year)+'-01-01' if release_year else '1900-01-01')
        item.update(clean({'year':year(film.get('year')),'thumb':url(film.get('posterUrl')),
                          'originalTitle':text(film.get('nameOriginal') or film.get('nameEn')),
                          'isAdult':bool(film.get('isAdult'))}))
        if film.get('imdbId'):item['Guid']=[{'id':'imdb://'+film['imdbId']}]
        return item

    def match(self,hints,lang='ru'):
        typ=number(hints.get('type'))
        if typ not in ([1] if self.kind=='movies' else [2,3,4]):raise ValueError('invalid_type')
        title=text(hints.get({1:'title',2:'title',3:'parentTitle',4:'grandparentTitle'}[typ]))
        filename=unquote(text(hints.get('filename')))
        external=text(hints.get('guid'))
        manual=number(hints.get('manual'))==1
        conf=self.store.get()
        direct=None
        mappings=self.store.mappings()
        for hint in [external,filename,title]:
            if hint in mappings:direct=mappings[hint];break
        own=re.fullmatch(re.escape(self.identifier)+r'://(?:movie|show|season|episode)/(kp-\d+(?:-s\d+(?:-e\d+)?)?)',external)
        if own:direct=self.parse(own[1])[0]
        if direct is None and conf['lookup_by_kinopoisk_id']:
            ids=set(re.findall(r'(?:\bkp-|\bkinopoisk-|kinopoisk\.ru/(?:film|series)/)(\d+)',title+' '+filename,re.I))
            if len(ids)>1:raise ValueError('ambiguous_kp_ids')
            if ids:direct=int(next(iter(ids)))
        if direct:
            candidates=[(100,self.film(direct))]
        elif re.fullmatch(r'imdb://tt\d+',external):
            found=self.sources.kp('/api/v2.2/films',{'imdbId':external.split('://')[1]})
            candidates=[(100,f) for f in found.get('items',[]) if self.compatible(f)]
        else:
            if not title and filename:
                title=re.split(r'[/\\]',filename)[-1].rsplit('.',1)[0]
                title=re.split(r'(?i)\b(?:19|20)\d{2}\b|\bS\d+E\d+\b',title)[0].replace('.',' ').strip(' ()-_')
            if not title:raise ValueError('title_required')
            found={}
            for page in range(1,conf['search_pages']+1):
                response=self.sources.kp('/api/v2.1/films/search-by-keyword',{'keyword':title,'page':page})
                for f in response.get('films',[]):
                    if self.compatible(f):found[f.get('filmId') or f.get('kinopoiskId')]=f
                if page>=int(response.get('pagesCount') or 1):break
            candidates=[]
            wanted=year(hints.get('year'))
            for f in found.values():
                score=max([SequenceMatcher(None,norm(title),norm(f.get(n))).ratio()*100 for n in ['nameRu','nameEn','nameOriginal']])
                actual=year(f.get('year'))
                if wanted and actual:score-=min(45,abs(wanted-actual)*7)
                elif wanted:score-=5
                candidates.append((round(max(0,score)),f))
        candidates.sort(key=lambda x:(-x[0],str(x[1].get('filmId') or x[1].get('kinopoiskId'))))
        if manual:
            # Manual search should be broad, but unrelated zero-score catalogue
            # entries are never useful and make the Plex/UI result list noisy.
            candidates=[c for c in candidates if c[0]>=25]
        else:
            candidates=[c for c in candidates if c[0]>=conf['match_threshold']]
            # Equal matches across remakes must be resolved manually.
            if len(candidates)>1 and candidates[0][0]-candidates[1][0]<2:return []
            candidates=candidates[:1]
        result=[]
        for score,f in candidates[:15]:
            if f.get('isAdult') is True and number(hints.get('includeAdult'))!=1:continue
            item=self.basic(f,lang)
            kp=number(f.get('kinopoiskId') or f.get('filmId'))
            if typ in {3,4}:
                sn=number(hints.get('index' if typ==3 else 'parentIndex'))
                ep=number(hints.get('index')) if typ==4 else None
                if typ==4 and (sn is None or ep is None) and hints.get('date'):
                    pairs=[(s['number'],e['episodeNumber']) for s in self.seasons(kp) for e in s.get('episodes',[]) if e.get('releaseDate')==hints['date']]
                    if len(pairs)!=1:continue
                    sn,ep=pairs[0]
                if sn is None or (typ==4 and ep is None):raise ValueError('episode_coordinates_required')
                try:item=self.metadata(self.key(kp,sn,ep),lang,number(hints.get('includeChildren'))==1)
                except SourceError as e:
                    if e.code=='not_found':continue
                    raise
            elif number(hints.get('includeChildren'))==1 and typ==2:
                children=self.children(item['ratingKey'],lang)
                item['Children']={'size':len(children),'Metadata':children}
            item['score']=score
            result.append(item)
        return result

    def seasons(self,kp):
        data=self.sources.kp('/api/v2.2/films/'+str(kp)+'/seasons')
        return sorted([s for s in data.get('items',[]) if number(s.get('number')) is not None],key=lambda s:int(s['number']))

    def child_items(self,kp,film,lang):
        show=self.basic(film,lang)
        result=[]
        for season in self.seasons(kp):
            sn=int(season['number'])
            dates=sorted([e['releaseDate'] for e in season.get('episodes',[]) if re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(e.get('releaseDate') or ''))])
            st=('Season ' if lang.startswith('en') else 'Сезон ')+str(sn)
            item=self.base_item('season',self.key(kp,sn),st,dates[0] if dates else None)
            self.parent(item,show,'parent')
            item['index']=sn
            episodes=[]
            for e in sorted(season.get('episodes',[]),key=lambda e:number(e.get('episodeNumber')) or 0):
                en=number(e.get('episodeNumber'))
                if en is None:continue
                et=self.title(e,lang)
                if et=='Unknown':et=('Episode ' if lang.startswith('en') else 'Эпизод ')+str(en)
                episode=self.base_item('episode',self.key(kp,sn,en),et,text(e.get('releaseDate')))
                episode.update(clean({'summary':text(e.get('synopsis')),'index':en,'parentIndex':sn,'year':year(e.get('releaseDate'))}))
                self.parent(episode,item,'parent');self.parent(episode,show,'grandparent')
                episodes.append(episode)
            result.append((item,episodes))
        return result

    @staticmethod
    def parent(item,parent,prefix):
        for k in ['ratingKey','key','guid','type','title','thumb']:
            if parent.get(k):item[prefix+k[0].upper()+k[1:]]=parent[k]

    def children(self,key,lang='ru',grandchildren=False):
        kp,sn,en=self.parse(key)
        if self.kind!='shows' or en is not None:return []
        pairs=self.child_items(kp,self.film(kp),lang)
        if sn is None:return [e for _,eps in pairs for e in eps] if grandchildren else [s for s,_ in pairs]
        return next((eps for s,eps in pairs if s['index']==sn),[])

    def metadata(self,key,lang='ru',include_children=False):
        kp,sn,en=self.parse(key)
        film=self.film(kp)
        if sn is not None:
            for s,episodes in self.child_items(kp,film,lang):
                if s['index']!=sn:continue
                if en is not None:
                    for ep in episodes:
                        if ep['index']==en:return ep
                else:
                    if include_children:s['Children']={'size':len(episodes),'Metadata':episodes}
                    return s
            raise SourceError('kp','not_found')
        conf=self.store.get()
        item=self.basic(film,lang)
        item.update(clean({'summary':text(film.get('description')),'tagline':text(film.get('slogan')),
                           'art':url(film.get('coverUrl')),'duration':number(film.get('filmLength'))*60000 if number(film.get('filmLength')) else None,
                           'Genre':[{'tag':g['genre']} for g in film.get('genres',[]) if g.get('genre')],
                           'Country':[{'tag':g['country']} for g in film.get('countries',[]) if g.get('country')]}))
        age=re.search(r'\d+',str(film.get('ratingAgeLimits') or ''))
        if conf['content_rating']=='age' and age:item['contentRating']='ru/'+age[0]+'+'
        if conf['content_rating']=='mpaa' and film.get('ratingMpaa'):item['contentRating']=film['ratingMpaa'].upper()
        base='/api/v2.2/films/'+str(kp)
        dist=self.sources.optional('kp',base+'/distributions')
        for d in sorted(dist.get('items',[]),key=lambda d:0 if d.get('type')=='WORLD_PREMIER' else 1):
            if d.get('type') in {'WORLD_PREMIER','COUNTRY_SPECIFIC'} and re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(d.get('date') or '')):
                item['originallyAvailableAt']=d['date'];break
        ratings={}
        for name,field in [('kp','ratingKinopoisk'),('imdb','ratingImdb')]:
            value=film.get(field)
            if isinstance(value,(int,float)) and 0<=value<=10:ratings[name]=float(value)
        if conf['load_staff']:
            staff=self.sources.optional('kp','/api/v1/staff',{'filmId':kp})
            if isinstance(staff,list):
                for person in staff:
                    kind={'ACTOR':'Role','DIRECTOR':'Director','WRITER':'Writer','PRODUCER':'Producer'}.get(person.get('professionKey'))
                    name=person.get('nameEn') if conf['actors_eng'] else person.get('nameRu')
                    name=name or person.get('nameRu') or person.get('nameEn')
                    if kind and name:item.setdefault(kind,[]).append(clean({'tag':name,'role':text(person.get('description')),'thumb':url(person.get('posterUrl')),'order':len(item.get(kind,[]))}))
        images=[]
        for typ,field in [('coverPoster','posterUrl'),('background','coverUrl'),('clearLogo','logoUrl')]:
            if url(film.get(field)):images.append({'type':typ,'url':film[field],'alt':item['title']})
        if conf['load_images']:
            for upstream_type,typ in [('POSTER','coverPoster'),('STILL','background')]:
                data=self.sources.optional('kp',base+'/images',{'type':upstream_type,'page':1})
                images.extend({'type':typ,'url':x['imageUrl'],'alt':item['title']} for x in data.get('items',[])[:conf['poster_limit']] if url(x.get('imageUrl')))
        if conf['load_similar']:
            similar=self.sources.optional('kp',base+'/similars')
            item['Similar']=[{'guid':self.guid(item['type'],self.key(x['filmId'])),'tag':self.title(x,lang)} for x in similar.get('items',[]) if number(x.get('filmId'))]
        self.enrich(item,film,ratings,images,lang)
        selected=ratings.get(conf['rating_source'])
        if selected is not None:item['rating']=selected
        # Plex has no custom Kinopoisk badge; do not mislabel its rating as IMDb.
        badges={'imdb':'imdb://image.rating','tmdb':'themoviedb://image.rating','rt':'rottentomatoes://image.rating.ripe'}
        item['Rating']=[{'image':badges[k],'type':'critic' if k=='rt' else 'audience','value':v} for k,v in ratings.items() if k in badges]
        prefix=[]
        if conf['desc_show_slogan'] and item.get('tagline'):prefix.append(item['tagline'])
        for k,name,field in [('kp','КиноПоиск','ratingKinopoiskVoteCount'),('imdb','IMDb','ratingImdbVoteCount')]:
            if conf['desc_rating_'+k] and k in ratings:
                line=name+': '+str(ratings[k])
                if conf['desc_rating_vote_count'] and film.get(field):line+=' ('+str(film[field])+')'
                prefix.append(line)
        if prefix:item['summary']=('\n' if conf['desc_rating_newline'] else '. ').join(prefix)+'\n\n'+item.get('summary','')
        unique=list({x['url']:x for x in images}.values())
        item['Image']=unique
        posters=[i for i in unique if i['type']=='coverPoster']
        if posters:item['thumb']=posters[0]['url']
        if include_children and self.kind=='shows':
            children=self.children(key,lang)
            item['Children']={'size':len(children),'Metadata':children}
        return clean(item)

    def enrich(self,item,film,ratings,images,lang):
        conf=self.store.get();imdb=film.get('imdbId');tmdb=None;tvdb=None
        media='movie' if self.kind=='movies' else 'tv'
        if conf['tmdb_key'] and imdb:
            found=self.sources.optional('tmdb','/find/'+imdb,{'external_source':'imdb_id'})
            matches=found.get(media+'_results',[])
            if matches:
                tmdb=matches[0]['id']
                data=self.sources.optional('tmdb','/'+media+'/'+str(tmdb),{'language':'en-US' if lang.startswith('en') else 'ru-RU','append_to_response':'external_ids,images','include_image_language':('en' if lang.startswith('en') else 'ru')+',null'})
                item.setdefault('Guid',[]).append({'id':'tmdb://'+str(tmdb)})
                tvdb=data.get('external_ids',{}).get('tvdb_id')
                if tvdb:item['Guid'].append({'id':'tvdb://'+str(tvdb)})
                if isinstance(data.get('vote_average'),(int,float)):ratings['tmdb']=data['vote_average']
                if conf['collections'] and data.get('belongs_to_collection'):item['Collection']=[{'tag':data['belongs_to_collection']['name']}]
                for src,dst in [('production_companies','Studio'),('networks','Network')]:
                    if data.get(src):item[dst]=[{'tag':x['name']} for x in data[src] if x.get('name')]
                if not item.get('originallyAvailableAt'):
                    date=data.get('release_date' if media=='movie' else 'first_air_date')
                    if date:item['originallyAvailableAt']=date
                if conf['load_images']:
                    for src,typ in [('posters','coverPoster'),('backdrops','background'),('logos','clearLogo')]:
                        vals=data.get('images',{}).get(src,[])
                        def priority(x):
                            language=x.get('iso_639_1')
                            preferred=(language is None) if conf['poster_priority']=='textless' else language==lang[:2]
                            return (preferred if conf['poster_priority']!='quality' else 0,x.get('width',0))
                        for x in sorted(vals,key=priority,reverse=True)[:conf['poster_limit']]:
                            if x.get('file_path'):images.append({'type':typ,'url':'https://image.tmdb.org/t/p/original'+x['file_path'],'alt':item['title']})
        if conf['omdb_key'] and imdb:
            data=self.sources.optional('omdb','/',{'i':imdb})
            for r in data.get('Ratings',[]):
                if r.get('Source')=='Rotten Tomatoes' and re.fullmatch(r'\d+%',r.get('Value','')):ratings['rt']=float(r['Value'][:-1])/10
        if conf['fanart_key'] and ((media=='movie' and tmdb) or tvdb):
            data=self.sources.optional('fanart',('/movies/' if media=='movie' else '/tv/')+str(tmdb if media=='movie' else tvdb))
            for src,typ in [('movieposter','coverPoster'),('tvposter','coverPoster'),('moviebackground','background'),('showbackground','background'),('hdmovielogo','clearLogo'),('hdtvlogo','clearLogo')]:
                images.extend({'type':typ,'url':x['url'],'alt':item['title']} for x in data.get(src,[])[:conf['poster_limit']] if url(x.get('url')))

    def supplements(self,key):
        kp,_,_=self.parse(key);conf=self.store.get();base='/api/v2.2/films/'+str(kp)
        result={'reviews':[],'videos':[], 'note':'external_links_not_native_extras'}
        if conf['review_source']=='kp':
            data=self.sources.optional('kp',base+'/reviews',{'page':1,'order':'DATE_DESC'})
            result['reviews']=[{'author':text(x.get('author')),'title':text(x.get('title')),'text':text(x.get('description')),'type':x.get('type')} for x in data.get('items',[])[:20]]
        if conf['trailer_source'] in {'kp','all'}:
            data=self.sources.optional('kp',base+'/videos')
            result['videos']=[{'name':text(x.get('name')),'url':url(x.get('url')),'source':'kp'} for x in data.get('items',[]) if url(x.get('url'))][:conf['trailer_limit']]
        if conf['trailer_source'] in {'tmdb','all'} and conf['tmdb_key']:
            film=self.film(kp);imdb=film.get('imdbId');media='movie' if self.kind=='movies' else 'tv'
            if imdb:
                found=self.sources.optional('tmdb','/find/'+imdb,{'external_source':'imdb_id'})
                matches=found.get(media+'_results',[])
                if matches:
                    data=self.sources.optional('tmdb','/'+media+'/'+str(matches[0]['id'])+'/videos',{'language':'ru-RU' if conf['language']=='ru' else 'en-US'})
                    for x in data.get('results',[]):
                        if not conf['extra_all'] and x.get('type') not in {'Trailer','Teaser'}:continue
                        if x.get('site')=='YouTube' and re.fullmatch(r'[A-Za-z0-9_-]+',x.get('key','')):
                            result['videos'].append({'name':text(x.get('name')),'url':'https://www.youtube.com/watch?v='+x['key'],'source':'tmdb'})
                    result['videos']=result['videos'][:conf['trailer_limit']]
        return result
