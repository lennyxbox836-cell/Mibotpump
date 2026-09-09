#!/usr/bin/env python3
"""
pump_scanner.py - Monitor de tokens nuevos en pump.fun via WebSocket.
Analiza y alerta. NO compra, NO firma transacciones, NO usa claves privadas.
"""

import asyncio, json, sys, time, platform, subprocess
from collections import defaultdict
from datetime import datetime

try:
    import websockets
except ImportError:
    sys.exit("Falta la libreria. Instala con:  pip install websockets")

WS_URL = "wss://pumpportal.fun/api/data"

# ---------------- CONFIGURACION ----------------
VENTANA_SEG      = 300     # tiempo de observacion por token antes de evaluar
MIN_VMC          = 1.0     # volumen / market cap minimo
MIN_TRADERS      = 50      # wallets unicas minimas
MAX_TX_POR_TRADER= 15.0    # por encima = probable wash trading
MIN_TX_POR_TRADER= 1.5     # por debajo = volumen sin participacion
MAX_CONCENTRACION= 0.15    # % maximo del volumen en la wallet top
MAX_TOKENS_VIVOS = 400     # limite de memoria
# -----------------------------------------------

class Token:
    __slots__ = ("mint","nombre","simbolo","t0","vol_sol","mcap_sol",
                 "traders","tx","vol_por_wallet")
    def __init__(self, mint, nombre, simbolo, mcap):
        self.mint = mint
        self.nombre = nombre
        self.simbolo = simbolo
        self.t0 = time.time()
        self.vol_sol = 0.0
        self.mcap_sol = mcap or 0.0
        self.traders = set()
        self.tx = 0
        self.vol_por_wallet = defaultdict(float)

    @property
    def edad(self):
        return time.time() - self.t0

    def registrar(self, wallet, sol, mcap):
        self.vol_sol += sol
        self.tx += 1
        if wallet:
            self.traders.add(wallet)
            self.vol_por_wallet[wallet] += sol
        if mcap:
            self.mcap_sol = mcap

    def metricas(self):
        n = len(self.traders) or 1
        vmc = self.vol_sol / self.mcap_sol if self.mcap_sol > 0 else 0.0
        top = max(self.vol_por_wallet.values()) if self.vol_por_wallet else 0.0
        conc = top / self.vol_sol if self.vol_sol > 0 else 1.0
        return {
            "vmc": vmc,
            "traders": len(self.traders),
            "tx": self.tx,
            "tx_por_trader": self.tx / n,
            "concentracion": conc,
            "vol_sol": self.vol_sol,
            "mcap_sol": self.mcap_sol,
        }

    def evaluar(self):
        m = self.metricas()
        fallos = []
        if m["vmc"] < MIN_VMC:
            fallos.append(f"V/MC {m['vmc']:.2f} < {MIN_VMC}")
        if m["traders"] < MIN_TRADERS:
            fallos.append(f"traders {m['traders']} < {MIN_TRADERS}")
        if m["tx_por_trader"] > MAX_TX_POR_TRADER:
            fallos.append(f"tx/trader {m['tx_por_trader']:.1f} (wash trading)")
        if m["tx_por_trader"] < MIN_TX_POR_TRADER:
            fallos.append(f"tx/trader {m['tx_por_trader']:.1f} (sin participacion)")
        if m["concentracion"] > MAX_CONCENTRACION:
            fallos.append(f"top wallet {m['concentracion']*100:.0f}% del volumen")
        return m, fallos


def alerta_sonora():
    sys.stdout.write("\a"); sys.stdout.flush()
    s = platform.system()
    try:
        if s == "Darwin":
            subprocess.Popen(["afplay","/System/Library/Sounds/Glass.aiff"],
                             stderr=subprocess.DEVNULL)
        elif s == "Linux":
            subprocess.Popen(["paplay","/usr/share/sounds/freedesktop/stereo/complete.oga"],
                             stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        elif s == "Windows":
            import winsound; winsound.Beep(880, 400)
    except Exception:
        pass


def notificar(token, m):
    txt = (f"{token.simbolo} | V/MC {m['vmc']:.2f} | "
           f"{m['traders']} traders | conc {m['concentracion']*100:.0f}%")
    s = platform.system()
    try:
        if s == "Linux":
            subprocess.Popen(["notify-send","PASA FILTROS", txt],
                             stderr=subprocess.DEVNULL)
        elif s == "Darwin":
            subprocess.Popen(["osascript","-e",
                f'display notification "{txt}" with title "PASA FILTROS"'],
                stderr=subprocess.DEVNULL)
    except Exception:
        pass


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


async def main():
    vivos = {}
    revisados = 0
    aprobados = 0

    log(f"Conectando a {WS_URL}")
    async for ws in websockets.connect(WS_URL, ping_interval=20, ping_timeout=20):
        try:
            await ws.send(json.dumps({"method":"subscribeNewToken"}))
            log("Suscrito a tokens nuevos. Ctrl+C para salir.\n")

            while True:
                raw = await ws.recv()
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                mint = d.get("mint")
                if not mint:
                    continue

                if d.get("txType") == "create" or mint not in vivos:
                    if mint in vivos:
                        continue
                    if len(vivos) >= MAX_TOKENS_VIVOS:
                        viejo = min(vivos, key=lambda k: vivos[k].t0)
                        vivos.pop(viejo, None)
                    vivos[mint] = Token(
                        mint,
                        d.get("name","?"),
                        d.get("symbol","?"),
                        d.get("marketCapSol", 0.0),
                    )
                    await ws.send(json.dumps(
                        {"method":"subscribeTokenTrade","keys":[mint]}))
                else:
                    t = vivos.get(mint)
                    if t:
                        t.registrar(
                            d.get("traderPublicKey"),
                            float(d.get("solAmount", 0) or 0),
                            d.get("marketCapSol"),
                        )

                # evaluar los que cumplieron la ventana
                for k in [k for k,v in vivos.items() if v.edad >= VENTANA_SEG]:
                    t = vivos.pop(k)
                    m, fallos = t.evaluar()
                    revisados += 1
                    if not fallos:
                        aprobados += 1
                        print("\n" + "="*58)
                        print(f"  PASA FILTROS   {t.simbolo}  ({t.nombre})")
                        print("="*58)
                        print(f"  mint          {t.mint}")
                        print(f"  V/MC          {m['vmc']:.2f}")
                        print(f"  traders       {m['traders']}")
                        print(f"  tx            {m['tx']}  ({m['tx_por_trader']:.1f}/trader)")
                        print(f"  concentracion {m['concentracion']*100:.1f}%")
                        print(f"  volumen       {m['vol_sol']:.2f} SOL")
                        print(f"  mcap          {m['mcap_sol']:.2f} SOL")
                        print(f"  pump.fun/{t.mint}")
                        print("="*58 + "\n")
                        alerta_sonora()
                        notificar(t, m)
                    if revisados % 25 == 0:
                        tasa = aprobados/revisados*100
                        log(f"revisados {revisados} | aprobados {aprobados} ({tasa:.1f}%) | siguiendo {len(vivos)}")

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
