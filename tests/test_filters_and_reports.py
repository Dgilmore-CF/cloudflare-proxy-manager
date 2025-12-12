import json
from pathlib import Path

import pytest

from cloudflare_proxy_manager import CloudflareProxyManager


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN_TEST", "dummy")
    m = CloudflareProxyManager(state_file=str(tmp_path / "state.json"))
    return m


def test_matches_name_filters(manager):
    assert manager._matches_name_filters("api.example.com", include=None, exclude=None)
    assert manager._matches_name_filters("api.example.com", include=r"^api\\.", exclude=None)
    assert not manager._matches_name_filters("www.example.com", include=r"^api\\.", exclude=None)
    assert not manager._matches_name_filters("api.example.com", include=None, exclude=r"example\\.com$")


def test_write_report_files_creates_json_md_and_optional_csv(manager, tmp_path):
    results = {
        "accounts": {"test": {"zones_processed": 1}},
        "total_changes": 1,
        "dry_run": True,
        "changes": [
            {
                "action": "would_disable_proxy",
                "account": "test",
                "account_id": "N/A",
                "zone": "example.com",
                "zone_id": "z1",
                "record_id": "r1",
                "record_name": "api.example.com",
                "record_type": "A",
                "content": "1.2.3.4",
            }
        ],
    }

    report_dir = tmp_path / "reports"
    manager._write_report_files(report_dir, results, "disable")

    json_files = list(report_dir.glob("disable_*.json"))
    md_files = list(report_dir.glob("disable_*.md"))
    csv_files = list(report_dir.glob("disable_*.csv"))

    assert len(json_files) == 1
    assert len(md_files) == 1
    assert len(csv_files) == 1

    payload = json.loads(json_files[0].read_text())
    assert payload["total_changes"] == 1
    assert payload["changes"][0]["record_name"] == "api.example.com"
