#!/usr/bin/env python3
"""
pump_sniper.py - Snipe de tokens nuevos en pump.fun con compra y venta
automatica.

A diferencia de pump_scanner.py (que solo observa 300s antes de opinar),
este bot decide en segundos: la mayoria de los tokens de pump.fun pumpean
y caen dentro del primer minuto, asi que esperar el analisis completo
significa comprar despues de la caida. Ac"a el filtro es minimo mientras
se compra rapido, y la proteccion pasa a la SALIDA: take-profit,
stop-loss y un tiempo maximo de holdeo, lo que se cumpla primero.

Requiere SOLANA_PRIVATE_KEY en el entorno. Por defecto corre en DRY_RUN
(no manda transacciones reales) hasta que lo desactives a proposito.

USA DINERO REAL cuando DRY_RUN = False. Empeza con montos chicos.
"""

import asyncio
import json
import sys
import time
from datetime import datetime

try:
    import websockets
except ImportError:
    sys.exit("Falta la libreria. Instala con:  pip install -r requirements.txt")

import pump_trader

WS_URL = "wss://pumpportal.fun/api/data"

# ---------------- CONFIGURACION ----------------
DRY_RUN = True              # False = manda transacciones reales. EMPEZA EN True.

FILTRO_RAPIDO_SEG   = 8      # ventana de observacion antes de decidir comprar
MIN_TRADERS_RAPIDO  = 5      # wallets unicas minimas en esa ventana
MAX_CONCENTRACION   = 0.5    # % maximo del volumen inicial en una sola wallet

SOL_POR_COMPRA       = 0.02  # SOL que arriesga cada compra
SLIPPAGE_PCT         = 20
PRIORITY_FEE_SOL     = 0.0005
POOL                 = "pump"

TAKE_PROFIT_MULT     = 1.8   # vende al +80% de mcap sobre el precio de entrada
STOP_LOSS_MULT       = 0.6   # vende al -40%
MAX_HOLD_SEG         = 90    # vende si llego a este tiempo sin tocar los anteriores

MAX_POSICIONES_ABIERTAS = 3
PERDIDA_MAX_SESION_SOL  = 0.5   # circuit breaker: si el balance cae esto desde el inicio, dejar de abrir posiciones

MAX_CANDIDATOS_VIVOS = 300
# -----------------------------------------------


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class Candidato:
    """Token en ventana de observacion, todavia sin comprar."""

    def __init__(self, mint, nombre, simbolo, creador, mcap):
        self.mint = mint
        self.nombre = nombre
        self.simbolo = simbolo
        self.creador = creador
        self.t0 = time.time()
        self.mcap = mcap or 0.0
        self.traders = set()
        self.vol_por_wallet = {}
        self.vol_total = 0.0
        self.dev_vendio = False

    @property
    def edad(self):
        return time.time() - self.t0

    def registrar(self, wallet, sol, mcap, tipo):
        if wallet == self.creador and tipo == "sell":
            self.dev_vendio = True
        self.vol_total += sol
        if wallet:
            self.traders.add(wallet)
            self.vol_por_wallet[wallet] = self.vol_por_wallet.get(wallet, 0.0) + sol
        if mcap:
            self.mcap = mcap

    def pasa_filtro(self):
        if self.dev_vendio:
            return False, "el creador vendio (rug)"
        if len(self.traders) < MIN_TRADERS_RAPIDO:
            return False, f"traders {len(self.traders)} < {MIN_TRADERS_RAPIDO}"
        top = max(self.vol_por_wallet.values()) if self.vol_por_wallet else 0.0
        conc = top / self.vol_total if self.vol_total > 0 else 1.0
        if conc > MAX_CONCENTRACION:
            return False, f"concentracion {conc*100:.0f}%"
        return True, None


class Posicion:
    def __init__(self, mint, simbolo, mcap_entrada, sol_invertido):
        self.mint = mint
        self.simbolo = simbolo
        self.mcap_entrada = mcap_entrada or 1e-9
        self.mcap_actual = self.mcap_entrada
        self.sol_invertido = sol_invertido
        self.t_compra = time.time()

    @property
    def edad(self):
        return time.time() - self.t_compra

    @property
    def multiplo(self):
        return self.mcap_actual / self.mcap_entrada

    def razon_de_salida(self):
        if self.multiplo >= TAKE_PROFIT_MULT:
            return f"take-profit x{self.multiplo:.2f}"
        if self.multiplo <= STOP_LOSS_MULT:
            return f"stop-loss x{self.multiplo:.2f}"
        if self.edad >= MAX_HOLD_SEG:
            return f"tiempo maximo ({self.edad:.0f}s) x{self.multiplo:.2f}"
        return None


