"""通过管理接口核验提供商凭据保存和普通账号权限。"""

from pathlib import Path

from tests.auth.support import admin_client, authenticated_client


def test_provider_configuration_never_returns_credentials(database_url: str, tmp_path: Path) -> None:
    """管理端可配置媒体服务但不回显值，普通账号没有配置权限。"""
    with admin_client(database_url, tmp_path) as client:
        saved = client.put('/api/settings/skill-providers', json={'variables': {
            'IMAGE_BACKEND': 'openai', 'OPENAI_API_KEY': 'private-provider-value',
            'COSYVOICE_API_KEY': 'another-private-value'}})
        assert saved.status_code == 200
        assert saved.json()['configured'] == ['COSYVOICE_API_KEY', 'IMAGE_BACKEND', 'OPENAI_API_KEY']
        assert 'private-provider-value' not in saved.text
        assert client.put('/api/settings/skill-providers', json={'variables': {'DATABASE_URL': 'invalid'}}).status_code == 422
        assert (tmp_path / 'skill-providers.json').stat().st_mode & 0o777 == 0o600
    with authenticated_client(database_url, tmp_path) as client:
        assert client.get('/api/settings/skill-providers').status_code == 401
