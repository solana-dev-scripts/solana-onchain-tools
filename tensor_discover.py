#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tensor_discover.py - searches Tensor (search.tensor.trade, the same Typesense
instance the tensor.trade site itself uses) for non-classic/compressed
collections, filters those out server-side, and keeps only regular
(classic/pNFT) collections with floor > 0, sorted by floor ascending.

Why this is needed: "cheap" collections on Tensor are almost all compressed
NFTs (especially anything from DRiP) - they have no per-NFT metadata/edition
account of their own, rent is shared across the whole collection's merkle
tree, and it can't be burned and reclaimed one NFT at a time. Tensor itself
flags this with a "compressed": true/false field on every collection card -
we filter server-side so we don't have to manually sift through thousands of
unusable entries.

Some collections have a "slugMe" field - this is the same collection's symbol
on Magic Eden. Those can be fed straight into scan_cheap_burns.py (via
--discover 0 plus manually appending the symbols, or a dedicated mode - see
--dump-symbols-only), bypassing Magic Eden's /collections pagination depth
limit entirely.

IMPORTANT - about Cloudflare: based on the captured request, search.tensor.trade
sits behind Cloudflare with Turnstile ("x-tensor-turnstile: verified" in the
response). It's possible that /multi_search specifically doesn't require
passing the challenge (it's a public search endpoint, hit by the site itself
on every keystroke) - in that case the script will just work. If you get a
403/503, or a response that doesn't look like JSON with collections, that
means Cloudflare is blocking the non-browser request - you'll need a
cf_clearance cookie from your browser (it's only valid for a while and tied
to your IP, so it needs refreshing) - pass it via --cookie, or switch to
another approach.

Usage:
    python tensor_discover.py --max-price 0.01 --limit 2000 --out tensor_candidates.csv
"""
import argparse
import csv
import json
import sys
import time

import requests

ENDPOINT = "https://search.tensor.trade/multi_search"
ORIGIN = "https://www.tensor.trade"
API_KEY = "DOCUMENT_SEARCH_ONLY_KEY"
MAX_PER_PAGE = 250  # hard Typesense limit


def search_page(session, page, per_page, max_price_lamports, cookie, timeout):
    filter_by = "compressed:false && statsV2.buyNowPrice:>0"
    if max_price_lamports is not None:
        filter_by += " && statsV2.buyNowPrice:<=%d" % max_price_lamports
    params = {
        "sort_by": "statsV2.buyNowPrice:asc",
        "infix": "fallback,off,off",
        "page": page,
        "per_page": per_page,
        "split_join_tokens": "off",
        "exhaustive_search": "true",
        "prioritize_exact_match": "true",
    }
    body = {
        "searches": [{
            "collection": "nft_collections",
            "q": "*",
            "query_by": "name,slugDisplay,acronym",
            "filter_by": filter_by,
        }]
    }
    headers = {
        "Content-Type": "application/json",
        "X-TYPESENSE-API-KEY": API_KEY,
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                      " (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
    }
    if cookie:
        headers["Cookie"] = cookie

    r = session.post(ENDPOINT, params=params, headers=headers, json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError("HTTP %d: %s" % (r.status_code, r.text[:300]))
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError("response is not JSON (looks like a Cloudflare/anti-bot block): %s" % r.text[:300])
    res = j.get("results", [None])[0]
    if res is None or "hits" not in res:
        raise RuntimeError("unexpected response shape: %s" % json.dumps(j)[:500])
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-price", type=float, default=0.01, help="max floor price in SOL (default 0.01)")
    ap.add_argument("--limit", type=int, default=5000, help="max number of collections to collect (default 5000)")
    ap.add_argument("--per-page", type=int, default=MAX_PER_PAGE)
    ap.add_argument("--delay", type=float, default=0.5, help="pause between pages, seconds (default 0.5)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--cookie", default="", help="Cookie header string from a real browser; without it you'll likely get a 403/block")
    ap.add_argument("--out", default="tensor_candidates.csv")
    args = ap.parse_args()

    max_price_lamports = int(round(args.max_price * 1e9)) if args.max_price else None
    per_page = min(args.per_page, MAX_PER_PAGE)

    session = requests.Session()
    rows = []
    page = 1
    total_found = None
    with_slugme = 0

    while len(rows) < args.limit:
        print("[page %d] requesting..." % page)
        try:
            res = search_page(session, page, per_page, max_price_lamports, args.cookie, args.timeout)
        except Exception as e:
            print("ERROR: %s" % e)
            if page == 1:
                sys.exit("Couldn't even fetch the first page - see the Cloudflare note at the top of this file.")
            break

        if total_found is None:
            total_found = res.get("found", 0)
            print("total matching collections on the server (after filter): %d" % total_found)

        hits = res.get("hits", [])
        if not hits:
            print("empty page - we've collected everything there is")
            break

        for h in hits:
            d = h.get("document", {})
            buy_now = d.get("statsV2.buyNowPrice")
            if buy_now is None:
                continue
            row = {
                "name": d.get("name", ""),
                "tensor_slug": d.get("slug", ""),
                "symbol_tensor": d.get("symbol", ""),
                "symbol_me": d.get("slugMe", ""),
                "floor_sol": buy_now / 1e9,
                "num_listed": d.get("statsV2.numListed", ""),
                "num_mints": d.get("statsV2.numMints", ""),
                "compressed": d.get("compressed"),
            }
            if row["symbol_me"]:
                with_slugme += 1
            rows.append(row)

        print("  +%d (collected so far: %d, of which with a Magic Eden symbol_me: %d)" % (len(hits), len(rows), with_slugme))

        if len(hits) < per_page:
            print("last page (fewer than per_page results)")
            break
        page += 1
        time.sleep(args.delay)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "tensor_slug", "symbol_tensor", "symbol_me",
                                           "floor_sol", "num_listed", "num_mints", "compressed"])
        w.writeheader()
        w.writerows(rows)

    print("\nTotal non-compressed collections collected with floor > 0 and <= %.5f SOL: %d" % (args.max_price, len(rows)))
    print("Of those with a known Magic Eden symbol (slugMe): %d" % with_slugme)
    print("Saved to %s" % args.out)
    if with_slugme < len(rows):
        print("\nThe other %d collections exist only on Tensor (no slugMe) - they need a separate"
              " way to pull real listing mint addresses (see tensor_listings_scan.py)." % (len(rows) - with_slugme))


if __name__ == "__main__":
    main()
