#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
burn_nft.py - burns ONLY the specified NFTs from a single wallet. No transfers,
no scanning, no purchasing, no menu - one keypair, a list of mint addresses,
burn them, report the reclaimed rent.

Uses the same instruction as solsweep (the Metaplex Burn instruction via the
Token Metadata program, not a plain SPL Token Burn) - it unfreezes the token
account itself as part of burning, unlike the "Burn" button in some wallets
(Solflare etc.), which can issue a plain SPL burn and fail with "Account is
frozen" (0x11) on frozen NFT token accounts.

Usage:
    python burn_nft.py --donor donor.txt --mint <mint1> --mint <mint2> ...
    # or a list of addresses from a file, one per line:
    python burn_nft.py --donor donor.txt --mints-file mints.txt

--rpc can be omitted - it will be read from settings.json next to the script
(the same RPC configured for solsweep).
"""
import argparse
import base64
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOLSWEEP_PATH = os.path.join(HERE, "solsweep.py")


def load_lib():
    if not os.path.exists(SOLSWEEP_PATH):
        sys.exit("Couldn't find solsweep.py next to this script (%s). Keep both files in the same folder." % SOLSWEEP_PATH)
    src = open(SOLSWEEP_PATH, encoding="utf-8").read()
    ns = {"__name__": "solsweep_lib", "__file__": SOLSWEEP_PATH}
    exec(compile(src, SOLSWEEP_PATH, "exec"), ns)
    return ns


def read_donor(path, keypair_from_text):
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                kp = keypair_from_text(s)
                if kp is None:
                    sys.exit("Couldn't parse the key in %s (expected base58 or a JSON byte array)." % path)
                return kp
    sys.exit("File %s is empty - it needs a single private key." % path)


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


def find_token_account(rpc, owner, mint, token_program_const):
    """Finds the ACTUAL token account where the owner holds this mint - doesn't
    derive the address itself (the standard ATA may not match where the NFT
    actually sits), it asks the network directly. Returns (address, owning
    program) or (None, None) if not found."""
    res = rpc.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    for it in res.get("value", []):
        try:
            info = it["account"]["data"]["parsed"]["info"]
            amount = int(info["tokenAmount"]["amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if amount == 1:
            return it["pubkey"], it["account"].get("owner")
    return None, None


def read_mints_file(path):
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--donor", required=True, help="file with ONE private key (base58 or JSON byte array)")
    ap.add_argument("--mint", action="append", default=[], help="mint address of the NFT to burn (repeat the flag for several)")
    ap.add_argument("--mints-file", help="file with a list of mint addresses, one per line")
    ap.add_argument("--rpc", help="RPC endpoint. If not given, read from settings.json next to the script")
    args = ap.parse_args()

    mints = list(args.mint)
    if args.mints_file:
        mints.extend(read_mints_file(args.mints_file))
    if not mints:
        sys.exit("Need at least one --mint or a --mints-file with a list of addresses.")

    ns = load_lib()
    keypair_from_text = ns["keypair_from_text"]
    metadata_pda = ns["metadata_pda"]
    edition_pda = ns["edition_pda"]
    parse_metadata = ns["parse_metadata"]
    TOKEN_PROGRAM = ns["TOKEN_PROGRAM"]
    ix_burn_nft = ns["ix_burn_nft"]
    send_tx = ns["send_tx"]
    simulate = ns["simulate"]
    make_rpc = ns["make_rpc"]
    load_settings = ns["load_settings"]

    donor = read_donor(args.donor, keypair_from_text)
    donor_pub = str(donor.pubkey())
    print("Wallet: %s" % donor_pub)

    rpc_url = args.rpc
    rps = 8.0
    if not rpc_url:
        settings_cfg = load_settings()
        rpc_url = settings_cfg.get("rpc") or ""
        rps = float(settings_cfg.get("rps") or 8.0)
        if not rpc_url:
            sys.exit("No RPC given and none found in settings.json. Either run solsweep.py and set the RPC there, or pass --rpc.")
        print("RPC taken from settings.json: %s" % rpc_url.split("?")[0])
    rpc = make_rpc({"rpc": rpc_url, "rps": rps, "priority_fee": 0})

    print("To burn: %d\n" % len(mints))
    ok_count, fail_count = 0, 0
    for i, mint in enumerate(mints, 1):
        print("[%d/%d] %s" % (i, len(mints), mint))
        try:
            accs = rpc.multi([metadata_pda(mint), edition_pda(mint)], full=True)
        except Exception as e:
            print("  ERROR reading accounts: %s - skipping" % e)
            fail_count += 1
            continue
        md_acc, ed_acc = accs[0], accs[1]
        if not md_acc or not md_acc.get("data"):
            print("  skipping: no metadata account (compressed NFT, or already burned/closed by someone else)")
            fail_count += 1
            continue
        md = parse_metadata(base64.b64decode(md_acc["data"][0]))
        if md is None:
            print("  skipping: couldn't parse metadata")
            fail_count += 1
            continue
        if md.get("standard") in (4, 5):
            print("  skipping: pNFT (programmable) - this burn instruction doesn't support those")
            fail_count += 1
            continue
        ed_raw = base64.b64decode(ed_acc["data"][0]) if ed_acc and ed_acc.get("data") else None
        if md.get("standard") == 3 or (ed_raw and ed_raw[0] == 1):
            print("  skipping: print edition (not a Master Edition) - not supported")
            fail_count += 1
            continue
        if not ed_raw or ed_raw[0] not in (2, 6):
            print("  skipping: no valid Master Edition account (missing or wrong type) - can't be burned with this tool")
            fail_count += 1
            continue
        if struct.unpack_from("<Q", ed_raw, 1)[0] > 0:
            print("  skipping: this Master Edition has printed copies outstanding - not supported")
            fail_count += 1
            continue
        coll = md.get("collection")
        collection_metadata = metadata_pda(coll[1]) if coll and coll[0] else None  # needed for Burn if the collection is verified
        if coll:
            print("  collection in metadata: mint=%s verified=%s%s" % (coll[1], coll[0],
                  (" -> collection_metadata=%s" % collection_metadata) if collection_metadata else ""))
        else:
            print("  collection in metadata: none")

        if collection_metadata:
            try:
                coll_md_acc = rpc.multi([collection_metadata], full=True)[0]
            except Exception as e:
                coll_md_acc = None
                print("  warning: couldn't read the parent collection's metadata (%s) - skipping the counter check" % e)
            if coll_md_acc and coll_md_acc.get("data"):
                coll_size = parse_collection_details_size(base64.b64decode(coll_md_acc["data"][0]))
                if coll_size == 0:
                    print("  skipping: the parent collection's collection_details.size counter is already at 0 - "
                          "Burn would try to subtract 1 from it and fail with custom program error 0x33 "
                          "(NumericalOverflowError); this can't be physically burned with this tool")
                    fail_count += 1
                    continue

        try:
            token_acc, token_owner_program = find_token_account(rpc, donor_pub, mint, TOKEN_PROGRAM)
            if token_acc is None:
                print("  skipping: couldn't find a token account for this mint in the wallet (NFT not in your balance?)")
                fail_count += 1
                continue
            if token_owner_program != TOKEN_PROGRAM:
                print("  skipping: the token account belongs to %s (not classic SPL Token) - this script doesn't support those yet" % token_owner_program)
                fail_count += 1
                continue
            balance_before = rpc.balance(donor_pub) / 1e9
            burn_ix = [ix_burn_nft(metadata_pda(mint), donor_pub, mint, token_acc, edition_pda(mint), collection_metadata)]

            ok, sim_err = simulate(rpc, donor_pub, burn_ix)
            if not ok:
                print("  SIMULATION ERROR: %s - not sending, skipping" % sim_err)
                fail_count += 1
                continue
            sig, err = send_tx(rpc, [donor], donor_pub, burn_ix)
            if err:
                print("  SEND ERROR: %s" % err)
                fail_count += 1
                continue
            balance_after = rpc.balance(donor_pub) / 1e9
        except Exception as e:
            print("  ERROR: %s - skipping" % e)
            fail_count += 1
            continue

        print("  burned, signature: %s | rent to balance: %+.5f SOL" % (sig, balance_after - balance_before))
        ok_count += 1

    print("\nBurned successfully: %d. Failed/skipped: %d." % (ok_count, fail_count))


if __name__ == "__main__":
    main()
