# Solana on-chain tooling

A set of standalone Python scripts for working directly with Solana on-chain data —
no SDK abstraction layers, just raw RPC calls and manual Borsh/account-layout parsing.

Built while researching NFT rent-reclaim economics on Solana (buying NFTs listed
below their locked rent value and burning them to recover the rent). The underlying
skills — reading Metaplex Token Metadata accounts byte-by-byte, reverse-engineering
undocumented marketplace APIs, and building safe (simulate-before-send) transaction
pipelines — are the reusable part and the reason this is here.

## Requirements

- Python 3.8+
- `pip install requests`
- `burn_nft.py` and `scan_cheap_burns.py` additionally import a local
  `solsweep.py` module (not included in this repo — it's part of a separate,
  closed-source tool) for shared low-level helpers: `metadata_pda`,
  `edition_pda`, `parse_metadata`, `make_rpc`, `load_settings`,
  `keypair_from_text`, `ix_burn_nft`, `simulate`, `send_tx`, `TOKEN_PROGRAM`,
  `me_get`. These two scripts are included here to show the on-chain
  validation/parsing logic (the actually reusable part); they won't run
  standalone without an equivalent helper module providing those names.
  `tensor_discover.py`, `tensor_to_symbols.py`, and `tensor_listings_scan.py`
  have no such dependency and run as-is.

## What's in here

### `tensor_discover.py`
Searches Tensor's public collection-search endpoint (`search.tensor.trade`,
Typesense) for non-compressed collections with a floor price in a given
range, filtering server-side so you don't have to page through compressed
NFTs that can't be individually burned.

```
python tensor_discover.py --max-price 0.01 --limit 2000 --out tensor_candidates.csv
```

| Flag | Default | Meaning |
|---|---|---|
| `--max-price` | `0.01` | Max collection floor price, in SOL |
| `--limit` | `5000` | Max number of collections to collect |
| `--per-page` | `250` | Results per request (Typesense hard cap is 250) |
| `--delay` | `0.5` | Seconds to sleep between pages |
| `--timeout` | `20.0` | Per-request timeout, seconds |
| `--cookie` | — | Cookie header from a real browser (see **Tensor cookie** below) |
| `--out` | `tensor_candidates.csv` | Output CSV path |

### `tensor_to_symbols.py`
Converts the discovery CSV into a plain list of Magic Eden symbols, for
collections that have a Magic Eden listing too.

```
python tensor_to_symbols.py --csv tensor_candidates.csv --out tensor_symbols.txt
```

| Flag | Default | Meaning |
|---|---|---|
| `--csv` | `tensor_candidates.csv` | Input, output of `tensor_discover.py` |
| `--out` | `tensor_symbols.txt` | Output, one symbol per line |

### `tensor_listings_scan.py`
Pulls real active listings (mint + price) directly from Tensor's GraphQL API
for every collection in the discovery CSV — including the ones with no
Magic Eden presence — then runs them through the same on-chain checks as
`scan_cheap_burns.py` (imported from it directly, so the logic is identical).

```
python tensor_listings_scan.py --csv tensor_candidates.csv --max-price 0.006 --min-profit 0.001 --out candidates_tensor.csv
```

| Flag | Default | Meaning |
|---|---|---|
| `--csv` | `tensor_candidates.csv` | Input, output of `tensor_discover.py` |
| `--max-price` | `0.006` | Max listing price, in SOL |
| `--min-profit` | `0.001` | Min computed profit to include in the report, in SOL |
| `--rpc` | — | Solana RPC endpoint; falls back to `settings.json` |
| `--cookie` | — | Cookie header, only needed if this endpoint starts requiring one (see below) |
| `--delay` | `0.3` | Seconds to sleep between collection requests |
| `--timeout` | `20.0` | Per-request timeout, seconds |
| `--out` | `candidates_tensor.csv` | Output CSV path |
| `--per-collection-limit` | `0` (unlimited) | Only check the N cheapest listings per collection |
| `--dump-raw` | off | Print the raw response for the first collection and exit |

