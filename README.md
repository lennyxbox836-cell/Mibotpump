# pump-scanner

Monitor en tiempo real de tokens nuevos en pump.fun via WebSocket, mas
un bot opcional de compra/venta automatica.

Este repo trae dos herramientas independientes:

| Script | Que hace | Usa claves privadas / plata real |
|---|---|---|
| `src/pump_scanner.py` | Observa 300s, filtra por liquidez y wash trading, avisa | No. Solo analiza. |
| `src/pump_sniper.py`  | Filtro rapido (segundos) + compra automatica + venta por take-profit/stop-loss/tiempo | Si, cuando `DRY_RUN = False` |

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

## pump_sniper.py - compra y venta automatica

### API key de PumpPortal (requisito para que el filtro funcione)

Sin esto, el bot arranca y corre sin errores, pero **nunca va a comprar
nada**: se va a quedar viendo candidatos con 0 traders para siempre. No
es un bug -- PumpPortal cobra por los streams de trades reales
(`subscribeTokenTrade`, `subscribeAccountTrade`), a diferencia de
`subscribeNewToken` (deteccion de tokens nuevos) que es gratis. Sin API
key, esos dos streams no entregan datos y el filtro se queda ciego.

Es una **wallet y una clave separadas de tu `SOLANA_PRIVATE_KEY`** --
esta otra solo paga el streaming de datos (0.01 SOL cada 10.000 eventos),
nunca la usa este bot para comprar ni firmar nada.

1. Generar la wallet + API key (gratis, solo con un GET):
   ```bash
   curl -s https://pumpportal.fun/api/create-wallet
   ```
   Devuelve un JSON con `apiKey`, `walletPublicKey` y `privateKey`.
   **Guardá el `privateKey` en ese momento -- no se vuelve a mostrar.**
2. Mandarle SOL a `walletPublicKey` (0.02 SOL alcanza para arrancar, es
   solo para pagar el streaming, no para operar).
3. Exportar la key:
   ```bash
   export PUMPPORTAL_API_KEY='el_apiKey_del_paso_1'
   ```

Sin `PUMPPORTAL_API_KEY` seteada, el bot ahora **avisa explicitamente**
en el log al arrancar en vez de fallar en silencio.

---

`pump_scanner.py` espera 300s para juntar suficientes datos y evitar
tokens sin salida, pero en pump.fun la mayoria de los tokens pumpean y
caen dentro del primer minuto: para cuando termina esa ventana, la
oportunidad de entrada ya paso. `pump_sniper.py` resuelve esto al reves:
compra rapido con un filtro minimo, y pone la proteccion en la **salida**
automatica (take-profit, stop-loss o tiempo maximo, lo que ocurra primero).

**Como compra:** usa la Local Transaction API de PumpPortal, que arma la
transaccion sobre la bonding curve de pump.fun (no existe ruta de Jupiter
para tokens que todavia no graduaron a Raydium). La transaccion se firma
aca mismo, con tu clave, y se manda por tu propio RPC. La clave privada
nunca sale de tu maquina ni se manda a ningun servidor de terceros. (Existe
tambien una API "Lightning" de PumpPortal que es custodial -- depositas SOL
en una wallet de ellos -- y **no se usa** en este bot a proposito.)

**Filtro rapido (segundos, no minutos):**

| Filtro | Que detecta |
|---|---|
| El creador del token vendio | Senal mas clara de rug pull que existe |
| `traders` minimos en la ventana | Que no sea un solo wallet operando solo |
| Concentracion de volumen | Que no sea una sola wallet inflando el volumen |

**Salida automatica**, la primera condicion que se cumpla:

- `TAKE_PROFIT_MULT` -- vende cuando el market cap sube ese multiplo
- `STOP_LOSS_MULT` -- vende cuando cae a ese multiplo
- `MAX_HOLD_SEG` -- vende igual pasado ese tiempo, para no quedarse
  esperando una salida que no llega
- El trader copiado vendio (ver "Copy-trading" abajo) -- tiene prioridad
  sobre las otras tres, se ejecuta al instante sin importar el multiplo

**Copy-trading (opcional):** ademas del filtro propio, el bot puede
seguir las compras/ventas de wallets puntuales via `PUMP_COPY_WALLETS`
(direcciones de Solana separadas por coma):

```bash
PUMP_COPY_WALLETS=direccionWallet1,direccionWallet2 python pump_sniper.py
```

Cuando una de esas wallets compra un token, el bot lo compra tambien casi
al instante (salteando la ventana de 8s del filtro propio -- la idea es
copiar rapido, no re-analizar), usando el `pool="auto"` de PumpPortal por
si el token ya migro a Raydium. Cuando esa wallet vende, el bot vende esa
posicion en el mismo instante, sin esperar take-profit/stop-loss/tiempo.
En el dashboard, las posiciones y el historial muestran su `origen`
(`filtro` o `copy:<wallet corta>`) para distinguir de donde vino cada una.

