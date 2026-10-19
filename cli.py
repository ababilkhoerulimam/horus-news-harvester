"""
Command Line Interface for Indonesian Sports Scrapper.
Usage:
    python cli.py doctor
    python cli.py search --query "Dewa United" --sport football --limit 5
    python cli.py extract --url "https://www.antaranews.com/..."
"""

import argparse
import json
import sys

from doctor import run_doctor, format_doctor_report
from channels import get_channel, get_all_channels


def main():
    parser = argparse.ArgumentParser(description="Indonesian Sports Multi-Channel Harvester")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # doctor command
    doc_parser = subparsers.add_parser("doctor", help="Run health check on channels & dependencies")
    doc_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    # search command
    search_parser = subparsers.add_parser("search", help="Search sports articles across channels")
    search_parser.add_argument("--query", "-q", required=True, help="Search keyword")
    search_parser.add_argument("--sport", "-s", default="football", choices=["football", "basketball", "badminton", "volleyball"])
    search_parser.add_argument("--limit", "-l", type=int, default=10, help="Max results")
    search_parser.add_argument("--channel", "-c", default="portal_news", help="Channel identifier")

    # extract command
    extract_parser = subparsers.add_parser("extract", help="Extract cleaned content from URL")
    extract_parser.add_argument("--url", "-u", required=True, help="Target article URL")
    extract_parser.add_argument("--channel", "-c", default="portal_news", help="Channel identifier")

    # sync-firefox-twitter command
    subparsers.add_parser("sync-firefox-twitter", help="Auto-extract Twitter session from local Firefox profile")

    # set-twitter-session command
    session_parser = subparsers.add_parser("set-twitter-session", help="Manually save Twitter auth_token and ct0 tokens")
    session_parser.add_argument("--auth-token", required=True, help="Twitter auth_token cookie")
    session_parser.add_argument("--ct0", required=True, help="Twitter ct0 CSRF cookie")

    args = parser.parse_args()

    if args.command == "doctor":
        res = run_doctor()
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(format_doctor_report(res))

    elif args.command == "search":
        ch = get_channel(args.channel)
        if not ch:
            print(f"[!] Error: Channel '{args.channel}' not found.")
            sys.exit(1)

        results = ch.search(query=args.query, sport=args.sport, limit=args.limit)
        print(f"[*] Found {len(results)} articles for '{args.query}' ({args.sport}):\n")
        for i, item in enumerate(results, 1):
            print(f"{i}. [{item.get('source')}] {item.get('title')}")
            print(f"   Date: {item.get('published_date')}")
            print(f"   URL:  {item.get('url')}\n")

    elif args.command == "extract":
        ch = get_channel(args.channel)
        if not ch:
            print(f"[!] Error: Channel '{args.channel}' not found.")
            sys.exit(1)

        result = ch.extract(args.url)
        if result.get("status") == "success":
            print(f"[*] Successfully extracted via {result.get('backend')}:\n")
            print(result.get("content")[:1000])
            if len(result.get("content", "")) > 1000:
                print("\n... [truncated] ...")
        else:
            print(f"[!] Extraction failed: {result.get('error', 'Unknown error')}")

    elif args.command == "sync-firefox-twitter":
        from backends.firefox_cookies import extract_firefox_twitter_tokens, save_twitter_session
        print("[*] Scanning Firefox profiles for active Twitter session...")
        tokens = extract_firefox_twitter_tokens()
        if tokens:
            save_twitter_session(tokens)
            print(f"[+] Extracted active session from Firefox profile '{tokens.get('profile')}'.")
            print("[+] Saved to config/twitter_session.json. GraphQL backend is now active!")
        else:
            print("[!] No active Twitter session found in Firefox cookies.")
            print("    Make sure you have logged into x.com in Firefox.")

    elif args.command == "set-twitter-session":
        from backends.firefox_cookies import save_twitter_session
        tokens = {
            "auth_token": args.auth_token,
            "ct0": args.ct0,
            "profile": "manual_input"
        }
        save_twitter_session(tokens)
        print("[+] Manually saved Twitter session to config/twitter_session.json.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
