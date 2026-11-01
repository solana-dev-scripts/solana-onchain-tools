#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_cheap_burns.py - looks through Magic Eden listings for NFTs priced below
the SOL locked in their metadata+edition accounts (that rent is reclaimed on
burn). Candidates = potential profit from "buy below rent, then burn".

IMPORTANT, read before running:
  - Listings (/collections/<symbol>/listings) and the tokenMint/price fields
    were verified live (real ME response, 2026-10-31) - this part can be
    trusted.
  - Collection auto-discovery (--discover) has NOT been verified live (the
    /collections schema isn't confirmed by a real response). Run
    --dump-collections-raw first and eyeball whether the fields look like
    symbol/floorPrice - if not, fix collection_symbol()/collection_floor()
    further down in this file.
  - Compressed NFTs (cNFTs) are filtered out automatically: they have no
    account on the regular Metaplex Token Metadata program, so rent can't be
    read and the candidate is skipped.
  - A SEPARATE trap, unrelated to compressed NFTs: for some "NFTs", the
    metadata and edition accounts used to EXIST but have already been closed
    (someone already reclaimed the rent) - while Magic Eden's cache still
    shows the old name/image as if the NFT were alive. Heuristic: if many
    listings in a collection share the EXACT SAME name/image (no individual
    numbering), that's a strong signal, and the script flags such
    collections with a separate warning. This trap is caught on-chain anyway
    (no account = no candidate), the heuristic just saves RPC calls by
    skipping the collection up front.
  - This is RESEARCH ONLY (reading + computing). Buying is done manually,
    through the Magic Eden or Tensor UI. The script sends nothing to the
    network besides ordinary GET requests.

Usage (solsweep.py must sit next to this script - it reuses solsweep's
NFT/rent-checking functions so the estimate matches exactly what will later
be burned; --rpc can be omitted if it's already configured in solsweep's
settings.json):

    python scan_cheap_burns.py --symbols symbols.txt --out candidates.csv

    # or without a manual list - auto-discover cheap collections via /collections:
    python scan_cheap_burns.py --discover 20 --out candidates.csv

symbols.txt - a list of Magic Eden collection symbols (from a URL like
magiceden.io/marketplace/<symbol>), one per line, # comments allowed.
"""
import argparse
import base64
import csv
import json
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SOLSWEEP_PATH = os.path.join(HERE, "solsweep.py")


def load_lib():
    if not os.path.exists(SOLSWEEP_PATH):
        sys.exit("Couldn't find solsweep.py next to this script (%s). Keep both files in the same folder." % SOLSWEEP_PATH)
    src = open(SOLSWEEP_PATH, encoding="utf-8").read()
    ns = {"__name__": "solsweep_lib", "__file__": SOLSWEEP_PATH}
    exec(compile(src, SOLSWEEP_PATH, "exec"), ns)
    return ns


def read_symbols(path):
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def extract_listings(res):
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        return res.get("listings") or res.get("results") or res.get("data") or []
    return []


def listing_mint(it):
    for k in ("tokenMint", "mintAddress", "mint", "tokenAddress"):
        v = it.get(k)
        if v:
            return v
    tok = it.get("token")
    if isinstance(tok, dict):
        for k in ("mintAddress", "mint", "tokenMint"):
            v = tok.get(k)
            if v:
                return v
    return None


def listing_price(it):
    for k in ("price", "listPrice", "priceSol", "amount"):
        v = it.get(k)
        if v is not None:
            return v
    return None


def collection_symbol(it):
    for k in ("symbol", "collectionSymbol"):
        v = it.get(k)
        if v:
            return v
    return None


def collection_floor(res):
    # /collections/{symbol}/stats - the response object itself (not a list); the
    # field name holding the floor price isn't confirmed live, so we try the
    # known candidate names.
    if not isinstance(res, dict):
        return None
    for k in ("floorPrice", "floor_price", "floor"):
        v = res.get(k)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        # guard against lamports instead of SOL: this is the legacy v2 API,
        # prices there are often in lamports - if the number is huge, divide by 1e9
        return v / 1e9 if v > 1000 else v
    return None


def extract_collections(res):
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        return res.get("collections") or res.get("results") or res.get("data") or []
    return []


def load_progress(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
            for k, default in (("next_page", 0), ("candidates", []), ("checked", []), ("found", [])):
                st.setdefault(k, default)
            return st
        except (OSError, ValueError):
            print("  [discover] progress file %s is corrupted - starting from scratch." % path)
    return {"next_page": 0, "candidates": [], "checked": [], "found": []}


def save_progress(path, state, found_txt_path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with open(found_txt_path, "w", encoding="utf-8") as f:
        f.write("# Collections with floor <= --max-price found by auto-discovery (updated as it runs)\n")
        f.write("\n".join(state["found"]) + ("\n" if state["found"] else ""))


def discover_symbols(me_get, pages, max_price, progress_path, page_size=100, page_retries=5):
    # IMPORTANT: /collections (verified live 2026-10-31) does NOT return a floor
    # price at all - only the collection card (symbol, name, hasCNFTs, etc).
    # So the price has to be fetched with a separate request to
    # /collections/{symbol}/stats for every NON-compressed collection - that
    # can be a lot of requests and take a while.
    #
    # Progress is saved to disk after every page and periodically during the
    # /stats check, so a network drop or script crash doesn't lose what's
    # already been found - just run the same command again and it resumes
    # where it left off.
    found_txt_path = progress_path.rsplit(".", 1)[0] + ".txt" if progress_path.endswith(".json") else progress_path + ".txt"
    state = load_progress(progress_path)
    if state["next_page"] or state["candidates"] or state["checked"]:
        print("  [discover] resuming from progress: page %d, candidates queued %d, already checked %d, already found %d"
              % (state["next_page"], len(state["candidates"]), len(state["checked"]), len(state["found"])))

    seen = set(state["candidates"]) | set(state["checked"])
    start_page = state["next_page"]
    consecutive_page_fails = 0
    MAX_CONSECUTIVE_PAGE_FAILS = 3  # after this many fully-failed pages in a row, we assume it's not a
    # transient network blip but the API's own pagination depth limit (a common REST API practice -
    # cutting off offset+limit somewhere in the tens of thousands) - no point paging further, it will
    # keep failing forever. Move on to checking /stats for what's already been collected.
    for page in range(start_page, start_page + pages):
        res, last_err = None, None
        for attempt in range(page_retries):
            try:
                res = me_get("/collections", {"offset": page * page_size, "limit": page_size})
                break
            except Exception as e:
                last_err = e
                print("  [discover, page %d] attempt %d/%d failed: %s" % (page, attempt + 1, page_retries, e))
                time.sleep(min(30, 2 ** attempt))
        if res is None:
            consecutive_page_fails += 1
            print("  [discover, page %d] failed after %d attempts (%s) - skipping this page, continuing."
                  % (page, page_retries, last_err))
            state["next_page"] = page + 1
            save_progress(progress_path, state, found_txt_path)
            if consecutive_page_fails >= MAX_CONSECUTIVE_PAGE_FAILS:
                print("  [discover] %d pages in a row failed completely - looks like the /collections pagination"
                      " depth limit (offset ~%d), not a transient failure. Stopping page iteration and moving"
                      " on to checking /stats for the %d candidates already collected."
                      % (consecutive_page_fails, page * page_size, len(state["candidates"])))
                break
            continue
        consecutive_page_fails = 0
        items = extract_collections(res)
        if not items:
            print("  [discover] page %d is empty - no more collections, stopping." % page)
            state["next_page"] = page
            save_progress(progress_path, state, found_txt_path)
            break
        skipped_cnft, added = 0, 0
        for it in items:
            if not isinstance(it, dict):
                continue
            sym = collection_symbol(it)
            if sym is None:
                continue
            if it.get("hasCNFTs"):
                skipped_cnft += 1
                continue
            if sym not in seen:
                seen.add(sym)
                state["candidates"].append(sym)
                added += 1
        state["next_page"] = page + 1
        save_progress(progress_path, state, found_txt_path)
        print("  [discover] page %d: %d collections, compressed skipped: %d, new candidates: %d (progress saved)"
              % (page, len(items), skipped_cnft, added))

    checked_set = set(state["checked"])
    pending = [s for s in state["candidates"] if s not in checked_set]
    print("  [discover] checking floor prices (/stats): already checked %d, remaining %d..." % (len(state["checked"]), len(pending)))
    for i, sym in enumerate(pending, 1):
        try:
            stats = me_get("/collections/%s/stats" % sym, None)
            floor = collection_floor(stats)
        except Exception as e:
            print("  [discover, %s] /stats request error: %s - skipping this collection." % (sym, e))
            floor = None
        state["checked"].append(sym)
        if floor is not None and floor <= max_price:
            state["found"].append(sym)
        if i % 20 == 0 or i == len(pending):
            save_progress(progress_path, state, found_txt_path)
            print("  [discover] checked %d/%d, found with matching floor: %d (progress saved to %s)"
                  % (i, len(pending), len(state["found"]), found_txt_path))
    return state["found"]


def parse_collection_details_size(raw):
    """Parses the PARENT collection's metadata account and extracts the
    collection_details.size field, if present (this is a so-called "sized"
    collection). Needed for a pre-flight check BEFORE burning: the Burn
    instruction for a member of such a collection decrements this counter by 1
    (size.checked_sub(1)) - if the on-chain counter is already 0 (out of sync
    with reality, which happens with some old/broken collections), this fails
    100% reliably with custom program error 0x33 (NumericalOverflowError), no
    matter how many times you retry. Returns an int (the size) for a V1 sized
    collection, None if unsized/V2/unparseable."""
    try:
        if raw[0] != 4:
            return None
        p = 1 + 32 + 32
        for _ in range(3):
            n = struct.unpack_from("<I", raw, p)[0]
            p += 4 + n
        p += 2  # seller_fee_basis_points
        if raw[p]:
            p += 1 + 4 + 34 * struct.unpack_from("<I", raw, p + 1)[0]
        else:
            p += 1
        p += 2  # primary_sale_happened + is_mutable
        p += 2 if raw[p] else 1  # edition_nonce
        p += 2 if raw[p] else 1  # token_standard
        p += 1 + 33 if raw[p] else 1  # collection: tag + (verified bool + 32-byte pubkey) if Some
        p += 1 + 17 if raw[p] else 1  # uses: tag + (1-byte method + 8-byte remaining + 8-byte total) if Some
        if raw[p]:  # collection_details: Some
            variant = raw[p + 1]
            if variant == 0:  # V1 { size: u64 }
                return struct.unpack_from("<Q", raw, p + 2)[0]
            return None  # V2 { padding } - semantics unknown to us, not checked
        return None  # None - unsized collection, decrement_collection_size is never called for these
    except (IndexError, struct.error, ValueError):
        return None


def check_listings_and_report(all_listings, rpc, metadata_pda, edition_pda, parse_metadata, TOKEN_PROGRAM,
                               max_price, min_profit, out_path):
    """all_listings: a list of (symbol, mint, price_sol), already filtered by max_price at the source.
    Runs the same on-chain checks (compressed/pNFT/Master Edition/Token-2022) that used to live directly
    in main() - factored out so it can be reused for both Magic Eden-sourced listings and listings
    straight from Tensor (both have the same shape: collection symbol/label, mint, price in SOL)."""
    if not all_listings:
        print("\nNothing found within --max-price.")
        return []

    print("\nTotal price candidates: %d. Checking real metadata+edition rent on-chain..." % len(all_listings))

    keys = []
    for sym, mint, price in all_listings:
        keys.append(metadata_pda(mint))
        keys.append(edition_pda(mint))
        keys.append(mint)  # the mint account itself - needed to learn the owning program (classic SPL Token vs Token-2022)
    accs = rpc.multi(keys, full=True)

    survivors = []  # (sym, mint, price, md, md_lam, ed_lam) - passed every check EXCEPT the collection-size check
    fee_est = 2 * 0.000005  # buy + burn, with a small margin
    skipped_no_metadata = 0
    skipped_pnft = 0
    skipped_no_edition = 0
    skipped_token2022 = 0
    for i, (sym, mint, price) in enumerate(all_listings):
        md_acc = accs[3 * i]
        ed_acc = accs[3 * i + 1]
        mint_acc = accs[3 * i + 2]
        if not md_acc or not md_acc.get("data"):
            skipped_no_metadata += 1
            continue  # no metadata account at all - almost certainly a compressed NFT, no rent sits on-chain
        md_raw = base64.b64decode(md_acc["data"][0])
        md = parse_metadata(md_raw)
        if md is None:
            skipped_no_metadata += 1
            continue
        if md.get("standard") in (4, 5):
            skipped_pnft += 1
            continue  # pNFT - burning via this instruction isn't supported
        # IMPORTANT: having metadata alone isn't enough - burning (unfreezing the
        # token account via the Metaplex Burn instruction) requires a LIVE Master
        # Edition. Without one, the NFT can't physically be burned with this tool,
        # even if the metadata is present and its rent looks tempting (confirmed
        # on a real candidate: metadata existed, Master Edition didn't, and the
        # purchase turned out to be dead weight). standard == 3 means a print
        # edition (not a Master Edition); the edition account's byte marker must
        # be 2 or 6, and it must have no printed copies outstanding (otherwise
        # it's a Master Edition with burn restrictions in some cases - filtered
        # out as a precaution, same as solsweep does).
        ed_raw = base64.b64decode(ed_acc["data"][0]) if ed_acc and ed_acc.get("data") else None
        if md.get("standard") == 3 or not ed_raw or ed_raw[0] not in (2, 6) or struct.unpack_from("<Q", ed_raw, 1)[0] > 0:
            skipped_no_edition += 1
            continue
        # Token-2022 (as opposed to classic SPL Token) isn't supported by this burn
        # instruction (burn_nft.py would simply skip it during a real burn) -
        # filtered out here too, so candidates.csv doesn't contain NFTs that
        # definitely can't be burned.
        mint_owner = mint_acc.get("owner") if mint_acc else None
        if mint_owner != TOKEN_PROGRAM:
            skipped_token2022 += 1
            continue
        md_lam = md_acc.get("lamports") or 0
        ed_lam = ed_acc.get("lamports") or 0
        survivors.append((sym, mint, price, md, md_lam, ed_lam))

    # Second pass: for the ones with a VERIFIED collection, check the
    # collection_details.size counter on the parent metadata. The Burn
    # instruction for a member of a "sized" collection decrements this counter
    # by 1; if it's already 0 (out of sync with reality - happens with some
    # old/broken collections), the burn fails with a 100%-reliable custom
    # program error 0x33 (NumericalOverflowError), confirmed on a real
    # purchase. These are filtered out up front.
    coll_keys = []
    coll_idx_map = {}  # index into survivors -> index into coll_keys
    for idx, (sym, mint, price, md, md_lam, ed_lam) in enumerate(survivors):
        coll = md.get("collection")
        if coll and coll[0]:  # verified
            coll_idx_map[idx] = len(coll_keys)
            coll_keys.append(metadata_pda(coll[1]))
    coll_accs = rpc.multi(coll_keys, full=True) if coll_keys else []

    rows = []
    near_misses = []
    skipped_zero_collection_size = 0
    for idx, (sym, mint, price, md, md_lam, ed_lam) in enumerate(survivors):
        if idx in coll_idx_map:
            coll_acc = coll_accs[coll_idx_map[idx]]
            if coll_acc and coll_acc.get("data"):
                coll_size = parse_collection_details_size(base64.b64decode(coll_acc["data"][0]))
                if coll_size == 0:
                    skipped_zero_collection_size += 1
                    continue
        rent_sol = (md_lam + ed_lam) / 1e9
        profit = rent_sol - price - fee_est
        candidate = {"symbol": sym, "mint": mint, "name": (md.get("name") or "").strip(),
                     "price_sol": round(price, 6), "rent_sol": round(rent_sol, 6), "profit_sol": round(profit, 6),
                     "magiceden": "https://magiceden.io/item-details/%s" % mint,
                     "tensor": "https://www.tensor.trade/item/%s" % mint}
        near_misses.append(candidate)  # physically burnable, but profit may be under --min-profit - useful for diagnostics
        if profit >= min_profit:
            rows.append(candidate)

    rows.sort(key=lambda r: -r["profit_sol"])
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=["symbol", "mint", "name", "price_sol", "rent_sol", "profit_sol", "magiceden", "tensor"])
        wr.writeheader()
        wr.writerows(rows)

    print("\nListings checked: %d. Skipped as likely compressed (no metadata account): %d. Skipped pNFT: %d. "
          "Skipped with no live Master Edition (physically can't burn): %d. Skipped as Token-2022 (not supported "
          "by the burn instruction): %d. Skipped due to a zero collection_details.size counter on the parent "
          "collection (Burn is guaranteed to fail with 0x33): %d."
          % (len(all_listings), skipped_no_metadata, skipped_pnft, skipped_no_edition, skipped_token2022,
             skipped_zero_collection_size))
    print("Passed the filter (profit >= %.4f SOL): %d" % (min_profit, len(rows)))
    print("File: %s" % os.path.abspath(out_path))
    if rows:
        total_price = sum(r["price_sol"] for r in rows)
        total_rent = sum(r["rent_sol"] for r in rows)
        total_profit = sum(r["profit_sol"] for r in rows)
        print("\nIf you buy and burn ALL %d of these:" % len(rows))
        print("  spent on purchase:    %.5f SOL" % total_price)
        print("  rent reclaimed:       %.5f SOL" % total_rent)
        print("  net profit:           %.5f SOL" % total_profit)
        print("  (before network priority fees beyond the %.6f SOL/tx already accounted for, and ignoring that"
              " some listings may be bought by someone else first - the real outcome is usually a bit lower)" % fee_est)
        print("\nTop by profit:")
        for r in rows[:10]:
            label = r["name"] or (r["mint"][:4] + "..." + r["mint"][-4:])
            print("  %-20s %-35s price %.5f / rent %.5f / profit %.5f SOL  %s"
                  % (r["symbol"], label, r["price_sol"], r["rent_sol"], r["profit_sol"], r["magiceden"]))
    elif near_misses:
        near_misses.sort(key=lambda r: -r["profit_sol"])
        print("\nNothing passed --min-profit %.4f SOL, but here are %d physically burnable candidates (have"
              " metadata+Master Edition, not Token-2022) closest to the threshold - you might want to lower"
              " --min-profit a bit, or --max-price was set with headroom (price close to rent, margin near-zero"
              "/negative):"
              % (min_profit, min(10, len(near_misses))))
        for r in near_misses[:10]:
            label = r["name"] or (r["mint"][:4] + "..." + r["mint"][-4:])
            print("  %-20s %-35s price %.5f / rent %.5f / profit %.5f SOL  %s"
                  % (r["symbol"], label, r["price_sol"], r["rent_sol"], r["profit_sol"], r["magiceden"]))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rpc", help="RPC endpoint. If not given, read from settings.json next to the script (the same RPC configured for solsweep)")
    ap.add_argument("--symbols", help="file with a list of Magic Eden collection symbols, one per line")
    ap.add_argument("--discover", type=int, metavar="PAGES", help="instead of --symbols: walk /collections itself (100 per page, PAGES pages) and collect collections with floor <= --max-price. Progress is saved to disk - if interrupted, just run the same command again to resume. Pass a deliberately large number (e.g. 100000) to go through everything - the script stops itself on the first empty page.")
    ap.add_argument("--discover-progress", help="file to save --discover progress to (default <out>.discover_progress.json)")
    ap.add_argument("--max-price", type=float, default=0.006, help="max listing price (and, with --discover, collection floor) in SOL (default 0.006)")
    ap.add_argument("--min-profit", type=float, default=0.001, help="minimum computed profit in SOL to be included in the report")
    ap.add_argument("--limit", type=int, default=50, help="how many of the cheapest listings to check per collection (default 50)")
    ap.add_argument("--out", default="candidates.csv")
    ap.add_argument("--dump-raw", action="store_true", help="print the raw ME listings response for the first collection and exit (to check field names)")
    ap.add_argument("--dump-collections-raw", action="store_true", help="print the raw /collections response (first page) and exit (to check auto-discovery field names)")
    ap.add_argument("--dump-stats-raw", metavar="SYMBOL", help="print the raw /collections/SYMBOL/stats response and exit (to check the floor-price field)")
    args = ap.parse_args()

    if not args.symbols and args.discover is None and not args.dump_collections_raw and not args.dump_stats_raw:
        sys.exit("Need --symbols with a collection list file, or --discover N for auto-discovery (for --discover,\n"
                  "check --dump-collections-raw and --dump-stats-raw <symbol> first - the schema hasn't been confirmed live).")

    ns = load_lib()
    me_get = ns["me_get"]
    metadata_pda = ns["metadata_pda"]
    edition_pda = ns["edition_pda"]
    parse_metadata = ns["parse_metadata"]
    make_rpc = ns["make_rpc"]
    load_settings = ns["load_settings"]
    TOKEN_PROGRAM = ns["TOKEN_PROGRAM"]

    if args.dump_collections_raw:
        # offset/limit must be multiples of 20, and offset a multiple of limit (actual ME API requirement)
        res = me_get("/collections", {"offset": 0, "limit": 20})
        print(json.dumps(res, indent=2, ensure_ascii=False)[:4000])
        return

    if args.dump_stats_raw:
        res = me_get("/collections/%s/stats" % args.dump_stats_raw, None)
        print(json.dumps(res, indent=2, ensure_ascii=False)[:4000])
        return

    if args.discover is not None:
        progress_path = args.discover_progress or (args.out + ".discover_progress.json")
        print("Auto-discovering collections: up to %d pages, floor <= %.4f SOL (progress: %s)..." % (args.discover, args.max_price, progress_path))
        symbols = discover_symbols(me_get, args.discover, args.max_price, progress_path)
        print("Collections found: %d" % len(symbols))
        if not symbols:
            print("Empty - the /collections schema may not match what's assumed here. Check --dump-collections-raw.")
            return
    else:
        symbols = read_symbols(args.symbols)
        if not symbols:
            sys.exit("Collection list is empty: %s" % args.symbols)
        print("Collections in list: %d" % len(symbols))

    rpc_url = args.rpc
    rps = 8.0
    if not rpc_url:
        settings_cfg = load_settings()
        rpc_url = settings_cfg.get("rpc") or ""
        rps = float(settings_cfg.get("rps") or 8.0)
        if not rpc_url:
            sys.exit("No RPC given and none found in settings.json next to the script.\n"
                      "Either run solsweep.py and set the RPC there (Settings -> 1), or pass --rpc explicitly.")
        print("RPC taken from settings.json: %s" % rpc_url.split("?")[0])

    rpc = make_rpc({"rpc": rpc_url, "rps": rps, "priority_fee": 0})

    all_listings = []  # (symbol, mint, price_sol)
    for sym in symbols:
        try:
            res = me_get("/collections/%s/listings" % sym, {"offset": 0, "limit": args.limit})
        except Exception as e:
            print("  [%s] listings request error: %s" % (sym, e))
            continue
        if args.dump_raw:
            sample = res[:2] if isinstance(res, list) else res
            print(json.dumps(sample, indent=2, ensure_ascii=False)[:4000])
            return
        items = extract_listings(res)
        names = [it.get("name") or (it.get("token") or {}).get("name") for it in items if isinstance(it, dict)]
        names = [n for n in names if n]
        if names and len(set(names)) == 1 and len(names) >= 3:
            print("  [%s] WARNING: all %d listings share the same name (\"%s\") - looks like an already-stripped"
                  " collection (metadata/edition may have been closed before you got here), skipping without an"
                  " RPC check." % (sym, len(names), names[0]))
            continue
        got = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            mint = listing_mint(it)
            price = listing_price(it)
            if mint is None or price is None:
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            if price > args.max_price:
                continue
            all_listings.append((sym, mint, price))
            got += 1
        print("  [%s] listings <= %.4f SOL: %d" % (sym, args.max_price, got))

    rows = check_listings_and_report(all_listings, rpc, metadata_pda, edition_pda, parse_metadata,
                                       TOKEN_PROGRAM, args.max_price, args.min_profit, args.out)


if __name__ == "__main__":
    main()
