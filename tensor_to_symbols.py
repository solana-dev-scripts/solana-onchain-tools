#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tensor_to_symbols.py - takes tensor_candidates.csv (the output of tensor_discover.py)
and extracts the Magic Eden symbols (symbol_me column) so they can be fed into
scan_cheap_burns.py via --symbols. This works around Magic Eden's broken
/collections pagination entirely: collections are already filtered by floor
price and "not compressed" on Tensor's side, this script just converts format.

Rows without symbol_me (collections that have no Magic Eden presence) are
skipped - there is no source of real listings/mint addresses for them yet.

Usage:
    python tensor_to_symbols.py --csv tensor_candidates.csv --out tensor_symbols.txt
    python scan_cheap_burns.py --symbols tensor_symbols.txt --max-price 0.01 ...
"""
import argparse
import csv
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="tensor_candidates.csv")
    ap.add_argument("--out", default="tensor_symbols.txt")
    args = ap.parse_args()

    seen = set()
    symbols = []
    skipped_no_me = 0
    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sym = (row.get("symbol_me") or "").strip()
            if not sym:
                skipped_no_me += 1
                continue
            if sym in seen:
                continue
            seen.add(sym)
            symbols.append(sym)

    if not symbols:
        sys.exit("Found no symbol_me values in %s - check that the file came from "
                  "tensor_discover.py and isn't empty." % args.csv)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(symbols) + "\n")

    print("Magic Eden symbols: %d (skipped, no symbol_me: %d)" % (len(symbols), skipped_no_me))
    print("Saved to %s - you can now run:" % args.out)
    print("  python scan_cheap_burns.py --symbols %s --max-price 0.01 --min-profit 0.001 --out candidates.csv" % args.out)


if __name__ == "__main__":
    main()
