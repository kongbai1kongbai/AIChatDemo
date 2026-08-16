"""
Create an appchat group for customer_service forwarding.

Usage:
  python create_group.py --config /path/to/customer_service_config.json
  python create_group.py --config /path/to/customer_service_config.json --chatid my_cs_forward --users user1,user2,user3

The created chatid can then be set as forward_chatid in customer_service_config.json.
"""
import json
import os
import sys
import argparse
import requests

_CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from shared.config import load_model_config, CS_CONFIG_PATH

_QYAPI = "https://qyapi.weixin.qq.com/cgi-bin"


def main():
    parser = argparse.ArgumentParser(description="Create appchat group for CS forwarding")
    parser.add_argument("--config", default=CS_CONFIG_PATH)
    parser.add_argument("--chatid", default="cs_forward", help="Custom chatid for the group")
    parser.add_argument("--name", default="CS转发群", help="Group display name")
    parser.add_argument("--users", default="", help="Comma-separated user IDs to add (required)")
    parser.add_argument("--list-users", action="store_true", help="List enterprise users first")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cs_config = json.load(f)

    corp_id = cs_config["corp_id"]
    corp_secret = cs_config["corp_secret"]

    # Get access token
    token_resp = requests.get(
        f"{_QYAPI}/gettoken?corpid={corp_id}&corpsecret={corp_secret}",
        timeout=10,
    ).json()
    if token_resp.get("errcode", 0) != 0:
        print(f"[FAIL] gettoken: {token_resp}")
        sys.exit(1)
    token = token_resp["access_token"]
    print(f"[OK] access_token obtained")

    # List users if requested
    if args.list_users:
        print(f"\n--- Enterprise Departments & Users ---")
        # Get department list
        dept_resp = requests.get(
            f"{_QYAPI}/department/list?access_token={token}",
            timeout=10,
        ).json()
        departments = dept_resp.get("department", [])
        for dept in departments:
            print(f"\n  Department: {dept['name']} (id={dept['id']})")
            # Get users in department
            users_resp = requests.get(
                f"{_QYAPI}/user/list?access_token={token}&department_id={dept['id']}",
                timeout=10,
            ).json()
            for u in users_resp.get("userlist", []):
                print(f"    userid={u['userid']}  name={u['name']}")
        return

    # Create group
    if not args.users:
        print(f"\n[ERROR] --users is required. Use --list-users to see available user IDs first.")
        print(f"\nExample:")
        print(f"  python create_group.py --config {args.config} --list-users")
        print(f"  python create_group.py --config {args.config} --chatid cs_forward --name \"AI自助科研小组-CS\" --users zhoulei,chenzhima")
        sys.exit(1)

    user_list = [u.strip() for u in args.users.split(",") if u.strip()]

    payload = {
        "chatid": args.chatid,
        "name": args.name,
        "userlist": user_list,
    }

    print(f"\nCreating appchat group:")
    print(f"  chatid: {args.chatid}")
    print(f"  name:   {args.name}")
    print(f"  users:  {user_list}")

    resp = requests.post(
        f"{_QYAPI}/appchat/create?access_token={token}",
        json=payload,
        timeout=10,
    ).json()

    if resp.get("errcode", 0) == 0:
        chatid = resp.get("chatid", args.chatid)
        print(f"\n[OK] Group created! chatid = {chatid}")
        print(f"\nAdd this to your customer_service_config.json:")
        print(f'  "forward_chatid": "{chatid}"')
    else:
        print(f"\n[FAIL] {resp}")
        if resp.get("errcode") == 60011:
            print("  -> chatid already exists. Use a different --chatid or use appchat/update to add members.")
        elif resp.get("errcode") == 60110:
            print("  -> user not found. Check user IDs with --list-users.")


if __name__ == "__main__":
    main()
