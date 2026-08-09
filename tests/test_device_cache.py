import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'kindle_hid_passthrough'))

from device_cache import DeviceCache


def test_suffixed_and_bare_addresses_share_one_file(tmp_path):
    cache = DeviceCache(str(tmp_path))
    cache.save('8A:76:5A:2F:36:23/P', {'report_map': 'aabb'})
    assert cache.load('8a:76:5a:2f:36:23') == {'report_map': 'aabb'}
    assert cache.clear('8A:76:5A:2F:36:23') == 1
    assert cache.load('8A:76:5A:2F:36:23/P') is None
