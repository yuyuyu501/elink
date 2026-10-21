import pytest

from scripts.prune_release_assets import obsolete, prune


@pytest.mark.parametrize('tag,expected', [
    ('v0.4.0', True), ('0.4.36', True), ('v0.4.38', True),
    ('v0.4.37', False), ('0.4.37', False), ('v0.5.0', False),
    ('v0.4.37-rc1', False), ('v0.4.036', False), ('v1.4.36', False),
])
def test_release_scope(tag, expected):
    assert obsolete({'tag_name': tag}) == expected


def test_prune_only_deletes_selected_assets():
    items = [dict(id=i, tag_name=tag, assets=[dict(id=i * 10, name='package.zip', size=10)])
             for i, tag in enumerate(('v0.4.36', 'v0.4.37', 'v0.5.0'), 1)]
    calls = []

    def api(url, method='GET'):
        if method == 'DELETE':
            calls.append(url)
            items[0]['assets'].clear()
        else:
            return items

    assert prune(api)['count'] == 1
    assert calls == []
    assert prune(api, True)['count'] == 1
    assert calls == ['https://api.github.com/repos/yuyuyu501/elink/releases/assets/10']
    assert all(item['assets'] for item in items[1:])