class Bot:
    def __init__(self, wallet):
        self.wallet = wallet
        self.candidatos = {}
        self.posiciones = {}
        self.saldo_inicial = None

    def circuito_abierto(self):
        """True si se puede seguir comprando (no se toco el limite de perdida)."""
        if DRY_RUN or self.saldo_inicial is None:
            return True
        try:
            saldo = pump_trader.obtener_balance_sol(self.wallet.pubkey())
        except Exception as e:
            log(f"no se pudo leer balance ({e}), no abro posiciones nuevas por las dudas")
            return False
        if saldo <= self.saldo_inicial - PERDIDA_MAX_SESION_SOL:
            log(f"CIRCUIT BREAKER: saldo {saldo:.3f} SOL, "
                f"limite de perdida de sesion alcanzado. No se abren mas posiciones.")
            return False
        return True

    async def iniciar_saldo(self):
        if DRY_RUN:
            return
        self.saldo_inicial = pump_trader.obtener_balance_sol(self.wallet.pubkey())
        log(f"saldo inicial: {self.saldo_inicial:.4f} SOL")

    def comprar(self, cand):
        if len(self.posiciones) >= MAX_POSICIONES_ABIERTAS:
            return
        if not self.circuito_abierto():
            return
        log(f"COMPRANDO {cand.simbolo} ({cand.mint}) por {SOL_POR_COMPRA} SOL")
        try:
            pump_trader.comprar(self.wallet, cand.mint, SOL_POR_COMPRA,
                                 SLIPPAGE_PCT, PRIORITY_FEE_SOL, POOL, DRY_RUN)
        except Exception as e:
            log(f"ERROR al comprar {cand.simbolo}: {e}")
            return
        self.posiciones[cand.mint] = Posicion(cand.mint, cand.simbolo, cand.mcap, SOL_POR_COMPRA)

    def vender(self, mint, razon):
        pos = self.posiciones.pop(mint, None)
        if not pos:
            return
        log(f"VENDIENDO {pos.simbolo} ({razon})")
        try:
            pump_trader.vender(self.wallet, mint, "100%",
                                SLIPPAGE_PCT, PRIORITY_FEE_SOL, POOL, DRY_RUN)
        except Exception as e:
            log(f"ERROR al vender {pos.simbolo}: {e}")

    async def barrer(self, ws):
        while True:
            await asyncio.sleep(1)

            for mint in [k for k, c in self.candidatos.items() if c.edad >= FILTRO_RAPIDO_SEG]:
                cand = self.candidatos.pop(mint)
                ok, motivo = cand.pasa_filtro()
                if ok:
                    self.comprar(cand)
                else:
                    log(f"descartado {cand.simbolo}: {motivo}")
                if mint not in self.posiciones:
                    try:
                        await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
                    except Exception:
                        pass

            for mint, pos in list(self.posiciones.items()):
                razon = pos.razon_de_salida()
                if razon:
                    self.vender(mint, razon)
                    try:
                        await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
                    except Exception:
                        pass

    def procesar_evento(self, d):
        mint = d.get("mint")
        if not mint:
            return None

        if d.get("txType") == "create":
            if len(self.candidatos) >= MAX_CANDIDATOS_VIVOS:
                viejo = min(self.candidatos, key=lambda k: self.candidatos[k].t0)
                self.candidatos.pop(viejo, None)
            self.candidatos[mint] = Candidato(
                mint, d.get("name", "?"), d.get("symbol", "?"),
                d.get("traderPublicKey"), d.get("marketCapSol", 0.0),
            )
            return "nuevo"

        wallet = d.get("traderPublicKey")
        sol = float(d.get("solAmount", 0) or 0)
        mcap = d.get("marketCapSol")
        tipo = "sell" if d.get("txType") == "sell" else "buy"

        if mint in self.candidatos:
            self.candidatos[mint].registrar(wallet, sol, mcap, tipo)
        elif mint in self.posiciones and mcap:
            self.posiciones[mint].mcap_actual = mcap
        return None


async def main():
    if DRY_RUN:
        log("=== DRY_RUN activo: no se va a mandar NINGUNA transaccion real ===")
    else:
        log("=== DRY_RUN desactivado: este bot va a gastar SOL real ===")

    wallet = pump_trader.cargar_wallet()
    log(f"wallet: {wallet.pubkey()}")

    bot = Bot(wallet)
    await bot.iniciar_saldo()

    log(f"Conectando a {WS_URL}")
    async for ws in websockets.connect(WS_URL, ping_interval=20, ping_timeout=20):
        try:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            log("Suscrito a tokens nuevos. Ctrl+C para salir.\n")

            tarea_barrido = asyncio.create_task(bot.barrer(ws))
            try:
                while True:
                    raw = await ws.recv()
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    if bot.procesar_evento(d) == "nuevo":
                        await ws.send(json.dumps(
                            {"method": "subscribeTokenTrade", "keys": [d["mint"]]}))
            finally:
                tarea_barrido.cancel()

        except websockets.ConnectionClosed:
            log("Conexion cerrada. Reintentando en 5s...")
            await asyncio.sleep(5)
            continue
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDetenido.")
