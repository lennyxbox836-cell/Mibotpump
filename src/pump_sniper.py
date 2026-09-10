#!/usr/bin/env python3
"""
pump_sniper.py - Snipe de tokens nuevos en pump.fun con compra y venta
automatica.

A diferencia de pump_scanner.py (que solo observa 300s antes de opinar),
este bot decide en segundos: la mayoria de los tokens de pump.fun pumpean
y caen dentro del primer minuto, asi que esperar el analisis completo
significa comprar despues de la caida. Aca el filtro es minimo mientras
se compra rapido, y la proteccion pasa a la SALIDA: take-profit,
stop-loss y un tiempo maximo de holdeo, lo que se cumpla primero.

Modo simulado o real: se decide por la variable de entorno PUMP_LIVE, NO
hay que tocar el codigo para alternar entre uno y otro.

  - Sin PUMP_LIVE (o con cualquier valor que no sea 1/true/si): DRY_RUN.
    No hace falta SOLANA_PRIVATE_KEY -- si falta o esta mal, se genera
    una wallet temporal solo para poder simular.
  - PUMP_LIVE=1: modo real. Ademas hace falta una SOLANA_PRIVATE_KEY
    valida con fondos -- si no esta, el bot corta con error antes de
    operar (no hay wallet temporal para esto).

Los dos interruptores son independientes a proposito: tener la clave
exportada de una prueba anterior no alcanza para operar real, hace falta
ademas poner PUMP_LIVE=1 explicitamente.

USA DINERO REAL cuando PUMP_LIVE=1. Empeza con montos chicos.
"""

import asyncio
import collections
import json
import os
import sys
import time
from datetime import datetime

try:
    import websockets
except ImportError:
    sys.exit("Falta la libreria. Instala con:  pip install -r requirements.txt")

import dashboard
import pump_trader

WS_URL = "wss://pumpportal.fun/api/data"

# ---------------- CONFIGURACION ----------------
# DRY_RUN se define por la variable de entorno PUMP_LIVE (ver docstring
# de arriba). No cambies esto a mano en el codigo -- usa PUMP_LIVE=1.
DRY_RUN = os.environ.get("PUMP_LIVE", "").strip().lower() not in ("1", "true", "si", "yes")

FILTRO_RAPIDO_SEG   = 8      # ventana de observacion antes de decidir comprar
# PUMP_MIN_TRADERS permite relajar el filtro sin editar el codigo (por
# ejemplo para ver la mecanica de compra/venta funcionar mas seguido en
# DRY_RUN). El default (5) es el valor pensado para juzgar la estrategia
# de verdad -- un valor bajo compra en tokens con poca o ninguna
# participacion real, que es justamente lo que este filtro busca evitar.
MIN_TRADERS_RAPIDO  = int(os.environ.get("PUMP_MIN_TRADERS", "5"))
MAX_CONCENTRACION   = 0.5    # % maximo del volumen inicial en una sola wallet

SOL_POR_COMPRA       = 0.02  # SOL que arriesga cada compra
SLIPPAGE_PCT         = 20
PRIORITY_FEE_SOL     = 0.0005
POOL                 = "pump"   # tokens nuevos: siempre en la bonding curve
POOL_COPY            = "auto"   # copy-trading: el token seguido puede ya haber graduado a Raydium

# Wallets a copiar (ademas del filtro propio), separadas por coma:
#   PUMP_COPY_WALLETS=wallet1,wallet2 python pump_sniper.py
# Vacio por defecto = no se copia a nadie, se usa solo el filtro propio.
WALLETS_SEGUIDAS = [w.strip() for w in os.environ.get("PUMP_COPY_WALLETS", "").split(",") if w.strip()]

TAKE_PROFIT_MULT     = 1.8   # vende al +80% de mcap sobre el precio de entrada
STOP_LOSS_MULT       = 0.6   # vende al -40%
MAX_HOLD_SEG         = 90    # vende si llego a este tiempo sin tocar los anteriores

MAX_POSICIONES_ABIERTAS = 3
PERDIDA_MAX_SESION_SOL  = 0.5   # circuit breaker: si el balance cae esto desde el inicio, dejar de abrir posiciones

MAX_CANDIDATOS_VIVOS = 300

# --- ranking de wallets observadas (para encontrar a quien copiar) ---
MAX_WALLETS_RASTREADAS  = 5000  # limite de memoria, se descartan las mas viejas
MIN_OPERACIONES_RANKING = 3     # ciclos completos (compra->venta) minimos para entrar al ranking

