"""PoreiaGo model administration reads the local project configuration safely."""
from pathlib import Path

from models import model_admin_cli


def test_model_admin_cli_reads_project_dotenv(monkeypatch, tmp_path):
    project = tmp_path / 'PoreiaGo'
    (project / 'models').mkdir(parents=True)
    (project / 'pyproject.toml').write_text('[project]\nname = \"poreiago\"\n', encoding='utf-8')
    (project / '.env').write_text('POREIAGO_MODEL_ADMIN_TOKEN=local-admin-test\n', encoding='utf-8')
    monkeypatch.setattr(model_admin_cli, '__file__', str(project / 'models' / 'model_admin_cli.py'))
    monkeypatch.delenv('POREIAGO_MODEL_ADMIN_TOKEN', raising=False)
    model_admin_cli._load_cli_environment()
    assert model_admin_cli.os.environ['POREIAGO_MODEL_ADMIN_TOKEN'] == 'local-admin-test'


def test_model_admin_cli_preserves_exported_token(monkeypatch, tmp_path):
    project = tmp_path / 'PoreiaGo'
    (project / 'models').mkdir(parents=True)
    (project / 'pyproject.toml').write_text('[project]\nname = \"poreiago\"\n', encoding='utf-8')
    (project / '.env').write_text('POREIAGO_MODEL_ADMIN_TOKEN=local-admin-test\n', encoding='utf-8')
    monkeypatch.setattr(model_admin_cli, '__file__', str(project / 'models' / 'model_admin_cli.py'))
    monkeypatch.setenv('POREIAGO_MODEL_ADMIN_TOKEN', 'shell-admin-test')
    model_admin_cli._load_cli_environment()
    assert model_admin_cli.os.environ['POREIAGO_MODEL_ADMIN_TOKEN'] == 'shell-admin-test'


def test_model_admin_cli_installed_mode_uses_working_directory(monkeypatch, tmp_path):
    site_packages = tmp_path / 'site-packages'
    (site_packages / 'models').mkdir(parents=True)
    working = tmp_path / 'deployment'
    working.mkdir()
    (working / '.env').write_text('POREIAGO_MODEL_ADMIN_TOKEN=deployed-admin-test\n', encoding='utf-8')
    monkeypatch.setattr(model_admin_cli, '__file__', str(site_packages / 'models' / 'model_admin_cli.py'))
    monkeypatch.chdir(working)
    monkeypatch.delenv('POREIAGO_MODEL_ADMIN_TOKEN', raising=False)
    model_admin_cli._load_cli_environment()
    assert model_admin_cli.os.environ['POREIAGO_MODEL_ADMIN_TOKEN'] == 'deployed-admin-test'
