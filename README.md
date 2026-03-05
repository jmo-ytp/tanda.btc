# tanda-btc

A trustless tanda/rosca savings circle implemented on Bitcoin using Taproot, MuSig2, and HTLCs. No custodian, no trusted coordinator — every spending path is enforced by Bitcoin script.

## What is a tanda?

A tanda (also called rosca, susu, or hui) is a rotating savings circle: N participants each contribute the same amount every round. One participant wins the full pot each round, rotating until everyone has received it once. Traditionally this requires trust in an organizer. This implementation removes that trust entirely.

## Protocol overview

Each round, all N participants send their contribution to a shared **Taproot address**. The output key is the MuSig2 aggregate of all participant public keys. The script tree provides two fallback paths if cooperation fails:

```
Taproot output
├── keypath  — MuSig2(P₁, …, Pₙ)  →  cooperative claim by winner
├── leaf1    — <Pₖ> OP_CHECKSIGVERIFY OP_SHA256 <H> OP_EQUAL  →  HTLC claim (winner + preimage)
└── leaf2    — <T_refund> OP_CSV OP_DROP  +  thresh(k_min, P₁, …, Pₙ)  →  collective refund
```

### Spending paths

| Path | Who | When | How |
|---|---|---|---|
| **Keypath** | All participants | Cooperative case | MuSig2 aggregate signature pays winner |
| **Leaf 1 (HTLC)** | Round winner Pₖ | Coordinator disappears after `t_claim` blocks | Pₖ signs + reveals SHA-256 preimage published by coordinator |
| **Leaf 2 (Refund)** | k_min-of-N participants | Winner disappears after `t_refund` blocks | Schnorr multisig (OP_CHECKSIGADD) returns funds pro-rata |

The coordinator is a **trustless** role: it generates HTLC secrets, builds transactions, and orchestrates the MuSig2 signing round, but it can never steal funds. Any participant can fill the coordinator role.

## Architecture

```
tanda/
  protocol.py      — Taproot scripts, transaction builders, BIP-341/342 sighash
  musig2.py        — BIP-327 MuSig2 (key aggregation, nonce gen, partial signing, aggregation)
  htlc.py          — HTLC secret generation and preimage verification
  coordinator.py   — Round orchestration: setup, contribution monitoring, MuSig2 flow, fallbacks
  participant.py   — Participant: contribute, sign claim, claim via HTLC, sign refund
  rpc.py           — Bitcoin Core JSON-RPC wrapper (wallet-less + wallet paths)

tests/
  test_protocol.py      — Unit tests: scripts, MuSig2, transactions (no node required)
  test_coordinator.py   — Unit tests: coordinator + participant with mock RPC (no node required)
  test_e2e_regtest.py   — End-to-end regtest: cooperative, HTLC fallback, refund fallback

scripts/
  regtest_setup.sh — Start bitcoind regtest and mine initial blocks
```

## Requirements

- Python 3.11+
- Bitcoin Core (compiled with or without wallet support)
- `bitcoin-util` in PATH (used for wallet-less PoW grinding)

```bash
pip install -r requirements.txt
```

Dependencies: `embit`, `python-bitcoinrpc`, `coincurve`, `pytest`.

## Running tests

```bash
# Unit tests — no Bitcoin node required
python -m pytest tests/test_protocol.py tests/test_coordinator.py -v

# End-to-end tests — requires a running regtest node
bash scripts/regtest_setup.sh
python -m pytest tests/test_e2e_regtest.py -v -s
```

The e2e tests cover three complete round scenarios on regtest:
- **Round 0** — cooperative MuSig2 keypath spend
- **Round 1** — HTLC fallback (leaf1): winner claims by revealing preimage
- **Round 2** — collective refund (leaf2): k_min participants reclaim funds after timelock

## Bitcoin Core setup

This project works with Bitcoin Core compiled **without wallet support**. Mining uses the wallet-less path: `getblocktemplate` → `bitcoin-util grind` → `submitblock`. UTXO discovery uses `scantxoutset`.

Default RPC configuration (`~/.bitcoin/bitcoin.conf`):
```
regtest=1
server=1
rpcuser=user
rpcpassword=password
rpcport=18443
txindex=1
```

## MuSig2 signing flow (cooperative path)

```
Coordinator                          Participants
──────────                           ────────────
setup()                              ← receive round parameters
  key_agg(pubkeys)
  apply_tweak(kac, taproot_tweak)
  → kac.agg_pk == output_key_xonly

prepare_claim_session()              ← claim_tx distributed

                                     generate_nonce(agg_pk)
collect_pub_nonce() × N              → pub_nonce sent to coordinator

finalize_nonce_aggregation()
build_session_context()              ← SessionContext distributed

                                     sign_claim(session_ctx)
collect_partial_sig() × N            → psig sent to coordinator

aggregate_and_broadcast()
  partial_sig_agg(psigs)
  → 64-byte Schnorr sig
  → broadcast claim_tx
```

## Key design decisions

- **One Taproot address per round** — each round has a fresh HTLC hash, so addresses are unlinkable
- **MuSig2 keypath hides script tree** — on-chain cooperative spends are indistinguishable from single-key P2TR
- **BIP-342 CHECKSIGADD for refund multisig** — efficient Tapscript threshold with explicit absent-signer slots (`b""`)
- **Per-input sighash** — BIP-341 commits to `input_index`; multi-input transactions require fresh nonces and separate signatures for each input
