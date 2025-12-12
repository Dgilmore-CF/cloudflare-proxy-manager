#!/usr/bin/env python3
"""
Cloudflare Proxy Manager

A script to manage Cloudflare proxy settings across multiple accounts.
Can disable/enable proxy for all hostnames and maintain state between runs.
"""
import os
import json
import logging
import csv
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cloudflare
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress
from python_json_logger import JsonFormatter

# Initialize console for rich output
console = Console()

# Setup logging
LOG_FILE = "cloudflare_proxy_manager.log"

class CloudflareProxyManager:
    """Manages Cloudflare proxy settings across multiple accounts."""
    
    def __init__(self, state_file: str = "proxy_state.json"):
        """Initialize the Cloudflare Proxy Manager.
        
        Args:
            state_file: Path to the JSON file storing proxy states
        """
        self.state_file = Path(state_file)
        self.state = self._load_state()
        self.setup_logging()
        self.accounts = self._load_accounts()
        
    def setup_logging(self):
        """Configure logging with both file and console output."""
        # Create logs directory if it doesn't exist
        Path("logs").mkdir(exist_ok=True)
        
        # File handler with JSON formatter
        file_handler = logging.FileHandler(f"logs/{LOG_FILE}")
        file_handler.setFormatter(JsonFormatter("%(asctime)s %(levelname)s %(message)s"))
        
        # Console handler with rich formatting
        console_handler = RichHandler(console=console, rich_tracebacks=True)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        
        # Configure root logger
        logging.basicConfig(
            level=logging.INFO,
            handlers=[file_handler, console_handler],
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        self.logger = logging.getLogger(__name__)
    
    def _load_accounts(self) -> Dict[str, Dict]:
        """Load Cloudflare accounts from environment variables."""
        accounts = {}
        for key, value in os.environ.items():
            if key.startswith("CLOUDFLARE_API_TOKEN_"):
                account_name = key.replace("CLOUDFLARE_API_TOKEN_", "").lower()
                accounts[account_name] = {"token": value}
                
                # Check for corresponding account ID
                account_id_key = f"CLOUDFLARE_ACCOUNT_ID_{account_name.upper()}"
                if account_id_key in os.environ:
                    accounts[account_name]["account_id"] = os.environ[account_id_key]
                    self.logger.info(f"Loaded account '{account_name}' with account ID: {os.environ[account_id_key]}")
                else:
                    self.logger.warning(f"Account '{account_name}' has no account ID specified. Will retrieve all zones accessible by token.")
        
        if not accounts:
            self.logger.error("No Cloudflare API tokens found in environment variables")
            console.print("[red]Error:[/] No Cloudflare API tokens found. Please set CLOUDFLARE_API_TOKEN_* environment variables.")
            exit(1)
            
        return accounts
    
    def _load_state(self) -> Dict:
        """Load the proxy state from the state file."""
        if self.state_file.exists():
            with open(self.state_file, 'r') as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    self.logger.warning("State file is corrupted, starting with empty state")
        return {"version": 1, "accounts": {}, "last_updated": datetime.utcnow().isoformat()}
    
    def _save_state(self):
        """Save the current proxy state to the state file."""
        self.state["last_updated"] = datetime.utcnow().isoformat()
        with open(self.state_file, 'w') as f:
            json.dump(self.state, f, indent=2)
    
    def _get_cloudflare_client(self, account_name: str) -> cloudflare.CloudFlare:
        """Create a Cloudflare client for the specified account."""
        if account_name not in self.accounts:
            raise ValueError(f"Account {account_name} not found")
            
        return cloudflare.CloudFlare(token=self.accounts[account_name]["token"])
    
    def _write_report_files(self, report_dir: Path, results: Dict[str, Any], prefix: str) -> None:
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

        report_json = report_dir / f"{prefix}_{ts}.json"
        with open(report_json, "w") as f:
            json.dump(results, f, indent=2)

        changes = results.get("changes", [])
        if changes:
            report_csv = report_dir / f"{prefix}_{ts}.csv"
            fieldnames = sorted({k for row in changes for k in row.keys()})
            with open(report_csv, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(changes)

        report_md = report_dir / f"{prefix}_{ts}.md"
        with open(report_md, "w") as f:
            f.write(f"# Cloudflare Proxy Manager Report\n\n")
            f.write(f"Generated: {datetime.utcnow().isoformat()}Z\n\n")
            f.write(f"Action: {prefix}\n\n")
            f.write(f"Total changes: {results.get('total_changes', results.get('total_restored', 0))}\n\n")
            f.write("## Accounts\n\n")
            for account_name, account_result in results.get("accounts", {}).items():
                f.write(f"- {account_name}: {json.dumps(account_result)}\n")
            if changes:
                f.write("\n## First 50 Changes\n\n")
                for row in changes[:50]:
                    f.write(f"- {row.get('record_name')} ({row.get('record_type')}) in {row.get('zone')} [{row.get('action')}]\n")

    def _cf_call(self, fn, *, max_retries: int = 5, base_sleep_seconds: float = 1.0):
        attempt = 0
        while True:
            try:
                return fn()
            except Exception as e:
                attempt += 1
                message = str(e)

                retryable = False
                status_code = getattr(e, "code", None)
                if status_code in (429, 500, 502, 503, 504):
                    retryable = True
                if any(s in message for s in ("429", "rate limit", "timeout", "timed out", "connection")):
                    retryable = True

                if not retryable or attempt > max_retries:
                    raise

                sleep_for = base_sleep_seconds * (2 ** (attempt - 1))
                self.logger.warning(
                    f"Retrying Cloudflare API call after error (attempt {attempt}/{max_retries}): {message}",
                    extra={"attempt": attempt, "max_retries": max_retries}
                )
                time.sleep(min(sleep_for, 30.0))

    def _paginate_get(self, getter, *, params: Optional[Dict[str, Any]] = None, per_page: int = 1000, max_retries: int = 5) -> List[Dict[str, Any]]:
        params = dict(params or {})
        page = 1
        out: List[Dict[str, Any]] = []

        while True:
            page_params = dict(params)
            page_params.update({"page": page, "per_page": per_page})

            resp = self._cf_call(lambda: getter(page_params), max_retries=max_retries)

            if isinstance(resp, dict) and "result" in resp:
                items = resp.get("result") or []
                out.extend(items)
                info = resp.get("result_info") or {}
                total_pages = info.get("total_pages")
                if total_pages is not None and page >= int(total_pages):
                    break
                if not items:
                    break
                page += 1
                continue

            if isinstance(resp, list):
                out.extend(resp)
                if len(resp) < per_page:
                    break
                page += 1
                continue

            break

        return out

    def _matches_name_filters(self, name: str, include: Optional[str], exclude: Optional[str]) -> bool:
        if include:
            if not re.search(include, name):
                return False
        if exclude:
            if re.search(exclude, name):
                return False
        return True

    def _matches_tag_filters(self, fields: Dict[str, Any], tags: Optional[List[str]], tag_fields: Optional[List[str]]) -> bool:
        if not tags:
            return True

        tag_fields = tag_fields or ["name", "content"]
        haystacks: List[str] = []
        for f in tag_fields:
            v = fields.get(f)
            if v is None:
                continue
            haystacks.append(str(v))

        if not haystacks:
            return False

        combined = "\n".join(haystacks).lower()
        return any(t.lower() in combined for t in tags)

    def _render_comment(self, template: str, *, account: str, account_id: str, zone: str, zone_id: str, record_name: str, record_id: str) -> str:
        ts = datetime.utcnow().isoformat() + "Z"
        return template.format(
            timestamp=ts,
            account=account,
            account_id=account_id,
            zone=zone,
            zone_id=zone_id,
            record_name=record_name,
            record_id=record_id,
        )
    
    def verify_account(self, account_name: str) -> Dict:
        """Verify account access and return account information."""
        cf = self._get_cloudflare_client(account_name)
        try:
            # Get user info to verify token
            user = self._cf_call(lambda: cf.user.get())
            
            # Get accounts accessible by this token
            accounts = self._cf_call(lambda: cf.accounts.get())
            
            account_info = {
                "account_name": account_name,
                "token_email": user.get("email"),
                "token_id": user.get("id"),
                "accessible_accounts": []
            }
            
            for acc in accounts:
                account_info["accessible_accounts"].append({
                    "id": acc["id"],
                    "name": acc["name"],
                    "type": acc.get("type", "Unknown")
                })
            
            # Check if configured account ID matches accessible accounts
            if "account_id" in self.accounts[account_name]:
                configured_id = self.accounts[account_name]["account_id"]
                account_info["configured_account_id"] = configured_id
                
                if any(acc["id"] == configured_id for acc in accounts):
                    account_info["account_id_valid"] = True
                else:
                    account_info["account_id_valid"] = False
                    self.logger.warning(
                        f"Configured account ID {configured_id} not found in accessible accounts for {account_name}"
                    )
            
            return account_info
            
        except Exception as e:
            self.logger.error(f"Error verifying account {account_name}: {str(e)}")
            return {"error": str(e)}
    
    def get_zones(self, account_name: str) -> List[Dict]:
        """Get all zones for an account, filtered by account ID if configured."""
        cf = self._get_cloudflare_client(account_name)
        try:
            # Check if account has a configured account ID
            if "account_id" in self.accounts[account_name]:
                account_id = self.accounts[account_name]["account_id"]
                # Filter zones by account ID
                params = {"account.id": account_id}
                zones = self._paginate_get(lambda p: cf.zones.get(params=p), params=params)
                self.logger.info(
                    f"Retrieved {len(zones)} zones for account '{account_name}' (ID: {account_id})"
                )
            else:
                # Get all zones accessible by token
                zones = self._paginate_get(lambda p: cf.zones.get(params=p))
                self.logger.warning(
                    f"Retrieved {len(zones)} zones for account '{account_name}' (no account ID filter applied)"
                )
            
            return zones
        except Exception as e:
            self.logger.error(f"Error fetching zones for account {account_name}: {str(e)}")
            return []
    
    def get_dns_records(self, account_name: str, zone_id: str) -> List[Dict]:
        """Get all DNS records for a zone."""
        cf = self._get_cloudflare_client(account_name)
        try:
            return self._paginate_get(lambda p: cf.zones.dns_records.get(zone_id, params=p))
        except Exception as e:
            self.logger.error(f"Error fetching DNS records for zone {zone_id}: {str(e)}")
            return []
    
    def update_dns_record_proxy_status(
        self, 
        account_name: str, 
        zone_id: str, 
        record_id: str, 
        proxied: bool,
        comment: Optional[str] = None,
    ) -> bool:
        """Update the proxy status of a DNS record."""
        cf = self._get_cloudflare_client(account_name)
        try:
            record = self._cf_call(lambda: cf.zones.dns_records.get(zone_id, record_id))

            changed = False
            if record.get("proxied") != proxied:
                record["proxied"] = proxied
                changed = True

            if comment is not None and record.get("comment") != comment:
                record["comment"] = comment
                changed = True

            if changed:
                self._cf_call(lambda: cf.zones.dns_records.put(zone_id, record_id, data=record))
            return changed
        except Exception as e:
            self.logger.error(f"Error updating record {record_id} in zone {zone_id}: {str(e)}")
            return False
    
    def scan_and_disable_proxies(
        self,
        dry_run: bool = False,
        selected_accounts: Optional[List[str]] = None,
        selected_zones: Optional[List[str]] = None,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        tags: Optional[List[str]] = None,
        tag_fields: Optional[List[str]] = None,
        comment_on_disable: Optional[str] = None,
    ) -> Dict:
        """Scan all zones and disable proxies, saving the original state."""
        results: Dict[str, Any] = {"accounts": {}, "total_changes": 0, "dry_run": dry_run, "changes": []}
        
        for account_name in self.accounts:
            if selected_accounts and account_name not in selected_accounts:
                continue
            account_results = {"zones_processed": 0, "records_processed": 0, "records_modified": 0, "errors": 0}
            results["accounts"][account_name] = account_results
            
            console.print(f"\n[bold]Processing account:[/] {account_name}")
            
            # Log account ID if configured
            if "account_id" in self.accounts[account_name]:
                account_id = self.accounts[account_name]["account_id"]
                console.print(f"[dim]Account ID: {account_id}[/]")
            
            zones = self.get_zones(account_name)
            if selected_zones:
                zones = [z for z in zones if z.get("name") in selected_zones or z.get("id") in selected_zones]
            
            with Progress() as progress:
                task = progress.add_task(f"Scanning {len(zones)} zones...", total=len(zones))
                
                for zone in zones:
                    zone_id = zone["id"]
                    zone_name = zone["name"]
                    account_results["zones_processed"] += 1
                    
                    # Initialize zone in state if not exists
                    if zone_id not in self.state.setdefault("accounts", {}).setdefault(account_name, {}):
                        self.state["accounts"][account_name][zone_id] = {"zone_name": zone_name, "records": {}}
                    
                    # Get DNS records
                    records = self.get_dns_records(account_name, zone_id)
                    
                    for record in records:
                        if record["type"] not in ["A", "AAAA", "CNAME"]:
                            continue
                            
                        record_id = record["id"]
                        record_name = record["name"]
                        if not self._matches_name_filters(record_name, include, exclude):
                            continue

                        if not self._matches_tag_filters(
                            {"name": record_name, "content": record.get("content"), "comment": record.get("comment")},
                            tags,
                            tag_fields,
                        ):
                            continue
                        account_results["records_processed"] += 1
                        
                        # Save original state if not already saved
                        record_state = self.state["accounts"][account_name][zone_id]["records"].setdefault(
                            record_id,
                            {
                                "name": record_name,
                                "type": record["type"],
                                "content": record["content"],
                                "proxied": record.get("proxied", False),
                                "comment": record.get("comment"),
                                "modified": False
                            }
                        )
                        
                        # Only process if proxy is enabled and not already modified
                        if record.get("proxied") and not record_state["modified"]:
                            desired_comment = None
                            comment_changed = False
                            if comment_on_disable is not None:
                                desired_comment = self._render_comment(
                                    comment_on_disable,
                                    account=account_name,
                                    account_id=self.accounts[account_name].get("account_id", "N/A"),
                                    zone=zone_name,
                                    zone_id=zone_id,
                                    record_name=record_name,
                                    record_id=record_id,
                                )
                                if record_state.get("comment") != desired_comment:
                                    record_state["comment_modified"] = True
                                    record_state["comment_after"] = desired_comment
                                    comment_changed = True

                            if not dry_run:
                                success = self.update_dns_record_proxy_status(
                                    account_name, zone_id, record_id, False, comment=desired_comment
                                )
                                if success:
                                    record_state["modified"] = True
                                    account_results["records_modified"] += 1
                                    results["total_changes"] += 1
                                    results["changes"].append({
                                        "action": "disable_proxy",
                                        "account": account_name,
                                        "account_id": self.accounts[account_name].get("account_id", "N/A"),
                                        "zone": zone_name,
                                        "zone_id": zone_id,
                                        "record_id": record_id,
                                        "record_name": record_name,
                                        "record_type": record["type"],
                                        "content": record["content"],
                                        "proxied_before": True,
                                        "proxied_after": False,
                                        "comment_before": record_state.get("comment"),
                                        "comment_after": desired_comment,
                                        "timestamp": datetime.utcnow().isoformat() + "Z",
                                    })
                                    self.logger.info(
                                        f"Disabled proxy for {record_name} ({record['type']} {record['content']})",
                                        extra={
                                            "account": account_name,
                                            "account_id": self.accounts[account_name].get("account_id", "N/A"),
                                            "zone": zone_name,
                                            "zone_id": zone_id,
                                            "record_type": record["type"],
                                            "record_name": record_name,
                                            "action": "disable_proxy"
                                        }
                                    )
                            else:
                                # In dry run mode, just count the potential changes
                                account_results["records_modified"] += 1
                                results["total_changes"] += 1
                                results["changes"].append({
                                    "action": "would_disable_proxy",
                                    "account": account_name,
                                    "account_id": self.accounts[account_name].get("account_id", "N/A"),
                                    "zone": zone_name,
                                    "zone_id": zone_id,
                                    "record_id": record_id,
                                    "record_name": record_name,
                                    "record_type": record["type"],
                                    "content": record["content"],
                                    "proxied_before": True,
                                    "proxied_after": False,
                                    "comment_before": record_state.get("comment"),
                                    "comment_after": desired_comment,
                                    "timestamp": datetime.utcnow().isoformat() + "Z",
                                })
                                self.logger.info(
                                    f"[DRY RUN] Would disable proxy for {record_name} ({record['type']} {record['content']})",
                                    extra={
                                        "account": account_name,
                                        "account_id": self.accounts[account_name].get("account_id", "N/A"),
                                        "zone": zone_name,
                                        "zone_id": zone_id,
                                        "record_type": record["type"],
                                        "record_name": record_name,
                                        "action": "would_disable_proxy"
                                    }
                                )
                    
                    progress.update(task, advance=1)
            
            # Save state after processing each account
            if not dry_run:
                self._save_state()
        
        return results
    
    def restore_proxies(
        self,
        dry_run: bool = False,
        selected_accounts: Optional[List[str]] = None,
        selected_zones: Optional[List[str]] = None,
        include: Optional[str] = None,
        exclude: Optional[str] = None,
        tags: Optional[List[str]] = None,
        tag_fields: Optional[List[str]] = None,
    ) -> Dict:
        """Restore proxies based on saved state."""
        if not self.state_file.exists():
            self.logger.error("No state file found. Cannot restore proxies.")
            return {"error": "No state file found. Cannot restore proxies."}
        
        results: Dict[str, Any] = {"accounts": {}, "total_restored": 0, "dry_run": dry_run, "changes": []}
        
        for account_name, account_data in self.state.get("accounts", {}).items():
            if selected_accounts and account_name not in selected_accounts:
                continue
            if account_name not in self.accounts:
                self.logger.warning(f"Account {account_name} not found in current configuration. Skipping.")
                continue
                
            account_results = {"zones_processed": 0, "records_restored": 0, "errors": 0}
            results["accounts"][account_name] = account_results
            
            console.print(f"\n[bold]Processing account:[/] {account_name}")
            
            # Log account ID if configured
            if "account_id" in self.accounts.get(account_name, {}):
                account_id = self.accounts[account_name]["account_id"]
                console.print(f"[dim]Account ID: {account_id}[/]")
            
            with Progress() as progress:
                zones = list(account_data.items())
                task = progress.add_task(f"Restoring proxies in {len(zones)} zones...", total=len(zones))
                
                for zone_id, zone_data in zones:
                    if zone_id == "zone_name":  # Skip metadata
                        continue

                    if selected_zones and (zone_data.get("zone_name") not in selected_zones and zone_id not in selected_zones):
                        progress.update(task, advance=1)
                        continue
                        
                    zone_name = zone_data["zone_name"]
                    account_results["zones_processed"] += 1
                    
                    for record_id, record_data in zone_data.get("records", {}).items():
                        record_name = record_data.get("name", "")
                        if not self._matches_name_filters(record_name, include, exclude):
                            continue

                        if not self._matches_tag_filters(
                            {"name": record_name, "content": record_data.get("content"), "comment": record_data.get("comment")},
                            tags,
                            tag_fields,
                        ):
                            continue
                        if record_data.get("modified") and record_data.get("proxied"):
                            desired_comment = None
                            if restore_original_comment and record_data.get("comment_modified"):
                                desired_comment = record_data.get("comment")
                            if not dry_run:
                                try:
                                    success = self.update_dns_record_proxy_status(
                                        account_name, zone_id, record_id, True, comment=desired_comment
                                    )
                                    if success:
                                        record_data["modified"] = False
                                        account_results["records_restored"] += 1
                                        results["total_restored"] += 1
                                        if restore_original_comment and record_data.get("comment_modified"):
                                            record_data["comment_modified"] = False
                                            record_data.pop("comment_after", None)
                                        results["changes"].append({
                                            "action": "restore_proxy",
                                            "account": account_name,
                                            "account_id": self.accounts.get(account_name, {}).get("account_id", "N/A"),
                                            "zone": zone_name,
                                            "zone_id": zone_id,
                                            "record_id": record_id,
                                            "record_name": record_data.get("name"),
                                            "record_type": record_data.get("type"),
                                            "content": record_data.get("content"),
                                            "proxied_before": False,
                                            "proxied_after": True,
                                            "comment_before": record_data.get("comment_after"),
                                            "comment_after": desired_comment,
                                            "timestamp": datetime.utcnow().isoformat() + "Z",
                                        })
                                        self.logger.info(
                                            f"Restored proxy for {record_data['name']} ({record_data['type']} {record_data['content']})",
                                            extra={
                                                "account": account_name,
                                                "account_id": self.accounts.get(account_name, {}).get("account_id", "N/A"),
                                                "zone": zone_name,
                                                "zone_id": zone_id,
                                                "record_type": record_data["type"],
                                                "record_name": record_data["name"],
                                                "action": "restore_proxy"
                                            }
                                        )
                                except Exception as e:
                                    account_results["errors"] += 1
                                    self.logger.error(
                                        f"Error restoring proxy for {record_data['name']}: {str(e)}",
                                        extra={
                                            "account": account_name,
                                            "zone": zone_name,
                                            "record_id": record_id,
                                            "error": str(e)
                                        }
                                    )
                            else:
                                # Dry run mode
                                account_results["records_restored"] += 1
                                results["total_restored"] += 1
                                results["changes"].append({
                                    "action": "would_restore_proxy",
                                    "account": account_name,
                                    "account_id": self.accounts.get(account_name, {}).get("account_id", "N/A"),
                                    "zone": zone_name,
                                    "zone_id": zone_id,
                                    "record_id": record_id,
                                    "record_name": record_data.get("name"),
                                    "record_type": record_data.get("type"),
                                    "content": record_data.get("content"),
                                    "proxied_before": False,
                                    "proxied_after": True,
                                    "comment_before": record_data.get("comment_after"),
                                    "comment_after": desired_comment,
                                    "timestamp": datetime.utcnow().isoformat() + "Z",
                                })
                                self.logger.info(
                                    f"[DRY RUN] Would restore proxy for {record_data['name']} ({record_data['type']} {record_data['content']})",
                                    extra={
                                        "account": account_name,
                                        "account_id": self.accounts.get(account_name, {}).get("account_id", "N/A"),
                                        "zone": zone_name,
                                        "zone_id": zone_id,
                                        "record_type": record_data["type"],
                                        "record_name": record_data["name"],
                                        "action": "would_restore_proxy"
                                    }
                                )
                    
                    progress.update(task, advance=1)
            
            # Save state after processing each account
            if not dry_run:
                self._save_state()
        
        return results

def main():
    """Main entry point for the script."""
    import argparse
    
    # Load environment variables from .env file if it exists
    load_dotenv()
    
    # Set up argument parser
    parser = argparse.ArgumentParser(description="Manage Cloudflare proxy settings across multiple accounts")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Command to execute")
    
    def _split_multi(v: Optional[str]) -> Optional[List[str]]:
        if not v:
            return None
        parts = [p.strip() for p in v.split(",") if p.strip()]
        return parts or None

    def _split_tags(v: Optional[str]) -> Optional[List[str]]:
        return _split_multi(v)

    parser.add_argument("--report-dir", default="reports", help="Directory to write audit reports")
    parser.add_argument("--require-account-id", action="store_true", help="Fail if any configured account is missing an account ID")
    parser.add_argument("--account", default=None, help="Comma-separated account names to target")
    parser.add_argument("--zone", default=None, help="Comma-separated zone names or IDs to target")
    parser.add_argument("--include", default=None, help="Regex: only record names matching this are included")
    parser.add_argument("--exclude", default=None, help="Regex: record names matching this are excluded")
    parser.add_argument("--tags", default=None, help="Comma-separated tag strings to match against DNS record fields")
    parser.add_argument("--tag-fields", default="name,content", help="Comma-separated fields to match tags against (name,content,comment)")
    parser.add_argument(
        "--comment-on-disable",
        default=None,
        help="Set DNS record comment when disabling proxy. Template supports {timestamp},{account},{account_id},{zone},{zone_id},{record_name},{record_id}",
    )
    parser.add_argument(
        "--restore-original-comment",
        action="store_true",
        help="When restoring proxy, restore the original DNS record comment if it was modified by this script",
    )

    # Disable subcommand
    disable_parser = subparsers.add_parser("disable", help="Disable proxies for all hostnames")
    disable_parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without making changes")
    
    # Restore subcommand
    restore_parser = subparsers.add_parser("restore", help="Restore proxies based on saved state")
    restore_parser.add_argument("--dry-run", action="store_true", help="Perform a dry run without making changes")
    
    # Status subcommand
    status_parser = subparsers.add_parser("status", help="Show current proxy status")
    
    # Verify subcommand
    verify_parser = subparsers.add_parser("verify", help="Verify account access and configuration")
    
    args = parser.parse_args()
    
    # Initialize the manager
    manager = CloudflareProxyManager()

    selected_accounts = _split_multi(getattr(args, "account", None))
    selected_zones = _split_multi(getattr(args, "zone", None))
    include = getattr(args, "include", None)
    exclude = getattr(args, "exclude", None)
    tags = _split_tags(getattr(args, "tags", None))
    tag_fields = _split_multi(getattr(args, "tag_fields", "name,content"))
    report_dir = Path(getattr(args, "report_dir", "reports"))
    comment_on_disable = getattr(args, "comment_on_disable", None)
    restore_original_comment = bool(getattr(args, "restore_original_comment", False))

    if getattr(args, "require_account_id", False):
        missing = [name for name, cfg in manager.accounts.items() if "account_id" not in cfg]
        if missing:
            console.print(f"[red]Error:[/] Missing account IDs for: {', '.join(missing)}")
            raise SystemExit(2)
    
    if args.command == "disable":
        console.print("[bold blue]Scanning and disabling proxies...[/]")
        if args.dry_run:
            console.print("[yellow]Dry run mode - no changes will be made[/]")
        
        results = manager.scan_and_disable_proxies(
            dry_run=args.dry_run,
            selected_accounts=selected_accounts,
            selected_zones=selected_zones,
            include=include,
            exclude=exclude,
            tags=tags,
            tag_fields=tag_fields,
            comment_on_disable=comment_on_disable,
        )
        manager._write_report_files(report_dir, results, "disable")
        
        console.print("\n[bold]Summary:[/]")
        console.print(f"Total records that would be modified: [bold]{results['total_changes']}[/]")
        if not args.dry_run:
            console.print("[green]✓ Changes have been applied[/]")
        
    elif args.command == "restore":
        console.print("[bold blue]Restoring proxies from saved state...[/]")
        if args.dry_run:
            console.print("[yellow]Dry run mode - no changes will be made[/]")
        
        results = manager.restore_proxies(
            dry_run=args.dry_run,
            selected_accounts=selected_accounts,
            selected_zones=selected_zones,
            include=include,
            exclude=exclude,
            tags=tags,
            tag_fields=tag_fields,
            restore_original_comment=restore_original_comment,
        )
        if "error" not in results:
            manager._write_report_files(report_dir, results, "restore")
        
        if "error" in results:
            console.print(f"[red]Error:[/] {results['error']}")
        else:
            console.print("\n[bold]Summary:[/]")
            console.print(f"Total records that would be restored: [bold]{results['total_restored']}[/]")
            if not args.dry_run:
                console.print("[green]✓ Changes have been applied[/]")
    
    elif args.command == "status":
        if not manager.state_file.exists():
            console.print("[yellow]No state file found. Run 'disable' command first to create a state file.[/]")
            return
        
        state = manager._load_state()
        console.print("[bold]Last updated:[/]", state.get("last_updated", "Unknown"))
        
        total_records = 0
        modified_records = 0
        
        for account_name, account_data in state.get("accounts", {}).items():
            console.print(f"\n[bold]Account:[/] {account_name}")
            
            for zone_id, zone_data in account_data.items():
                if zone_id == "zone_name":  # Skip metadata
                    continue
                    
                zone_name = zone_data.get("zone_name", "Unknown")
                console.print(f"  [bold]Zone:[/] {zone_name} ({zone_id})")
                
                for record_id, record_data in zone_data.get("records", {}).items():
                    total_records += 1
                    status = "[green]active" if not record_data.get("modified") else "[yellow]modified"
                    console.print(
                        f"    {record_data.get('name', 'Unknown')} ({record_data.get('type', '?')}): {status}[/]"
                    )
                    if record_data.get("modified"):
                        modified_records += 1
        
        console.print(f"\n[bold]Total records:[/] {total_records}")
        console.print(f"[bold]Modified records:[/] {modified_records}")
    
    elif args.command == "verify":
        console.print("[bold blue]Verifying account configurations...[/]\n")
        
        for account_name in manager.accounts:
            console.print(f"[bold]Account:[/] {account_name}")
            account_info = manager.verify_account(account_name)
            
            if "error" in account_info:
                console.print(f"  [red]✗ Error:[/] {account_info['error']}")
                continue
            
            console.print(f"  [green]✓[/] Token email: {account_info.get('token_email', 'Unknown')}")
            console.print(f"  [green]✓[/] Token ID: {account_info.get('token_id', 'Unknown')}")
            
            if "configured_account_id" in account_info:
                if account_info.get("account_id_valid"):
                    console.print(f"  [green]✓[/] Configured account ID: {account_info['configured_account_id']} (Valid)")
                else:
                    console.print(f"  [red]✗[/] Configured account ID: {account_info['configured_account_id']} (Not found in accessible accounts)")
            else:
                console.print(f"  [yellow]![/] No account ID configured - will retrieve all zones accessible by token")
            
            console.print(f"\n  [bold]Accessible Cloudflare Accounts:[/]")
            for acc in account_info.get("accessible_accounts", []):
                console.print(f"    • {acc['name']} (ID: {acc['id']}, Type: {acc['type']})")
            
            console.print()

if __name__ == "__main__":
    main()