# --- saldo ficticio + Kelly (SOLO afecta el dimensionamiento en DRY_RUN) ---
SALDO_FICTICIO_INICIAL_USD = 25.0
KELLY_FRACCION       = 0.5    # medio-Kelly: Kelly completo apuesta demasiado en un mercado de colas gordas como este
MAX_KELLY_PCT        = 0.20   # tope duro, nunca mas del 20% del saldo ficticio en una sola operacion
MIN_MUESTRAS_KELLY   = 10     # con menos operaciones cerradas, el estimado de Kelly no es confiable
APUESTA_INICIAL_PCT  = 0.02   # tamano fijo usado mientras no hay suficientes muestras
APUESTA_MINIMA_USD   = 1.0    # piso: nunca apostar menos que esto (si el saldo ficticio alcanza)
FRICCION_PCT         = 0.03   # estimado de slippage + fees ida y vuelta, se resta del retorno simulado
SOL_USD_FALLBACK     = 150.0  # se usa si falla la consulta de precio en vivo

PUERTO_DASHBOARD = 8080   # dashboard web de solo lectura en http://localhost:<puerto>
HISTORIAL_MAX    = 100    # cantidad de operaciones cerradas que se guardan para el dashboard
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


class ActividadWallet:
    """
    Estadisticas de una wallet armadas en vivo a partir del mismo feed de
    trades que el bot ya recibe -- no es una fuente de datos historica
    aparte, asi que solo ve lo que el bot mismo observa mientras corre
    (sobre todo los primeros segundos de cada token nuevo, mas la vida
    completa de lo que el bot compra o copia). Tarda en juntar muestra.

    Un ciclo se da por cerrado cuando el propio evento de trade reporta
    "newTokenBalance" en ~0 para esa wallet en ese mint -- ahi se registra
    el SOL neto acumulado de ese ciclo (compras restan, ventas suman).
    """

    def __init__(self):
        self.t0 = time.time()
        self.sol_neto_por_mint = {}
        self.operaciones_cerradas = []  # SOL neto de cada ciclo cerrado (positivo = gano)
        self.volumen_total_sol = 0.0

    def registrar(self, mint, tipo, sol, nuevo_balance_tokens):
        self.volumen_total_sol += sol
        neto = self.sol_neto_por_mint.get(mint, 0.0) + (-sol if tipo == "buy" else sol)
        if nuevo_balance_tokens is not None and abs(nuevo_balance_tokens) < 1e-3:
            self.operaciones_cerradas.append(neto)
            self.sol_neto_por_mint.pop(mint, None)
        else:
            self.sol_neto_por_mint[mint] = neto

    @property
    def pnl_sol(self):
        return sum(self.operaciones_cerradas)

    @property
    def tasa_acierto(self):
        n = len(self.operaciones_cerradas)
        return sum(1 for x in self.operaciones_cerradas if x > 0) / n if n else 0.0


class BilleteraSimulada:
    """
    Saldo ficticio en USD para probar dimensionamiento por Kelly en DRY_RUN,
    sin arriesgar nada real. Kelly se calcula con la tasa de acierto y el
    ratio ganancia/perdida de las propias operaciones simuladas de la
    sesion -- no es un numero inventado, pero tampoco es confiable con
    pocas muestras, por eso MIN_MUESTRAS_KELLY existe.

    Advertencia: en un mercado de colas gordas como pump.fun (la mayoria
    pierde casi todo, pocos ganan mucho), el Kelly "de libro" tiende a
    sobre-apostar porque asume una distribucion mejor comportada que la
    real. Por eso se aplica KELLY_FRACCION (medio-Kelly) y un tope duro
    (MAX_KELLY_PCT). Esto es una herramienta de simulacion para ver como
    se comporta el crecimiento del saldo, no una recomendacion para
    dimensionar operaciones con plata real.
    """

    def __init__(self, saldo_inicial_usd):
        self.saldo_usd = saldo_inicial_usd
        self.resultados = []  # multiplicadores netos de cada operacion cerrada

    def kelly_fraccionario(self):
        n = len(self.resultados)
        if n < MIN_MUESTRAS_KELLY:
            return APUESTA_INICIAL_PCT
        ganadoras = [r - 1 for r in self.resultados if r > 1]
        perdedoras = [1 - r for r in self.resultados if r <= 1]
        if not ganadoras or not perdedoras:
            return APUESTA_INICIAL_PCT
        p = len(ganadoras) / n
        b = (sum(ganadoras) / len(ganadoras)) / (sum(perdedoras) / len(perdedoras))
        if b <= 0:
            return 0.0
        kelly = max(p - (1 - p) / b, 0.0) * KELLY_FRACCION
        return min(kelly, MAX_KELLY_PCT)

    def tamano_apuesta_usd(self):
        if self.saldo_usd <= 0:
            return 0.0
        monto = self.saldo_usd * self.kelly_fraccionario()
        piso = min(APUESTA_MINIMA_USD, self.saldo_usd)  # no apostar mas de lo que hay
        return max(monto, piso)

    def cerrar_operacion(self, apuesta_usd, multiplo_neto):
        pnl = apuesta_usd * (multiplo_neto - 1)
        self.saldo_usd += pnl
        self.resultados.append(multiplo_neto)
        return pnl


