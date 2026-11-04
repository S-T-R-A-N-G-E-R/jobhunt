import pytest
import yaml
from typer.testing import CliRunner

from jobhunt import cli, config


def test_missing_companies_file_exits_with_hint(home):
    config.COMPANIES_PATH.unlink()
    with pytest.raises(SystemExit) as exc:
        config.load_companies()
    assert "companies.example.yaml" in str(exc.value)


def test_company_add_creates_file_from_example_header(home):
    config.COMPANIES_PATH.unlink()
    result = CliRunner().invoke(cli.app, ["company", "add", "https://careers.example.com/jobs"])
    assert result.exit_code == 1  # not a supported board, so it goes to manual_check
    text = config.COMPANIES_PATH.read_text()
    assert text.startswith("# Companies to watch.")
    data = yaml.safe_load(text)
    assert data == {"companies": [], "manual_check": ["https://careers.example.com/jobs"]}
