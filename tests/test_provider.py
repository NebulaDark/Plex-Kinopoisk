import tempfile
import unittest

from app.provider import Provider, container
from app.storage import Store
from app.upstream import SourceError


FILM = {
    'kinopoiskId': 301,
    'imdbId': 'tt0133093',
    'nameRu': 'Матрица',
    'nameOriginal': 'The Matrix',
    'year': 1999,
    'type': 'FILM',
    'description': 'Хакер узнаёт правду о мире.',
    'ratingKinopoisk': 8.5,
    'ratingImdb': 8.7,
    'ratingKinopoiskVoteCount': 100,
    'ratingImdbVoteCount': 200,
    'genres': [{'genre': 'фантастика'}],
    'countries': [{'country': 'США'}],
    'posterUrl': 'https://example.test/poster.jpg',
    'coverUrl': 'https://example.test/art.jpg',
    'filmLength': 136,
}

SHOW = {
    'kinopoiskId': 404900,
    'nameRu': 'Тестовый сериал',
    'nameOriginal': 'Test Show',
    'year': 2020,
    'type': 'TV_SERIES',
    'description': 'Описание сериала.',
}


class FakeSources:
    def kp(self, path, params=None):
        if path.endswith('/seasons'):
            return {'items': [
                {'number': 1, 'episodes': [
                    {'seasonNumber': 1, 'episodeNumber': 1, 'nameRu': 'Начало', 'nameEn': 'Beginning',
                     'synopsis': 'Первая серия', 'releaseDate': '2020-01-02'},
                    {'seasonNumber': 1, 'episodeNumber': 2, 'nameRu': 'Продолжение', 'nameEn': 'Next',
                     'synopsis': 'Вторая серия', 'releaseDate': '2020-01-09'},
                ]},
                {'number': 2, 'episodes': [
                    {'seasonNumber': 2, 'episodeNumber': 1, 'nameRu': 'Возвращение', 'nameEn': 'Return',
                     'releaseDate': '2021-03-01'},
                ]},
            ]}
        if path == '/api/v2.2/films/301': return dict(FILM)
        if path == '/api/v2.2/films/404900': return dict(SHOW)
        if path == '/api/v2.1/films/search-by-keyword':
            return {'pagesCount': 1, 'films': [dict(FILM, filmId=301),
                                               {'filmId': 999, 'nameRu': 'Матрица времени', 'year': '2017', 'type': 'FILM'}]}
        raise SourceError('kp', 'not_found')

    def optional(self, source, path, params=None):
        if path == '/api/v1/staff':
            return [{'professionKey': 'ACTOR', 'nameRu': 'Киану Ривз', 'nameEn': 'Keanu Reeves',
                     'description': 'Нео', 'posterUrl': 'https://example.test/person.jpg'}]
        if path.endswith('/similars'):
            return {'items': [{'filmId': 302, 'nameRu': 'Матрица: Перезагрузка'}]}
        return {}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.update({'kp_key': 'test'})
        self.sources = FakeSources()

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_manifest_has_separate_movie_type(self):
        provider = Provider(self.store, self.sources, 'movies')
        body = provider.manifest()['MediaProvider']
        self.assertEqual([1], [x['type'] for x in body['Types']])
        self.assertTrue(body['identifier'].startswith('tv.plex.agents.custom.'))

    def test_direct_kp_match(self):
        provider = Provider(self.store, self.sources, 'movies')
        result = provider.match({'type': 1, 'title': 'Matrix kp-301', 'manual': 0}, 'ru')
        self.assertEqual('kp-301', result[0]['ratingKey'])
        self.assertEqual(100, result[0]['score'])

    def test_title_and_year_matching(self):
        provider = Provider(self.store, self.sources, 'movies')
        result = provider.match({'type': 1, 'title': 'Матрица', 'year': 1999, 'manual': 0}, 'ru')
        self.assertEqual(['kp-301'], [x['ratingKey'] for x in result])

    def test_manual_search_omits_unrelated_zero_scores(self):
        provider = Provider(self.store, self.sources, 'movies')
        result = provider.match({'type': 1, 'title': 'Матрица', 'manual': 1}, 'ru')
        self.assertTrue(result)
        self.assertTrue(all(x['score'] >= 25 for x in result))

    def test_movie_metadata(self):
        provider = Provider(self.store, self.sources, 'movies')
        item = provider.metadata('kp-301', 'ru')
        self.assertEqual('Матрица', item['title'])
        self.assertEqual(8160000, item['duration'])
        self.assertEqual('Киану Ривз', item['Role'][0]['tag'])
        self.assertIn('КиноПоиск: 8.5', item['summary'])
        self.assertEqual('imdb://tt0133093', item['Guid'][0]['id'])

    def test_series_includes_last_season_and_episode(self):
        provider = Provider(self.store, self.sources, 'shows')
        seasons = provider.children('kp-404900', 'ru')
        self.assertEqual([1, 2], [x['index'] for x in seasons])
        episode = provider.metadata('kp-404900-s2-e1', 'ru')
        self.assertEqual('Возвращение', episode['title'])
        self.assertEqual('kp-404900-s2', episode['parentRatingKey'])
        self.assertEqual('kp-404900', episode['grandparentRatingKey'])

    def test_invalid_key_is_rejected(self):
        provider = Provider(self.store, self.sources, 'movies')
        with self.assertRaises(ValueError): provider.metadata('../../etc/passwd')

    def test_container_pagination(self):
        value = container([{'n': x} for x in range(5)], 2, 2)['MediaContainer']
        self.assertEqual(5, value['totalSize'])
        self.assertEqual([2, 3], [x['n'] for x in value['Metadata']])


if __name__ == '__main__': unittest.main()
