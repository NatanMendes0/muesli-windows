"""Windows updater selects the expected binary, including on corporate networks."""
from types import SimpleNamespace

import launch
from muesli import api


def test_release_info_prefers_versioned_zip(monkeypatch):
    host = 'https://github.com/NatanMendes0/muesli-windows'
    tag = 'v1.6.2'
    name = f'Muesli-windows-x64-{tag}.zip'
    assets = [
        {'name': 'Muesli-windows-x64.zip', 'browser_download_url': f'{host}/legacy'},
        {'name': name, 'browser_download_url': f'{host}/versioned'},
    ]
    response = SimpleNamespace(status_code=200, json=lambda: {'tag_name': tag, 'assets': assets})
    monkeypatch.setattr(api.requests, 'get', lambda *args, **kwargs: response)
    result = api.Api().check_updates()
    assert result['zip_name'] == name
    assert result['zip_url'] == f'{host}/versioned'


def test_redirect_fallback_downloads_versioned_zip(monkeypatch):
    host = 'https://github.com/NatanMendes0/muesli-windows'
    tag = 'v1.6.2'
    responses = iter([
        SimpleNamespace(status_code=403),
        SimpleNamespace(headers={'Location': f'{host}/releases/tag/{tag}'}),
    ])
    monkeypatch.setattr(api.requests, 'get', lambda *args, **kwargs: next(responses))
    result = api.Api().check_updates()
    assert result['zip_name'] == f'Muesli-windows-x64-{tag}.zip'
    assert result['zip_url'] == f'{host}/releases/download/{tag}/{result["zip_name"]}'


def test_other_instances_matches_legacy_and_versioned_names_only():
    rows = ('"Muesli-windows-x64-v1.6.2.exe","100"\n'
            '"Muesli-windows-x64-v1.6.1.exe","200"\n'
            '"Muesli.exe","300"\n'
            '"Muesli-windows-x64-helper.exe","400"\n'
            '"Muesli-other.exe","500"\n')
    assert list(launch._other_instance_pids(rows, 100)) == ['200', '300']
