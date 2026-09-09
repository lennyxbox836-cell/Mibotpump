# pump-scanner

Monitor en tiempo real de tokens nuevos en pump.fun via WebSocket.
Aplica filtros cuantitativos de liquidez y deteccion de wash trading,
y avisa con sonido y notificacion cuando un token los pasa.

**Solo analiza.** No compra, no vende, no firma transacciones y no
usa claves privadas.

---

## Que resuelve

La mayoria de tokens de lanzadera son imposibles de vender despues de
comprarlos: market cap inflado sin volumen real detras. Este script
mide eso automaticamente sobre cientos de tokens mientras vos no
mirás la pantalla.

## Filtros

| Filtro | Umbral | Que detecta |
|---|---|---|
| `V/MC` | > 1.0 | Si hay volumen suficiente para poder salir |
| `traders` | >= 50 | Wallets unicas participando |
| `tx/trader` | < 15 | Wash trading (pocas wallets, muchas operaciones) |
| `tx/trader` | > 1.5 | Volumen sin participacion real |
| `concentracion` | < 15% | Cuanto del volumen viene de una sola wallet |

Se cuentan **wallets unicas**, no holders declarados. Un bot operando
consigo mismo infla las transacciones pero no la cantidad de traders,
y ahi es donde lo detecta el ratio `tx/trader`.

## Instalacion

```bash
git clone https://github.com/TU_USUARIO/pump-scanner.git
cd pump-scanner
pip install -r requirements.txt
python src/pump_scanner.py
```

### Termux (Android)

```bash
pkg install python
pip install websockets
python src/pump_scanner.py
```

## Configuracion

Los umbrales estan al inicio de `src/pump_scanner.py`:

```python
VENTANA_SEG       = 300    # segundos de observacion por token
MIN_VMC           = 1.0
MIN_TRADERS       = 50
MAX_TX_POR_TRADER = 15.0
MIN_TX_POR_TRADER = 1.5
MAX_CONCENTRACION = 0.15
```

## Salida

```
==========================================================
  PASA FILTROS   EJEMPLO  (Token de Ejemplo)
==========================================================
  mint          7xKX...pump
  V/MC          2.84
  traders       137
  tx            412  (3.0/trader)
  concentracion 8.2%
  volumen       94.31 SOL
  mcap          33.20 SOL
==========================================================
```

Cada 25 tokens revisados imprime la tasa de aprobacion acumulada.
Esperá que sea baja: **1-3%** es lo normal. Ese numero no es un fallo
del script, es la tasa base del mercado que esta midiendo.

## Limitaciones

- Depende de `pumpportal.fun`, un servicio de terceros que puede
  cambiar o caerse. Si no conecta, revisá `WS_URL`.
- No detecta si el equipo puede mintear mas tokens, si la liquidez
  esta bloqueada, ni si el contrato tiene funciones ocultas.
- **Pasar los filtros significa "se puede vender", no "va a subir".**

## Sobre las expectativas

Estos filtros reducen el riesgo de quedar atrapado en un token sin
salida. No cambian la distribucion de retornos de la categoria, que
es fuertemente negativa: en una simulacion Monte Carlo de 100.000
trayectorias sobre tokens de este tipo, la mediana de $100 invertidos
termina por debajo de $3.

La herramienta sirve para entender el mecanismo y para evitar trampas
evidentes. No es una estrategia rentable, y no deberia usarse como si
lo fuera.

## Licencia

MIT