class Posicion:
    def __init__(self, mint, simbolo, mcap_entrada, sol_invertido, apuesta_usd=None, origen="filtro"):
        self.mint = mint
        self.simbolo = simbolo
        self.mcap_entrada = mcap_entrada or 1e-9
        self.mcap_actual = self.mcap_entrada
        self.sol_invertido = sol_invertido
        self.apuesta_usd = apuesta_usd  # solo se usa para liquidar contra la billetera simulada
        self.origen = origen  # "filtro" o "copy:<wallet corta>"
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
    """
    Las llamadas de red (PumpPortal + RPC) son bloqueantes (usan `requests`),
    asi que corren en un hilo aparte via run_in_executor: la venta se dispara
    apenas llega el evento de trade que confirma la razon de salida, sin
    esperar el proximo tick del barrido y sin trabar el loop mientras esa
    venta esta en vuelo (para poder seguir reaccionando a otras posiciones
    en paralelo).
    """

    def __init__(self, wallet):
        self.wallet = wallet
        self.candidatos = {}
        self.posiciones = {}
        self.saldo_inicial = None
        self.ws = None
        self._tareas = set()
        self.precio_sol_usd = SOL_USD_FALLBACK
        self.billetera = BilleteraSimulada(SALDO_FICTICIO_INICIAL_USD) if DRY_RUN else None
        self.historial = collections.deque(maxlen=HISTORIAL_MAX)
        self.wallets_seguidas = set(WALLETS_SEGUIDAS)
        self.actividad_wallets = {}

    def _lanzar(self, coro):
        tarea = asyncio.create_task(coro)
        self._tareas.add(tarea)
        tarea.add_done_callback(self._tareas.discard)
        return tarea

    async def circuito_abierto(self):
        """True si se puede seguir comprando (no se toco el limite de perdida)."""
        if DRY_RUN or self.saldo_inicial is None:
            return True
        loop = asyncio.get_running_loop()
        try:
            saldo = await loop.run_in_executor(None, pump_trader.obtener_balance_sol, self.wallet.pubkey())
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

    async def comprar(self, mint, simbolo, mcap, pool=POOL, origen="filtro"):
        if len(self.posiciones) >= MAX_POSICIONES_ABIERTAS or mint in self.posiciones:
            return
        if not await self.circuito_abierto():
            return

        apuesta_usd = None
        if self.billetera:
            apuesta_usd = self.billetera.tamano_apuesta_usd()
            if apuesta_usd <= 0:
                log("saldo ficticio agotado, no se simulan mas compras")
                return
            monto_sol = apuesta_usd / self.precio_sol_usd
        else:
            monto_sol = SOL_POR_COMPRA

        # Reservar el lugar ANTES de cualquier await: evita que dos compras
        # concurrentes pasen el chequeo de MAX_POSICIONES_ABIERTAS a la vez.
        self.posiciones[mint] = Posicion(mint, simbolo, mcap, monto_sol, apuesta_usd, origen)

        extra = ""
        if self.billetera:
            extra = (f" (${apuesta_usd:.2f} ficticios, kelly {self.billetera.kelly_fraccionario()*100:.1f}%, "
                     f"saldo ${self.billetera.saldo_usd:.2f})")
        etiqueta = "COPIANDO" if origen.startswith("copy:") else "COMPRANDO"
        log(f"{etiqueta} {simbolo} ({mint}) por {monto_sol:.4f} SOL{extra}"
            + (f" -- {origen}" if origen.startswith("copy:") else ""))

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, pump_trader.comprar, self.wallet, mint, monto_sol,
                SLIPPAGE_PCT, PRIORITY_FEE_SOL, pool, DRY_RUN)
        except Exception as e:
            log(f"ERROR al comprar {simbolo}: {e}")
            self.posiciones.pop(mint, None)

    async def vender(self, mint, razon):
        pos = self.posiciones.pop(mint, None)
        if not pos:
            return
        log(f"VENDIENDO {pos.simbolo} ({razon})")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, pump_trader.vender, self.wallet, mint, "100%",
                SLIPPAGE_PCT, PRIORITY_FEE_SOL, POOL, DRY_RUN)
        except Exception as e:
            log(f"ERROR al vender {pos.simbolo}: {e} -- se reintentara con el proximo trade/tick")
            self.posiciones[mint] = pos
            return

        pnl_usd = None
        if self.billetera and pos.apuesta_usd is not None:
            multiplo_neto = pos.multiplo * (1 - FRICCION_PCT)
            pnl_usd = self.billetera.cerrar_operacion(pos.apuesta_usd, multiplo_neto)
            log(f"    [SIMULADO] PnL ${pnl_usd:+.2f} -- saldo ficticio ${self.billetera.saldo_usd:.2f} "
                f"({len(self.billetera.resultados)} operaciones)")

        self.historial.append({
            "t": time.time(), "simbolo": pos.simbolo, "razon": razon, "origen": pos.origen,
            "multiplo": round(pos.multiplo, 3), "pnl_usd": round(pnl_usd, 2) if pnl_usd is not None else None,
        })

        if self.ws:
            try:
                await self.ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
            except Exception:
                pass

    def estado(self):
        """Snapshot para el dashboard web. Solo lectura, no muta nada."""
        return {
            "dry_run": DRY_RUN,
            "wallet": str(self.wallet.pubkey()),
            "precio_sol_usd": self.precio_sol_usd,
            "saldo_ficticio": self.billetera.saldo_usd if self.billetera else None,
            "operaciones_simuladas": len(self.billetera.resultados) if self.billetera else None,
            "kelly_pct": self.billetera.kelly_fraccionario() * 100 if self.billetera else None,
            "candidatos_en_ventana": [
                {"simbolo": c.simbolo, "mint": c.mint, "edad": round(c.edad, 1), "traders": len(c.traders)}
                for c in self.candidatos.values()
            ],
            "posiciones_abiertas": [
                {"simbolo": p.simbolo, "mint": p.mint, "multiplo": round(p.multiplo, 3),
                 "edad": round(p.edad, 1), "origen": p.origen}
                for p in self.posiciones.values()
            ],
            "historial": list(self.historial)[::-1],
            "wallets_seguidas": [w[:4] + ".." + w[-4:] for w in self.wallets_seguidas],
            "wallets_rastreadas": len(self.actividad_wallets),
            "top_wallets": self.top_wallets(),
        }

    def top_wallets(self, n=10):
        candidatas = [
            (w, act) for w, act in self.actividad_wallets.items()
            if len(act.operaciones_cerradas) >= MIN_OPERACIONES_RANKING
        ]
        candidatas.sort(key=lambda item: item[1].pnl_sol, reverse=True)
        return [
            {
                "wallet": w,
                "pnl_sol": round(act.pnl_sol, 4),
                "operaciones": len(act.operaciones_cerradas),
                "tasa_acierto": round(act.tasa_acierto * 100, 1),
                "volumen_sol": round(act.volumen_total_sol, 2),
            }
            for w, act in candidatas[:n]
        ]

    async def barrer(self):
        while True:
            await asyncio.sleep(0.5)

            for mint in [k for k, c in self.candidatos.items() if c.edad >= FILTRO_RAPIDO_SEG]:
                cand = self.candidatos.pop(mint)
                ok, motivo = cand.pasa_filtro()
                if ok:
                    self._lanzar(self.comprar(cand.mint, cand.simbolo, cand.mcap))
                else:
                    log(f"descartado {cand.simbolo}: {motivo}")
                    if self.ws:
                        try:
                            await self.ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]}))
                        except Exception:
                            pass

            # Red de seguridad para MAX_HOLD_SEG: sin esto, un token que dejo
            # de tener trades (sin liquidez para vender ni para nadie) nunca
            # generaria un evento que dispare la salida por tiempo.
            for mint, pos in list(self.posiciones.items()):
                razon = pos.razon_de_salida()
                if razon:
                    self._lanzar(self.vender(mint, razon))

    def procesar_evento(self, d):
        """Devuelve un mint si hace falta suscribirse a sus trades, o None."""
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
            return mint

        wallet = d.get("traderPublicKey")
        sol = float(d.get("solAmount", 0) or 0)
        mcap = d.get("marketCapSol")
        tipo = "sell" if d.get("txType") == "sell" else "buy"

        # Ranking de wallets: corre para TODOS los trades que vemos, sin
        # importar el resto de la logica de abajo (filtro propio o copy).
        if wallet:
            act = self.actividad_wallets.get(wallet)
            if act is None:
                if len(self.actividad_wallets) >= MAX_WALLETS_RASTREADAS:
                    mas_vieja = min(self.actividad_wallets, key=lambda w: self.actividad_wallets[w].t0)
                    self.actividad_wallets.pop(mas_vieja, None)
                act = self.actividad_wallets[wallet] = ActividadWallet()
            act.registrar(mint, tipo, sol, d.get("newTokenBalance"))

        # Senal de copy-trading: tiene prioridad sobre el filtro propio.
        if wallet in self.wallets_seguidas:
            corto = wallet[:4] + ".." + wallet[-4:]
            if tipo == "sell" and mint in self.posiciones:
                self._lanzar(self.vender(mint, f"trader seguido {corto} vendio"))
            elif tipo == "buy" and mint not in self.posiciones:
                self.candidatos.pop(mint, None)  # no hace falta esperar el filtro propio
                simbolo = d.get("symbol") or (mint[:6] + "..")
                self._lanzar(self.comprar(mint, simbolo, mcap, pool=POOL_COPY, origen=f"copy:{corto}"))
                return mint  # necesita suscripcion para poder vigilar la salida despues
            return None

        if mint in self.candidatos:
            self.candidatos[mint].registrar(wallet, sol, mcap, tipo)
        elif mint in self.posiciones and mcap:
            # Se evalua la salida ACA, en el mismo instante en que llega el
            # dato de precio -- no se espera al barrido periodico.
            pos = self.posiciones[mint]
            pos.mcap_actual = mcap
            razon = pos.razon_de_salida()
            if razon:
                self._lanzar(self.vender(mint, razon))
        return None