### `scan_cheap_burns.py`
Scans Magic Eden collection listings and reports which NFTs are cheaper than
the rent locked in their `metadata` + `edition` accounts. Exposes
`check_listings_and_report()`, which `tensor_listings_scan.py` reuses.

```
python scan_cheap_burns.py --symbols symbols.txt --max-price 0.006 --min-profit 0.001 --out candidates.csv
# or auto-discover collections instead of a symbols file:
python scan_cheap_burns.py --discover 20 --out candidates.csv
```

| Flag | Default | Meaning |
|---|---|---|
| `--rpc` | — | Solana RPC endpoint; falls back to `settings.json` |
| `--symbols` | — | File with Magic Eden collection symbols, one per line |
| `--discover` | — | Auto-walk `/collections` for `PAGES` pages instead of `--symbols` |
| `--discover-progress` | `<out>.discover_progress.json` | Progress file for `--discover` (resumable) |
| `--max-price` | `0.006` | Max listing price, in SOL |
| `--min-profit` | `0.001` | Min computed profit to include in the report, in SOL |
| `--limit` | `50` | Listings checked per collection |
| `--out` | `candidates.csv` | Output CSV path |
| `--dump-raw` / `--dump-collections-raw` / `--dump-stats-raw SYMBOL` | off | Print a raw API response and exit, for checking field names |

### `burn_nft.py`
Burns a specific list of NFTs from one wallet via the Metaplex Burn
instruction, with pre-flight checks (pNFT, print editions, the
`collection_details.size == 0` trap — see below) so it refuses to send a
transaction that's guaranteed to fail.

```
python burn_nft.py --donor donor.txt --mint <mint1> --mint <mint2>
python burn_nft.py --donor donor.txt --mints-file mints.txt
```

| Flag | Default | Meaning |
|---|---|---|
| `--donor` | required | File holding one private key (base58 or JSON byte array) |
| `--mint` | — | Mint address to burn (repeat the flag for more than one) |
| `--mints-file` | — | File with mint addresses, one per line, alternative to `--mint` |
| `--rpc` | — | Solana RPC endpoint; falls back to `settings.json` |

## Tensor cookie

`search.tensor.trade` (used by `tensor_discover.py`) sits behind Cloudflare
Turnstile. Without a cookie, requests get a `403 TURNSTILE_REQUIRED`. Fix:
open tensor.trade in a real browser, open DevTools → Application/Storage →
Cookies, copy the value of **`__Secure-tensor_verified`**, and pass it as:

```
python tensor_discover.py --cookie "__Secure-tensor_verified=<value>" ...
```

That single cookie was enough in testing (the `cf_clearance` cookie wasn't
actually required alongside it). It's short-lived — roughly 30 minutes — so
it needs refreshing from the browser if the script starts getting 403s again.

The GraphQL listings endpoint used by `tensor_listings_scan.py`
(`graphql.tensor.trade/graphql`) did **not** require a cookie at the time
this was written — `--cookie` is there in case that changes later.

## A specific on-chain failure this handles: `custom program error: 0x33`

Some NFTs belong to a "sized" verified collection, where the parent
collection's metadata tracks a `collection_details.size` counter. The Burn
instruction decrements this counter by 1; if it's already at 0 (a real,
on-chain data inconsistency that exists for some older/broken collections),
the subtraction underflows and the transaction fails 100% of the time with
`custom program error: 0x33` (`NumericalOverflowError`) — no retry fixes it.
Both `burn_nft.py` and `scan_cheap_burns.py` parse this field directly from
the raw account bytes (not exposed by most SDKs) and skip these NFTs before
a transaction is ever built or sent.

## Notes

- No API keys, RPC endpoints, or wallet keys are hardcoded — all are supplied
  via CLI args or a local (gitignored) `settings.json`.
- Written for clarity of the on-chain parsing logic over polish; happy to
  adapt any of this to a specific task (account parsing, marketplace API
  work, tx simulation pipelines, etc).
