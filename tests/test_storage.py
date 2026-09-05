import json
import os
import tempfile
import unittest
from app.storage import Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store(self.tmp.name)

    def tearDown(self):
        self.store.db.close(); self.tmp.cleanup()

    def test_public_settings_hide_secrets(self):
        self.store.update({'kp_key':'secret'})
        public=self.store.public()
        self.assertNotIn('kp_key',public)
        self.assertTrue(public['kp_key_configured'])
        self.assertNotIn('secret',json.dumps(public))

    def test_secret_files_are_private(self):
        if os.name == 'nt': self.skipTest('POSIX file modes are not available on Windows')
        mode=os.stat(self.tmp.name+'/admin-token').st_mode & 0o777
        self.assertEqual(0o600,mode)

    def test_rejects_unknown_setting(self):
        with self.assertRaises(ValueError):self.store.update({'unknown':True})

    def test_validates_numeric_ranges(self):
        with self.assertRaises(ValueError):self.store.update({'search_pages':100})

    def test_mapping_lifecycle(self):
        self.store.map('imdb://tt0133093',301)
        self.assertEqual(301,self.store.mappings()['imdb://tt0133093'])
        self.store.map('imdb://tt0133093',None)
        self.assertEqual({},self.store.mappings())


if __name__ == '__main__':unittest.main()
