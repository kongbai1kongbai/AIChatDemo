"""
Diagnostic script for WeChat Customer Service module.
Tests API connectivity step by step to pinpoint issues.

Usage:
  python diagnose.py
  python diagnose.py --config /path/to/customer_service_config.json
"""
import json
import os
import sys
import argparse
import requests

# Add project root to path
_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from shared.config import load_model_config, CS_CONFIG_PATH

_QYAPI = "https://qyapi.weixin.qq.com/cgi-bin"


def get_access_token(corp_id, corp_secret):
    """Get access token from WeCom API."""
    url = f"{_QYAPI}/gettoken?corpid={corp_id}&corpsecret={corp_secret}"
    resp = requests.get(url, timeout=10)
    data = resp.json()
    if data.get("errcode", 0) != 0:
        print(f"[FAIL] gettoken: {data}")
        return None
    print(f"[OK]   access_token obtained (expires in {data.get('expires_in', '?')}s)")
    return data["access_token"]


def test_kf_account_list(token):
    """Test kf/account/list API."""
    url = f"{_QYAPI}/kf/account/list?access_token={token}"
    resp = requests.get(url, timeout=10)
    data = resp.json()
    if data.get("errcode", 0) != 0:
        print(f"[FAIL] kf/account/list: {data}")
        return []
    accounts = data.get("account_list", [])
    print(f"[OK]   kf/account/list: {len(accounts)} account(s)")
    for acc in accounts:
        print(f"       - {acc['open_kfid']}  {acc.get('name', '')}")
    return accounts


def test_kf_sync_msg(token, open_kfid):
    """Test kf/sync_msg API with the given open_kfid."""
    url = f"{_QYAPI}/kf/sync_msg?access_token={token}"
    payload = {
        "cursor": "",
        "open_kfid": open_kfid,
        "limit": 10,
    }
    resp = requests.post(url, json=payload, timeout=10)
    data = resp.json()
    errcode = data.get("errcode", 0)

    if errcode == 0:
        msg_list = data.get("msg_list", [])
        has_more = data.get("has_more", 0)
        print(f"[OK]   kf/sync_msg: {len(msg_list)} message(s), has_more={has_more}")
        for msg in msg_list[:5]:
            mt = msg.get("msgtype", "?")
            origin = msg.get("origin", "?")
            eid = msg.get("external_userid", "?")
            print(f"       - type={mt}, origin={origin}, user={eid[:20]}...")
        return True
    elif errcode == 48007:
        print(f"[FAIL] kf/sync_msg: errcode=48007 (no kfid privilege)")
        print(f"       >>> This means the app is NOT authorized to access KF conversation messages.")
        print(f"       >>> Go to WeCom admin -> 应用管理 -> 自建应用 -> your app")
        print(f"       >>> Find '可调用接口的应用' section and click '前往配置'")
        print(f"       >>> Authorize the KF account '{open_kfid}' for this app")
        return False
    elif errcode == 95000:
        print(f"[FAIL] kf/sync_msg: errcode=95000 (invalid open_kfid)")
        print(f"       >>> The open_kfid '{open_kfid}' is not valid or not found")
        return False
    else:
        print(f"[FAIL] kf/sync_msg: {data}")
        return False


def test_kf_send_msg(token, open_kfid):
    """Test kf/send_msg API (dry run - just check if API is accessible)."""
    # We don't actually send, just check if the endpoint is reachable
    url = f"{_QYAPI}/kf/send_msg?access_token={token}"
    # Send with invalid touser to test API access without actually sending
    payload = {
        "touser": "test_invalid_user",
        "open_kfid": open_kfid,
        "msgid": f"diag_test",
        "msgtype": "text",
        "text": {"content": "diagnostic test"},
    }
    resp = requests.post(url, json=payload, timeout=10)
    data = resp.json()
    errcode = data.get("errcode", 0)

    if errcode == 0:
        print(f"[OK]   kf/send_msg: API accessible (unexpectedly succeeded)")
    elif errcode == 48007:
        print(f"[FAIL] kf/send_msg: errcode=48007 (no kfid privilege)")
    elif errcode == 95004:
        # Invalid external_userid - expected for dry run
        print(f"[OK]   kf/send_msg: API accessible (got expected error for invalid user)")
    else:
        print(f"[INFO] kf/send_msg: errcode={errcode}, errmsg={data.get('errmsg', '')}")


def main():
    parser = argparse.ArgumentParser(description="Customer Service Diagnostic Tool")
    parser.add_argument("--config", default=CS_CONFIG_PATH, help="Path to customer_service_config.json")
    args = parser.parse_args()

    print("=" * 60)
    print("WeChat Customer Service - Diagnostic Tool")
    print("=" * 60)

    # Load config
    if not os.path.exists(args.config):
        print(f"[FAIL] Config not found: {args.config}")
        sys.exit(1)

    with open(args.config, "r", encoding="utf-8") as f:
        cs_config = json.load(f)

    corp_id = cs_config["corp_id"]
    corp_secret = cs_config["corp_secret"]
    open_kfid = cs_config.get("open_kfid", "")

    print(f"\n[INFO] Config: {args.config}")
    print(f"[INFO] Corp ID: {corp_id[:8]}...")
    print(f"[INFO] Port: {cs_config.get('port', 8081)}")
    print(f"[INFO] Configured open_kfid: {open_kfid or '(not set)'}")

    # Step 1: Get access token
    print(f"\n--- Step 1: Access Token ---")
    token = get_access_token(corp_id, corp_secret)
    if not token:
        print("\n>>> Cannot proceed without access token. Check corp_id and corp_secret.")
        sys.exit(1)

    # Step 2: List KF accounts
    print(f"\n--- Step 2: KF Account List ---")
    accounts = test_kf_account_list(token)
    if not accounts:
        print("\n>>> No KF accounts found. Create one in WeCom admin first.")
        sys.exit(1)

    # If open_kfid not configured, use first account
    if not open_kfid:
        open_kfid = accounts[0]["open_kfid"]
        print(f"[INFO] Using first account: {open_kfid}")
    else:
        # Verify configured open_kfid exists
        kfid_list = [a["open_kfid"] for a in accounts]
        if open_kfid in kfid_list:
            print(f"[OK]   Configured open_kfid matches an existing account")
        else:
            print(f"[WARN] Configured open_kfid '{open_kfid}' NOT found in account list!")
            print(f"       Available: {kfid_list}")

    # Step 3: Test sync_msg
    print(f"\n--- Step 3: Sync Messages (open_kfid={open_kfid}) ---")
    sync_ok = test_kf_sync_msg(token, open_kfid)

    # Step 4: Test send_msg
    print(f"\n--- Step 4: Send Message API ---")
    test_kf_send_msg(token, open_kfid)

    # Summary
    print(f"\n{'=' * 60}")
    print("Summary:")
    print(f"  Access Token:   OK")
    print(f"  KF Accounts:    {len(accounts)} found")
    print(f"  sync_msg:       {'OK' if sync_ok else 'FAILED (likely 48007 - need per-app authorization)'}")
    print(f"{'=' * 60}")

    if not sync_ok:
        print("\nMost likely cause: Error 48007")
        print("Fix: WeCom admin -> 应用管理 -> 自建 -> your app -> '前往配置' -> authorize KF account")


if __name__ == "__main__":
    main()
