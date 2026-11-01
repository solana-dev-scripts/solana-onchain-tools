#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tensor_listings_scan.py - takes tensor_candidates.csv (the output of
tensor_discover.py, ALL rows, including ones with no symbol_me) and, for each
collection, asks Tensor directly for real active listings (mint address +
price) via their own GraphQL endpoint (graphql.tensor.trade/graphql, the
ActiveListingsPricesV2 query) - with no dependency on Magic Eden at all.
Based on the captured request, this endpoint does NOT require a cookie/API key.

The listings are then run through the same on-chain validation as
scan_cheap_burns.py (compressed / pNFT / live Master Edition / Token-2022) -
the function is imported directly from there, so there's exactly one copy of
that logic shared by both data sources.

Usage:
    python tensor_listings_scan.py --csv tensor_candidates.csv --max-price 0.006 --min-profit 0.001 --out candidates_tensor.csv

If you get a 403/empty response, this endpoint may be behind Cloudflare too -
in that case, same as with the search endpoint, you'll need --cookie with a
value from your browser.
"""
import argparse
import csv
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN_PATH = os.path.join(HERE, "scan_cheap_burns.py")
SOLSWEEP_PATH = os.path.join(HERE, "solsweep.py")

GRAPHQL_ENDPOINT = "https://graphql.tensor.trade/graphql"

QUERY = """query ActiveListingsPricesV2($slug: String!, $filters: ActiveListingsFilters) {
  activeListingsPricesV2(slug: $slug, filters: $filters) {
    prices {
      ...ReducedActiveListingPrice
      __typename
    }
    numListed
    maxPrice {
      ...ReducedActiveListingPrice
      __typename
    }
    __typename
  }
}

fragment ReducedActiveListingPrice on ActiveListingPrice {
  tx {
    mint {
      onchainId
      name
      imageUri
      sellRoyaltyFeeBPS
      ...MintRarityFields
      __typename
    }
    __typename
  }
  owner
  price
  txAt
  source
  __typename
}

fragment MintRarityFields on TLinkedTxMintTV2 {
  rarityRankHrtt
  rarityRankStat
  rarityRankTeam
  rarityRankTn
  __typename
}"""


def load_module(path, name):
    if not os.path.exists(path):
        sys.exit("Couldn't find %s next to this script. Keep all the files in the same folder." % path)
    src = open(path, encoding="utf-8").read()
    ns = {"__name__": name, "__file__": path}
    exec(compile(src, path, "exec"), ns)
    return ns


def fetch_listings(session, tensor_slug, cookie, timeout):
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://www.tensor.trade",
        "Referer": "https://www.tensor.trade/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                      " (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    }
    if cookie:
        headers["Cookie"] = cookie
    body = {"operationName": "ActiveListingsPricesV2",
             "variables": {"slug": tensor_slug, "filters": None},
             "query": QUERY}
    r = session.post(GRAPHQL_ENDPOINT, headers=headers, json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError("HTTP %d: %s" % (r.status_code, r.text[:300]))
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError("response is not JSON (looks like a block): %s" % r.text[:300])
    if j.get("errors"):
        raise RuntimeError("GraphQL error: %s" % j["errors"])
    data = (j.get("data") or {}).get("activeListingsPricesV2")
    if data is None:
        raise RuntimeError("unexpected response shape: %s" % str(j)[:500])
    return data.get("prices") or []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="tensor_candidates.csv", help="output of tensor_discover.py")
    ap.add_argument("--max-price", type=float, default=0.006)
    ap.add_argument("--min-profit", type=float, default=0.001)
    ap.add_argument("--rpc", help="RPC endpoint; if not given, read from settings.json")
    ap.add_argument("--cookie", default="", help="Cookie header string from a browser, in case of a block")
    ap.add_argument("--delay", type=float, default=0.3, help="pause between requests to Tensor, seconds")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out", default="candidates_tensor.csv")
    ap.add_argument("--per-collection-limit", type=int, default=0,
                     help="only take the N cheapest listings per collection (0 = all). "
                          "Useful for a quick survey across a wide price range - e.g. --per-collection-limit 1 "
                          "to look at just the single cheapest NFT per collection without waiting for a full pass.")
    ap.add_argument("--dump-raw", action="store_true", help="print the raw response for the first collection and exit")
    args = ap.parse_args()

    rows_in = []
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("tensor_slug"):
                rows_in.append(row)
    if not rows_in:
        sys.exit("Empty, or no tensor_slug column, in %s" % args.csv)
    print("Collections in list: %d" % len(rows_in))

    session = requests.Session()
    all_listings = []  # (symbol, mint, price_sol) - using the collection name as "symbol" for the report
    for i, row in enumerate(rows_in, 1):
        slug = row["tensor_slug"]
        name = row.get("name") or slug
        try:
            prices = fetch_listings(session, slug, args.cookie, args.timeout)
        except Exception as e:
            print("  [%d/%d, %s] error: %s" % (i, len(rows_in), name, e))
            continue
        if args.dump_raw:
            import json
            print(json.dumps(prices[:3], indent=2, ensure_ascii=False)[:3000])
            return
        parsed = []
        for p in prices:
            price_lamports = p.get("price")
            mint_info = ((p.get("tx") or {}).get("mint")) or {}
            mint = mint_info.get("onchainId")
            if price_lamports is None or not mint:
                continue
            try:
                price_sol = float(price_lamports) / 1e9
            except (TypeError, ValueError):
                continue
            parsed.append((price_sol, mint))
        parsed.sort(key=lambda t: t[0])  # cheapest listings in the collection first
        if args.per_collection_limit > 0:
            parsed = parsed[:args.per_collection_limit]
        got = 0
        for price_sol, mint in parsed:
            if price_sol > args.max_price:
                continue
            all_listings.append((name, mint, price_sol))
            got += 1
        if got or (i % 20 == 0):
            print("  [%d/%d, %s] listings <= %.4f SOL: %d (total active: %d)"
                  % (i, len(rows_in), name, args.max_price, got, len(prices)))
        time.sleep(args.delay)

    print("\nListings collected from Tensor: %d" % len(all_listings))

    ns_scan = load_module(SCAN_PATH, "scan_cheap_burns_lib")
    check_listings_and_report = ns_scan["check_listings_and_report"]

    ns_sol = load_module(SOLSWEEP_PATH, "solsweep_lib")
    metadata_pda = ns_sol["metadata_pda"]
    edition_pda = ns_sol["edition_pda"]
    parse_metadata = ns_sol["parse_metadata"]
    make_rpc = ns_sol["make_rpc"]
    load_settings = ns_sol["load_settings"]
    TOKEN_PROGRAM = ns_sol["TOKEN_PROGRAM"]

    rpc_url = args.rpc
    rps = 8.0
    if not rpc_url:
        settings_cfg = load_settings()
        rpc_url = settings_cfg.get("rpc") or ""
        rps = float(settings_cfg.get("rps") or 8.0)
        if not rpc_url:
            sys.exit("No RPC given and none found in settings.json next to this script.")
        print("RPC taken from settings.json: %s" % rpc_url.split("?")[0])
    rpc = make_rpc({"rpc": rpc_url, "rps": rps, "priority_fee": 0})

    check_listings_and_report(all_listings, rpc, metadata_pda, edition_pda, parse_metadata,
                               TOKEN_PROGRAM, args.max_price, args.min_profit, args.out)


if __name__ == "__main__":
    main()