async def main():
    if DRY_RUN:
        log("=== DRY_RUN activo: no se va a mandar NINGUNA transaccion real ===")
    else:
        log("=== DRY_RUN desactivado: este bot va a gastar SOL real ===")

    wallet = pump_trader.cargar_wallet(dry_run=DRY_RUN)
    log(f"wallet: {wallet.pubkey()}")

    bot = Bot(wallet)
    await bot.iniciar_saldo()

    if bot.billetera:
        loop = asyncio.get_running_loop()
        precio = await loop.run_in_executor(None, pump_trader.obtener_precio_sol_usd)
        if precio:
            bot.precio_sol_usd = precio
        log(f"precio SOL/USD: ${bot.precio_sol_usd:.2f} "
            f"{'(en vivo)' if precio else '(fallback, no se pudo consultar)'}")
        log(f"saldo ficticio inicial: ${bot.billetera.saldo_usd:.2f}")

    await dashboard.iniciar(bot, PUERTO_DASHBOARD, log=log)

    log(f"Conectando a {WS_URL}")
    async for ws in websockets.connect(WS_URL, ping_interval=20, ping_timeout=20):
        try:
            bot.ws = ws
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
            log("Suscrito a tokens nuevos.")
            if bot.wallets_seguidas:
                await ws.send(json.dumps({"method": "subscribeAccountTrade", "keys": list(bot.wallets_seguidas)}))
                cortos = ", ".join(w[:4] + ".." + w[-4:] for w in bot.wallets_seguidas)
                log(f"Copiando trades de: {cortos}")
            log("Ctrl+C para salir.\n")

            tarea_barrido = asyncio.create_task(bot.barrer())
            try:
                while True:
                    raw = await ws.recv()
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    mint_a_suscribir = bot.procesar_evento(d)
                    if mint_a_suscribir:
                        await ws.send(json.dumps(
                            {"method": "subscribeTokenTrade", "keys": [mint_a_suscribir]}))
            finally:
                tarea_barrido.cancel()
                bot.ws = None

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
