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


def test_matches_tag_filters_default_fields(manager):
    fields = {"name": "api.example.com", "content": "1.2.3.4", "comment": "hello"}
    assert manager._matches_tag_filters(fields, tags=None, tag_fields=None)
    assert manager._matches_tag_filters(fields, tags=["api"], tag_fields=None)
    assert manager._matches_tag_filters(fields, tags=["1.2.3"], tag_fields=None)
    assert not manager._matches_tag_filters(fields, tags=["hello"], tag_fields=None)


def test_matches_tag_filters_comment_field(manager):
    fields = {"name": "api.example.com", "content": "1.2.3.4", "comment": "managed-by-script"}
    assert manager._matches_tag_filters(fields, tags=["managed"], tag_fields=["comment"])
    assert not manager._matches_tag_filters(fields, tags=["api"], tag_fields=["comment"])


def test_render_comment_template_contains_expected_fields(manager):
    rendered = manager._render_comment(
        "disabled {record_name} in {zone} at {timestamp} ({account}/{account_id})",
        account="prod",
        account_id="accid",
        zone="example.com",
        zone_id="zid",
        record_name="api.example.com",
        record_id="rid",
    )
    assert "api.example.com" in rendered
    assert "example.com" in rendered
    assert "prod/accid" in rendered
    assert "disabled" in rendered