Riesgos propios de esto, que el filtro cuantitativo no tiene: no hay
forma de saber si esa wallet es genuinamente buena, es el equipo del
propio token operando su holding para atraer copiadores, o si la senal
llega publica y otros bots la copian al mismo tiempo que el tuyo (lo que
puede mover el precio en tu contra antes de que tu compra confirme). Sin
`PUMP_COPY_WALLETS` seteada (default), esto no hace nada -- se usa
unicamente el filtro propio, como antes.

**Ranking de wallets rentables (para decidir a quien copiar):** el bot
arma solo, en vivo, una tabla de las wallets mas rentables que fue
observando -- se ve en el dashboard, seccion "Wallets mas rentables
observadas". No es un scan del historial completo de pump.fun ni usa
ninguna fuente externa: se arma exclusivamente con los trades que el
bot mismo va viendo mientras corre (sobre todo los primeros segundos de
cada token nuevo, mas la vida completa de lo que compra o copia), asi
que tarda en juntar muestra y solo ve una porcion del mercado real.

Una wallet entra al ranking cuando el bot observa que **cierra un ciclo
completo** en un mint (compra y despues vende hasta volver a 0 tokens) --
ahi se calcula el SOL neto de ese ciclo a partir de los montos reales de
las transacciones, sin estimaciones. Con menos de `MIN_OPERACIONES_RANKING`
(3) ciclos cerrados observados, la wallet no aparece -- muy poca muestra
para decir nada. La tabla no agrega nada a `PUMP_COPY_WALLETS`
automaticamente: muestra la direccion completa para que la copies vos a
mano si te parece que vale la pena, con el mismo ojo critico que cualquier
otra wallet que decidas seguir.

**Circuit breaker:** si el balance de la wallet cae mas de
`PERDIDA_MAX_SESION_SOL` desde el inicio de la sesion, el bot deja de
abrir posiciones nuevas (las que ya estan abiertas se siguen manejando).

