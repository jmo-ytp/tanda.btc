"""
Dockerized e2e tests for the Lightning Network tanda protocol.

These tests run inside the test-runner container defined in
docker-compose.test.yml, which has:
  - Direct access to all four CLN unix sockets (coordinator + 3 participants)
  - HTTP access to the three participant FastAPI APIs
  - HTTP access to Bitcoin Core RPC

Run with:
    docker compose -f docker-compose.test.yml up --build --exit-code-from test-runner

Or, if the stack is already up:
    docker exec <test-runner> python -m pytest tests/test_e2e_ln_docker.py -v -s

The tests are skipped automatically if the CLN coordinator socket is not
accessible (i.e. the stack is not running).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import threading
import time
import urllib.parse

import pytest
import httpx

from tanda.lnrpc import CLNRpc
from tanda.rpc import BitcoinRPC


# ── Environment (injected by docker-compose.test.yml) ──────────────────────

CLN_COORD_RPC = os.environ.get("CLN_COORDINATOR_RPC", "/cln/coordinator/regtest/lightning-rpc")
CLN_P_RPCS = [
    os.environ.get(f"CLN_P{i}_RPC", f"/cln/p{i}/regtest/lightning-rpc")
    for i in range(3)
]
P_URLS = [
    os.environ.get(f"P{i}_URL", f"http://participant-p{i}:8080")
    for i in range(3)
]
BITCOIND_URL = os.environ.get("BITCOIND_RPC_URL", "http://user:password@bitcoind:18443")
CONTRIBUTION_SATS = int(os.environ.get("CONTRIBUTION_SATS", "10000"))

# How long to wait for hold invoices to be accepted after payments are started
ACCEPT_TIMEOUT = 60


# ── Skip guard ─────────────────────────────────────────────────────────────────

def _cln_reachable() -> bool:
    if not os.path.exists(CLN_COORD_RPC):
        return False
    try:
        CLNRpc(CLN_COORD_RPC).get_info()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _cln_reachable(),
    reason="CLN coordinator socket not accessible — start docker-compose.test.yml",
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_rpc() -> BitcoinRPC:
    p = urllib.parse.urlparse(BITCOIND_URL)
    return BitcoinRPC(
        rpc_user=p.username,
        rpc_password=p.password,
        rpc_host=p.hostname,
        rpc_port=p.port or 18443,
    )


def _channel_balance_msat(cln: CLNRpc, peer_id: str) -> int:
    """
    Return our total local balance (msat) across ALL CHANNELD_NORMAL channels
    to peer_id.  Summing handles cases where multiple channels exist between
    the same peers (e.g. from repeated bootstrap runs on persistent volumes).
    Payments may route through any of these channels; summing the balances
    gives the correct aggregate for delta-based assertions.
    """
    total = 0
    found = False
    for ch in cln.list_peer_channels():
        if ch.get("peer_id") != peer_id:
            continue
        if ch.get("state") != "CHANNELD_NORMAL":
            continue
        found = True
        val = ch.get("to_us_msat", ch.get("msatoshi_to_us", 0))
        if isinstance(val, str):
            val = int(val.replace("msat", ""))
        total += int(val)
    if not found:
        raise KeyError(f"No CHANNELD_NORMAL channel found to peer {peer_id[:16]}...")
    return total


def _wait_accepted(
    cln_coord: CLNRpc,
    payment_hashes: list[str],
    errors: list[str] | None = None,
    timeout: int = ACCEPT_TIMEOUT,
) -> None:
    """
    Poll until all HTLCs (identified by payment_hash) appear as incoming HTLCs
    on the coordinator's channels.  Fails fast if payment errors are reported.

    Rationale: CLN's standard listinvoices does not reliably show
    status='pending' for hold invoices held by the holdinvoice plugin.
    Inspecting channel HTLCs directly via listpeerchannels is authoritative.
    """
    n = len(payment_hashes)
    ph_set = set(payment_hashes)
    deadline = time.time() + timeout
    count = 0
    while time.time() < deadline:
        if errors:
            raise RuntimeError(f"Payment errors (fast-fail): {errors}")
        held = cln_coord.get_incoming_htlc_hashes()
        count = len(held & ph_set)
        if count >= n:
            return
        time.sleep(0.5)
    raise TimeoutError(f"Only {count}/{n} HTLCs accepted after {timeout}s")


def _wait_cln_funds(cln: CLNRpc, min_sats: int, timeout: int = 60) -> None:
    """
    Block until CLN's wallet shows at least min_sats in confirmed outputs.

    blockheight sync is necessary but not sufficient: the wallet scanner
    runs asynchronously and may lag a few seconds behind the block processor.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        funds = cln.list_funds()
        confirmed_sats = sum(
            # CLN ≥23 uses "amount_msat"; older uses "value" (sats)
            (o.get("amount_msat", 0) // 1000 if "amount_msat" in o else o.get("value", 0))
            for o in funds.get("outputs", [])
            if o.get("status") == "confirmed"
        )
        if confirmed_sats >= min_sats:
            return
        time.sleep(1)
    raise TimeoutError(f"CLN wallet has < {min_sats} confirmed sats after {timeout}s")


def _wait_balances_stable(
    cln_ps: list[CLNRpc],
    coord_id: str,
    stable_sec: float = 2.0,
    timeout: int = 30,
) -> list[int]:
    """
    Poll all participants' total local balances until they stop changing for
    `stable_sec` consecutive seconds.  Returns the stable balance list.

    This guards against CLN's asynchronous commitment exchange: after
    settle_holdinvoice() returns on the coordinator side, the corresponding
    update_fulfill_htlc messages propagate to each participant separately and
    may take a moment to be reflected in listpeerchannels.to_us_msat.
    """
    prev = [_channel_balance_msat(cln_p, coord_id) for cln_p in cln_ps]
    stable_since = time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        curr = [_channel_balance_msat(cln_p, coord_id) for cln_p in cln_ps]
        if curr == prev:
            if time.time() - stable_since >= stable_sec:
                return curr
        else:
            prev = curr
            stable_since = time.time()
    return [_channel_balance_msat(cln_p, coord_id) for cln_p in cln_ps]


def _wait_cln_synced(cln: CLNRpc, rpc: BitcoinRPC, timeout: int = 60) -> None:
    """
    Block until CLN's blockheight matches bitcoind's current height.

    CLN reports 'Cannot afford: still syncing with bitcoin network' if
    fundchannel is called before it has processed the block containing the
    funding UTXO.  Polling blockheight is the only reliable guard.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        target = rpc.get_block_height()
        info = cln.get_info()
        cln_height = info.get("blockheight", 0)
        syncing = info.get("warning_bitcoind_sync") or info.get("warning_lightningd_sync")
        if cln_height >= target and not syncing:
            return
        time.sleep(1)
    raise TimeoutError(f"CLN not synced after {timeout}s (cln={cln_height}, target={target})")


def _unique_label(base: str) -> str:
    """Make a unique label to avoid collisions between test runs."""
    return f"{base}-{int(time.time() * 1000) % 1_000_000}"


# ── Session fixture: bootstrap ─────────────────────────────────────────────────

@pytest.fixture(scope="session")
def ln_env():
    """
    Bootstrap the LN stack once per test session:
      1. Mine 101 blocks, fund CLN coordinator on-chain.
      2. Connect coordinator → each participant CLN node, open channels
         with 150k sat pushed to each participant.
      3. Mine 6 confirmation blocks and wait for CHANNELD_NORMAL.

    Returns a dict with cln_coord, cln_participants, rpc, p_urls, coord_id, p_ids.
    """
    import hashlib as _h
    from bitcoinrpc.authproxy import AuthServiceProxy
    from embit.ec import PrivateKey as _PK
    from embit.networks import NETWORKS as _NETS

    rpc = _make_rpc()
    cln_coord = CLNRpc(CLN_COORD_RPC)
    cln_ps = [CLNRpc(p) for p in CLN_P_RPCS]

    # ── mine + fund coordinator CLN ────────────────────────────────────────────
    _mine_wif = _PK(_h.sha256(b"regtest_mine_key").digest()).wif(network=_NETS["regtest"])
    coord_addr = rpc._default_mine_addr()

    if rpc.get_block_height() < 101:
        rpc.mine(101, address=coord_addr)

    cln_coord_addr = cln_coord.new_address()
    n = len(cln_ps)
    fund_btc = round(n * 200_000 / 1e8 + 0.001, 8)

    all_utxos = rpc.scan_utxos(coord_addr)
    height = rpc.get_block_height()
    mature = [u for u in all_utxos if int(u.get("height", 0)) <= height - 100]
    assert mature, "No mature coinbases — mine more blocks"

    selected, total_in = [], 0.0
    for u in mature:
        selected.append(u)
        total_in += float(u["amount"])
        if total_in >= fund_btc + 0.0001:
            break

    fee_btc = 0.0001
    change_btc = round(total_in - fund_btc - fee_btc, 8)
    outputs = {cln_coord_addr: fund_btc}
    if change_btc > 0.00000546:
        outputs[coord_addr] = change_btc

    base = AuthServiceProxy(rpc._base_url)
    inputs = [{"txid": u["txid"], "vout": u["vout"]} for u in selected]
    prevtxs = [{
        "txid": u["txid"], "vout": u["vout"],
        "scriptPubKey": u["scriptPubKey"] if isinstance(u["scriptPubKey"], str) else u["scriptPubKey"]["hex"],
        "amount": float(u["amount"]),
    } for u in selected]

    raw = base.createrawtransaction(inputs, outputs)
    signed = base.signrawtransactionwithkey(raw, [_mine_wif], prevtxs)
    assert signed.get("complete"), f"Funding tx incomplete: {signed}"
    base.sendrawtransaction(signed["hex"])
    # Mine 3 blocks: CLN wallet scanner considers UTXOs confirmed after ≥1
    # block, but the scanner runs async and may lag.  3 blocks + explicit
    # list_funds polling is the reliable guard.
    rpc.mine(3)
    _wait_cln_synced(cln_coord, rpc)
    _wait_cln_funds(cln_coord, min_sats=int(fund_btc * 1e8) - 10_000)

    # ── open channels with push ────────────────────────────────────────────────
    coord_id = cln_coord.get_info()["id"]
    p_ids = [cln.get_info()["id"] for cln in cln_ps]

    # Open a channel to each participant if none exists OR if the participant's
    # total local balance has dropped below the test minimum (3 × contribution).
    # The latter handles persistent volumes from previous depleted sessions.
    min_bal_sats = CONTRIBUTION_SATS * 3  # need capacity for 3 rounds
    opened_any = False
    for i, (cln_p, p_id) in enumerate(zip(cln_ps, p_ids)):
        try:
            cln_coord.connect(p_id, f"cln-p{i}", 9735)
        except Exception:
            pass
        # Check existing CHANNELD_NORMAL channels and their total push to participant
        normal_chs = [
            ch for ch in cln_coord.list_peer_channels()
            if ch.get("peer_id") == p_id and ch.get("state") == "CHANNELD_NORMAL"
        ]
        total_their_bal = sum(
            int(str(ch.get("their_to_us_msat",
                           ch.get("msatoshi_to_them", 0))).replace("msat", "")) // 1000
            for ch in normal_chs
        )
        # Recompute participant-side view (more reliable)
        try:
            p_bal = _channel_balance_msat(cln_p, coord_id)
        except KeyError:
            p_bal = 0
        if p_bal < min_bal_sats * 1000:
            cln_coord.fund_channel(p_id, 200_000, push_msat=150_000_000)
            opened_any = True
            # Mine 1 block immediately so the change UTXO from this fundchannel
            # TX is confirmed before the next fundchannel call.  CLN only spends
            # confirmed UTXOs; without this, the second (and third) channel open
            # fails with "0 available UTXOs".
            rpc.mine(1)
            _wait_cln_synced(cln_coord, rpc)

    if opened_any:
        # Mine 6 more blocks so all funding TXs reach CHANNELD_NORMAL
        # (each requires ≥ 6 confirmations for channel announcement).
        rpc.mine(6)
        _wait_cln_synced(cln_coord, rpc)
        for cln_p in cln_ps:
            _wait_cln_synced(cln_p, rpc)

    # Ensure at least n CHANNELD_NORMAL channels exist (one per participant)
    deadline = time.time() + 120
    while time.time() < deadline:
        channels = cln_coord.list_peer_channels()
        normal_peers = {
            c.get("peer_id") for c in channels if c.get("state") == "CHANNELD_NORMAL"
        }
        if len(normal_peers) >= n:
            break
        time.sleep(2)
    else:
        pytest.fail(f"Channels did not reach CHANNELD_NORMAL in 120 s")

    return {
        "cln_coord": cln_coord,
        "cln_ps": cln_ps,
        "rpc": rpc,
        "p_urls": P_URLS,
        "coord_id": coord_id,
        "p_ids": p_ids,
    }


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestLNBootstrap:
    """Verify the channel topology is correct after bootstrap."""

    def test_coordinator_has_n_channels(self, ln_env):
        cln_coord = ln_env["cln_coord"]
        n = len(ln_env["cln_ps"])
        channels = cln_coord.list_peer_channels()
        normal = [c for c in channels if c.get("state") == "CHANNELD_NORMAL"]
        # Use unique peer_ids in case previous runs left duplicate channels
        normal_peers = {c.get("peer_id") for c in normal}
        assert len(normal_peers) >= n

    def test_participants_have_outbound_capacity(self, ln_env):
        """
        Each participant must have enough local balance to pay at least one
        contribution.  We use a low threshold (1 × contribution + a bit extra)
        because persistent Docker volumes may carry depleted balances from
        previous test sessions; the important thing is that rounds can run.
        """
        coord_id = ln_env["coord_id"]
        min_bal = CONTRIBUTION_SATS * 1000 * 2  # enough for 2 rounds
        for i, cln_p in enumerate(ln_env["cln_ps"]):
            bal = _channel_balance_msat(cln_p, coord_id)
            assert bal >= min_bal, (
                f"P{i} local balance {bal} msat < {min_bal} msat (need capacity for ≥2 rounds)"
            )

    def test_participant_apis_healthy(self, ln_env):
        for i, url in enumerate(ln_env["p_urls"]):
            r = httpx.get(f"{url}/health", timeout=10)
            assert r.status_code == 200, f"P{i} API unhealthy: {r.text}"
            assert r.json()["status"] == "ok"


class TestLNRoundHappyPath:
    """Full round: hold invoices → pay winner → settle."""

    def _run_round(self, ln_env, winner_idx: int, round_label: str):
        cln_coord = ln_env["cln_coord"]
        cln_ps    = ln_env["cln_ps"]
        coord_id  = ln_env["coord_id"]
        p_ids     = ln_env["p_ids"]
        p_urls    = ln_env["p_urls"]
        n         = len(cln_ps)

        # N separate preimages — CLN rejects duplicate payment_hashes
        preimages     = [os.urandom(32) for _ in range(n)]
        payment_hashes = [hashlib.sha256(p).hexdigest() for p in preimages]
        labels        = [_unique_label(f"{round_label}-p{i}") for i in range(n)]

        # Record balances before
        p_bal_before = [_channel_balance_msat(cln_p, coord_id) for cln_p in cln_ps]

        # Create N hold invoices (each with its own payment_hash).
        # BoltzExchange/hold API: coordinator passes payment_hash; preimage is
        # kept secret and revealed only at settle time via settleholdinvoice.
        invoices = []
        for i in range(n):
            inv = cln_coord.holdinvoice(
                payment_hash_hex=payment_hashes[i],
                amount_msat=CONTRIBUTION_SATS * 1000,
            )
            invoices.append(inv)

        # Dispatch payments concurrently (CLN pay blocks until settle/cancel)
        errors: list[str] = []

        def _pay_via_api(url: str, bolt11: str, idx: int) -> None:
            try:
                r = httpx.post(f"{url}/pay_invoice", json={"bolt11": bolt11}, timeout=120)
                r.raise_for_status()
            except Exception as exc:
                errors.append(f"P{idx}: {exc}")

        threads = [
            threading.Thread(target=_pay_via_api, args=(url, inv["bolt11"], i), daemon=True)
            for i, (url, inv) in enumerate(zip(p_urls, invoices))
        ]
        for t in threads:
            t.start()

        # Wait for all HTLCs to be locked (detected via channel HTLC inspection)
        _wait_accepted(cln_coord, payment_hashes, errors=errors)

        # Pay the winner via regular invoice
        pot_msat = CONTRIBUTION_SATS * n * 1000
        r = httpx.post(
            f"{p_urls[winner_idx]}/create_invoice",
            json={"amount_msat": pot_msat, "label": _unique_label(f"pot-{round_label}")},
            timeout=30,
        )
        r.raise_for_status()
        cln_coord.pay(r.json()["bolt11"])

        # Settle all hold invoices — reveals each preimage, unblocking payment threads.
        # BoltzExchange/hold: settleholdinvoice takes preimage (not payment_hash).
        for p in preimages:
            cln_coord.settle_holdinvoice(p.hex())

        for t in threads:
            t.join(timeout=60)

        assert not errors, f"Payment errors: {errors}"

        # Wait for all channel balance updates to commit.  CLN propagates
        # update_fulfill_htlc to each participant asynchronously; polling until
        # all balances stabilise avoids reading mid-settlement values.
        p_bal_after = _wait_balances_stable(cln_ps, coord_id)

        # Verify balances (1 sat / 1000 msat tolerance for routing fees)
        for i in range(n):
            if i == winner_idx:
                expected = p_bal_before[i] - CONTRIBUTION_SATS * 1000 + pot_msat
            else:
                expected = p_bal_before[i] - CONTRIBUTION_SATS * 1000
            assert abs(p_bal_after[i] - expected) <= 1000, (
                f"P{i} balance: expected ~{expected} msat, got {p_bal_after[i]} msat"
            )

        return p_bal_after

    def test_round_p0_wins(self, ln_env):
        """Round 0: P0 wins the pot."""
        self._run_round(ln_env, winner_idx=0, round_label="r0")

    def test_round_p1_wins(self, ln_env):
        """Round 1: P1 wins the pot."""
        self._run_round(ln_env, winner_idx=1, round_label="r1")

    def test_round_p2_wins(self, ln_env):
        """Round 2: P2 wins the pot (all three rounds complete)."""
        self._run_round(ln_env, winner_idx=2, round_label="r2")


class TestLNFallbackCancel:
    """Fallback: coordinator cancels hold invoices before any participant pays."""

    def test_cancel_before_payment(self, ln_env):
        """Hold invoices can be cancelled; no sats move."""
        cln_coord = ln_env["cln_coord"]
        cln_ps    = ln_env["cln_ps"]
        coord_id  = ln_env["coord_id"]

        n = len(cln_ps)
        # Separate preimage per invoice
        preimages     = [os.urandom(32) for _ in range(n)]
        payment_hashes = [hashlib.sha256(p).hexdigest() for p in preimages]
        labels        = [_unique_label(f"cancel-test-p{i}") for i in range(n)]

        # Wait for any previous round's deferred balance updates to settle
        # before capturing the baseline, so p_bal_before is a stable value.
        p_bal_before = _wait_balances_stable(cln_ps, coord_id)

        # Create invoices but DON'T pay them
        for i in range(n):
            cln_coord.holdinvoice(
                payment_hash_hex=payment_hashes[i],
                amount_msat=CONTRIBUTION_SATS * 1000,
            )

        # Cancel each invoice by payment_hash
        for ph in payment_hashes:
            cln_coord.cancel_holdinvoice(ph)

        # Verify no HTLCs remain — this is the authoritative check for a cancelled
        # invoice (no HTLCs were ever sent, so absence is immediate).
        time.sleep(0.5)
        held = cln_coord.get_incoming_htlc_hashes()
        assert not (held & set(payment_hashes)), (
            f"HTLCs still held after cancel: {held & set(payment_hashes)}"
        )
        # No payments were made, so balances must be identical to baseline.
        p_bal_after = _wait_balances_stable(cln_ps, coord_id)
        assert p_bal_after == p_bal_before, (
            f"Balances changed after cancel (no payments): before={p_bal_before} after={p_bal_after}"
        )

    def test_cancel_after_partial_payment(self, ln_env):
        """
        If not all participants pay in time, coordinator cancels.
        The paying participant gets their HTLC returned.
        """
        cln_coord = ln_env["cln_coord"]
        cln_ps    = ln_env["cln_ps"]
        coord_id  = ln_env["coord_id"]
        p_urls    = ln_env["p_urls"]
        n         = len(cln_ps)

        # Separate preimage per invoice
        preimages     = [os.urandom(32) for _ in range(n)]
        payment_hashes = [hashlib.sha256(p).hexdigest() for p in preimages]
        labels        = [_unique_label(f"partial-p{i}") for i in range(n)]

        # Wait for deferred updates from previous rounds before taking baseline.
        p_bal_before = _wait_balances_stable(cln_ps, coord_id)

        # Create invoices
        invoices = []
        for i in range(n):
            inv = cln_coord.holdinvoice(
                payment_hash_hex=payment_hashes[i],
                amount_msat=CONTRIBUTION_SATS * 1000,
            )
            invoices.append(inv)

        # Only P0 pays
        errors: list[str] = []

        def _pay(url: str, bolt11: str) -> None:
            try:
                httpx.post(f"{url}/pay_invoice", json={"bolt11": bolt11}, timeout=120)
            except Exception as exc:
                errors.append(str(exc))

        t = threading.Thread(target=_pay, args=(p_urls[0], invoices[0]["bolt11"]), daemon=True)
        t.start()

        # Wait for P0's HTLC to be accepted (detect via channel HTLC inspection)
        deadline = time.time() + ACCEPT_TIMEOUT
        while time.time() < deadline:
            if payment_hashes[0] in cln_coord.get_incoming_htlc_hashes():
                break
            time.sleep(0.5)

        # Cancel P0's invoice — their HTLC is returned
        cln_coord.cancel_holdinvoice(payment_hashes[0])
        # Cancel the remaining unpaid invoices
        for ph in payment_hashes[1:]:
            cln_coord.cancel_holdinvoice(ph)
        t.join(timeout=30)

        # Wait for the update_fail_htlc to propagate back to P0 before asserting.
        # CLN returns a cancelled HTLC asynchronously; polling until stable
        # ensures we read the final settled value.
        p_bal_after = _wait_balances_stable(cln_ps, coord_id)
        assert p_bal_after == p_bal_before, (
            f"Balances changed after cancel: before={p_bal_before} after={p_bal_after}"
        )


class TestLNNodeInfo:
    """Participant API /node_info endpoint returns valid CLN node data."""

    def test_all_participants_have_node_info(self, ln_env):
        p_ids = ln_env["p_ids"]
        for i, (url, expected_id) in enumerate(zip(ln_env["p_urls"], p_ids)):
            r = httpx.get(f"{url}/node_info", timeout=10)
            assert r.status_code == 200, f"P{i} /node_info failed: {r.text}"
            data = r.json()
            assert data["id"] == expected_id, f"P{i} id mismatch"
