
import unittest, json
from unittest.mock import Mock, patch
from helpers.multiaspect.bucket import BucketManager
from tests.helpers.data import MockDataBackend

class TestBucketManager(unittest.TestCase):

    def setUp(self):
        self.data_backend = MockDataBackend()
        self.data_backend.exists = Mock(return_value=True)
        self.data_backend.write = Mock(return_value=True)
        self.data_backend.list_files = Mock(
            return_value=[
                ('subdir', '', 'image_path.png')
            ]
        )
        self.instance_data_root = "/some/fake/path"
        self.cache_file = "/some/fake/cache.json"
        # Overload cache file with json:
        with patch('pathlib.Path.exists', return_value=True):
            with self.assertLogs('BucketManager', level='WARNING'):
                self.bucket_manager = BucketManager(
                    instance_data_root=self.instance_data_root,
                    cache_file=self.cache_file,
                    data_backend=self.data_backend
                )

    def test_len(self):
        self.bucket_manager.aspect_ratio_bucket_indices = {
            '1.0': ['image1', 'image2'],
            '1.5': ['image3']
        }
        self.assertEqual(len(self.bucket_manager), 3)

    def test_discover_new_files(self):
        with patch.object(self.data_backend, 'list_files', return_value=[('root', ['dir'], ['image1.jpg', 'image2.png'])]):
            new_files = self.bucket_manager._discover_new_files()
            self.assertEqual(new_files, ['image1.jpg', 'image2.png'])

    def test_load_cache_valid(self):
        valid_cache_data = {
            "aspect_ratio_bucket_indices": {'1.0': ['image1', 'image2']},
            "instance_images_path": ['image1', 'image2']
        }
        with patch.object(self.data_backend, 'read', return_value=json.dumps(valid_cache_data)):
            self.bucket_manager._load_cache()
        self.assertEqual(self.bucket_manager.aspect_ratio_bucket_indices, {'1.0': ['image1', 'image2']})

    def test_load_cache_invalid(self):
        invalid_cache_data = "this is not valid json"
        with patch.object(self.data_backend, 'read', return_value=invalid_cache_data):
            with self.assertLogs('BucketManager', level='WARNING'):
                self.bucket_manager._load_cache()

    def test_save_cache(self):
        self.bucket_manager.aspect_ratio_bucket_indices = {'1.0': ['image1', 'image2']}
        self.bucket_manager.instance_images_path = ['image1', 'image2']
        with patch.object(self.data_backend, 'write') as mock_write:
            self.bucket_manager._save_cache()
        mock_write.assert_called_once()

    # Add more tests for other methods as needed

if __name__ == "__main__":
    unittest.main()