**Saldo ficticio con Kelly (solo en DRY_RUN):** en modo simulacion, el bot
arranca con `SALDO_FICTICIO_INICIAL_USD` (default $25) y dimensiona cada
operacion simulada con el [criterio de Kelly](https://es.wikipedia.org/wiki/Criterio_de_Kelly),
calculado con la tasa de acierto y el ratio ganancia/perdida de las
propias operaciones simuladas de esa sesion -- no es un numero inventado,
pero tampoco confiable con pocas muestras. Por eso:

- Con menos de `MIN_MUESTRAS_KELLY` (10) operaciones cerradas, usa un
  tamano fijo chico (`APUESTA_INICIAL_PCT`, 2% del saldo) en vez de Kelly.
- A partir de ahi, aplica **medio-Kelly** (`KELLY_FRACCION = 0.5`) con un
  tope duro de `MAX_KELLY_PCT` (20% del saldo por operacion).
- Se resta `FRICCION_PCT` (3%) del retorno simulado de cada operacion, como
  estimado grosero de slippage + fees ida y vuelta.

Esto es **para ver como se comporta el crecimiento del saldo con
dimensionamiento dinamico antes de arriesgar plata real**, no una
recomendacion de sizing para produccion. El Kelly "de libro" asume una
distribucion de retornos razonablemente bien comportada; pump.fun es lo
opuesto (la mayoria pierde casi todo, pocos ganan mucho), asi que incluso
con la fraccion y el tope aplicados, un estimado de `p` y `b` basado en
20-30 operaciones puede no sostenerse en las siguientes 200. Pasar esta
logica a plata real (`DRY_RUN = False`) es una decision aparte que este
bot no toma sola -- `SOL_POR_COMPRA` en real sigue siendo un monto fijo,
manual, deliberadamente conservador.

**Dashboard en tiempo real:** al arrancar, `pump_sniper.py` levanta un
servidor web de solo lectura en `PUERTO_DASHBOARD` (default `8080`) --
`http://localhost:8080` muestra el saldo (real o ficticio), el precio
SOL/USD, los candidatos en ventana de filtro, las posiciones abiertas con
su multiplo actual, y el historial de operaciones cerradas, todo
actualizandose solo cada 1.5s. En GitHub Codespaces no hace falta
configurar nada: el puerto se reenvia automaticamente, aparece un aviso
o se ve en la pestaña `PORTS` con un link para abrirlo en el navegador.
El dashboard no controla nada del bot, solo lee su estado.

**Salida event-driven, no por polling:** la condicion de salida se evalua
en el mismo instante en que llega el dato de precio de un trade nuevo, no
en el siguiente chequeo periodico. El barrido de fondo (cada 0.5s) es solo
una red de seguridad para el caso de `MAX_HOLD_SEG` cuando un token se
queda sin trades (sin volumen, tampoco llegarian eventos que disparen la
salida). Las llamadas de red (compra/venta) corren en un hilo aparte para
no trabar el procesamiento de otros tokens mientras una transaccion esta
en vuelo.

**Sobre "ganarle" a los dumps -- limites reales, no de software:** ningun
diseño de bot elimina el riesgo de que te vendan encima. Aunque la
deteccion sea instantanea del lado del cliente, tu venta sigue atada a:
la latencia del feed de PumpPortal hasta que te llega el evento, el
viaje de ida y vuelta para armar la transaccion, y el tiempo de bloque de
Solana (~400ms) hasta que tu venta queda incluida. Si un wallet grande
larga una venta que se lleva puesta toda la liquidez en un solo bloque,
tu bot puede reaccionar "al instante" y aun asi llegar tarde -- no hay
transaccion que viaje mas rapido que el bloque que ya se cerro. Lo que
esta mejora reduce es el retraso que agregaba el propio codigo (hasta 1s
de antes), no la latencia de red ni la posibilidad de quedar del lado
perdedor de un dump. Un RPC de baja latencia (Helius, Triton, etc.) pesa
mas en esto que cualquier ajuste de codigo.

### Uso

El modo (simulado o real) se controla por variable de entorno, **no hay
que editar el codigo para alternar entre uno y otro**.

**Simulado (default, sin nada seteado):**

```bash
python src/pump_sniper.py
```

`SOLANA_PRIVATE_KEY` es opcional en este modo: si falta o esta mal
escrita, el bot genera una wallet temporal solo para poder simular y
arranca igual (avisa en el log que lo hizo). Loguea que compraria/venderia
pero no manda nada a la red ni gasta un solo lamport.

**Real:**

```bash
export SOLANA_PRIVATE_KEY='tu_clave_privada_base58'   # nunca la escribas en el codigo
export SOLANA_RPC_URL='https://tu-rpc-rapido.com'      # opcional, usa uno publico por defecto
export PUMP_LIVE=1
python src/pump_sniper.py
```

Hacen falta **las dos cosas** para operar real: `SOLANA_PRIVATE_KEY`
valida Y `PUMP_LIVE=1`. Es a proposito -- tener la clave exportada de una
prueba anterior en la terminal no alcanza para que empiece a gastar SOL
solo; falta el segundo interruptor explicito. Sin `PUMP_LIVE=1` (o con
cualquier otro valor), corre en modo simulado sin importar si la clave
esta seteada o no. Para volver a simulado despues de haber operado real,
alcanza con `unset PUMP_LIVE` (o `export PUMP_LIVE=0`) en esa terminal.

**Para ver la mecanica de compra/venta funcionar mas seguido** (en vez de
solo descartes) sin editar el codigo, bajar el filtro de traders con
`PUMP_MIN_TRADERS`:

```bash
PUMP_MIN_TRADERS=1 python pump_sniper.py
```

Esto compra en tokens con muy poca o ninguna participacion real -- sirve
para ver el dashboard funcionando de punta a punta, pero **no** para
juzgar si la estrategia es buena: con el filtro relajado se compra
literalmente lo que el filtro de 5 traders existe para evitar. Sin la
variable (o con cualquier otro numero), sigue en 5 como siempre.

### Configuracion

Todos los parametros estan al inicio de `src/pump_sniper.py`. `DRY_RUN`
en particular NO se edita a mano ahi -- se define solo a partir de
`PUMP_LIVE` (ver "Uso" arriba):

```python
DRY_RUN = ...                   # NO tocar: sale de la env var PUMP_LIVE
FILTRO_RAPIDO_SEG   = 8
MIN_TRADERS_RAPIDO  = 5          # override: env var PUMP_MIN_TRADERS
MAX_CONCENTRACION   = 0.5
SOL_POR_COMPRA       = 0.02
SLIPPAGE_PCT         = 20
TAKE_PROFIT_MULT     = 1.8
STOP_LOSS_MULT       = 0.6
MAX_HOLD_SEG         = 90
MAX_POSICIONES_ABIERTAS = 3
PERDIDA_MAX_SESION_SOL  = 0.5
```

### Advertencias especificas de este script

- **Usa dinero real cuando `DRY_RUN = False`.** Probalo primero en dry
  run, despues con `SOL_POR_COMPRA` chico, antes de subir el monto.
- El filtro rapido es deliberadamente mas laxo que el de
  `pump_scanner.py` -- es la contrapartida de comprar en segundos en vez
  de minutos. No elimina el riesgo, solo saca el caso mas obvio (dev
  vendiendo).
- Un RPC publico puede ser lento; en pump.fun la velocidad de ejecucion
  importa. Si vas en serio, un RPC dedicado (Helius, QuickNode, etc.)
  ayuda mas que ajustar cualquier otro parametro.
- El PnL logueado es una estimacion en base al market cap, no la plata
  efectivamente recibida (eso depende del slippage real de la bonding
  curve en el momento de la venta).
- Todo lo que dice la seccion "Sobre las expectativas" mas abajo aplica
  igual o peor aca: comprar mas rapido no cambia que la mayoria de estos
  tokens pierden valor, solo cambia en que momento entras y salis.

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
