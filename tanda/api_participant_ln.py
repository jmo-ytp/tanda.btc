"""
FastAPI server for a Lightning Network tanda participant.

Each container runs one instance of this server backed by its own CLN node.
The coordinator communicates with participants exclusively via HTTP.

Environment variables:
  CLN_RPC_PATH — absolute path to the CLN unix socket
                 e.g. /cln-data/regtest/lightning-rpc
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .lnrpc import CLNRpc


@asynccontextmanager
async def lifespan(application: FastAPI):
    socket_path = os.environ["CLN_RPC_PATH"]
    application.state.cln = CLNRpc(socket_path)
    yield


app = FastAPI(title="Tanda LN Participant", lifespan=lifespan)


# ── Request / response models ──────────────────────────────────────────────────

class PayInvoiceRequest(BaseModel):
    bolt11: str


class CreateInvoiceRequest(BaseModel):
    amount_msat: int
    label: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Returns node status, id, and open channels."""
    info = app.state.cln.get_info()
    channels = app.state.cln.list_peer_channels()
    return {
        "status": "ok",
        "pubkey_hex": info["id"],
        "channels": channels,
    }


@app.get("/node_info")
def node_info():
    """Returns node id and first announced address (for coordinator to connect)."""
    info = app.state.cln.get_info()
    addresses = info.get("address", [])
    address = addresses[0] if addresses else {}
    return {"id": info["id"], "address": address}


@app.post("/pay_invoice")
def pay_invoice(req: PayInvoiceRequest):
    """
    Pay a hold invoice from the coordinator.
    The HTLC stays locked until the coordinator settles or cancels.
    """
    try:
        result = app.state.cln.pay(req.bolt11)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"payment failed: {exc}")
    return {"payment_hash": result.get("payment_hash", "")}


@app.post("/create_invoice")
def create_invoice(req: CreateInvoiceRequest):
    """Create a regular invoice so the coordinator can pay the winner."""
    try:
        result = app.state.cln.invoice(req.amount_msat, req.label, req.label)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"invoice creation failed: {exc}")
    return {"bolt11": result["bolt11"]}
