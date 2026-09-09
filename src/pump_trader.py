#!/usr/bin/env python3
"""
pump_trader.py - Firma y envia compras/ventas en pump.fun.

Usa la Local Transaction API de PumpPortal: ellos arman la transaccion
(interactua con la bonding curve de pump.fun), pero se firma aca mismo
con tu clave y se manda por tu propio RPC. La clave privada nunca viaja
a ningun servidor de terceros.

No usa la API "Lightning" de PumpPortal (esa es custodial: depositas SOL
en una wallet de ellos). Esto es intencional.
"""

import base64
import os
import sys

import requests
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")


def cargar_wallet() -> Keypair:
    clave = os.environ.get("SOLANA_PRIVATE_KEY")
    if not clave:
        sys.exit(
            "Falta SOLANA_PRIVATE_KEY. Exportala antes de correr el bot:\n"
            "  export SOLANA_PRIVATE_KEY='tu_clave_privada_base58'\n"
            "Nunca la pongas en el codigo ni la commitees."
        )
    try:
        return Keypair.from_base58_string(clave.strip())
    except Exception as e:
        sys.exit(f"SOLANA_PRIVATE_KEY invalida: {e}")


def obtener_balance_sol(pubkey) -> float:
    r = requests.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [str(pubkey)]},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"RPC error al leer balance: {data['error']}")
    return data["result"]["value"] / 1_000_000_000


def _armar_transaccion(pubkey, accion, mint, cantidad, denominado_en_sol,
                        slippage_pct, priority_fee_sol, pool):
    payload = {
        "publicKey": str(pubkey),
        "action": accion,  # "buy" | "sell"
        "mint": mint,
        "amount": cantidad,
        "denominatedInSol": "true" if denominado_en_sol else "false",
        "slippage": slippage_pct,
        "priorityFee": priority_fee_sol,
        "pool": pool,
    }
    r = requests.post(TRADE_LOCAL_URL, data=payload, timeout=10)
    r.raise_for_status()
    return r.content  # transaccion serializada, sin firmar


def _firmar_y_enviar(wallet: Keypair, tx_bytes: bytes) -> str:
    tx_sin_firmar = VersionedTransaction.from_bytes(tx_bytes)
    tx = VersionedTransaction(tx_sin_firmar.message, [wallet])

    resp = requests.post(
        RPC_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(bytes(tx)).decode("utf-8"),
                {"encoding": "base64", "skipPreflight": True, "maxRetries": 0},
            ],
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error al enviar tx: {data['error']}")
    return data["result"]


def comprar(wallet, mint, sol, slippage_pct=15, priority_fee_sol=0.0005,
            pool="pump", dry_run=True):
    return _ejecutar(wallet, "buy", mint, sol, True, slippage_pct,
                      priority_fee_sol, pool, dry_run)


def vender(wallet, mint, cantidad="100%", slippage_pct=15, priority_fee_sol=0.0005,
           pool="pump", dry_run=True):
    return _ejecutar(wallet, "sell", mint, cantidad, False, slippage_pct,
                      priority_fee_sol, pool, dry_run)


def _ejecutar(wallet, accion, mint, cantidad, denominado_en_sol,
              slippage_pct, priority_fee_sol, pool, dry_run):
    if dry_run:
        print(f"    [DRY_RUN] {accion.upper()} {mint} cantidad={cantidad} "
              f"slippage={slippage_pct}% fee={priority_fee_sol} SOL -- no se envio nada real")
        return {"dry_run": True, "signature": None}

    tx_bytes = _armar_transaccion(wallet.pubkey(), accion, mint, cantidad,
                                   denominado_en_sol, slippage_pct, priority_fee_sol, pool)
    firma = _firmar_y_enviar(wallet, tx_bytes)
    print(f"    tx enviada ({accion}): https://solscan.io/tx/{firma}")
    return {"dry_run": False, "signature": firma}
