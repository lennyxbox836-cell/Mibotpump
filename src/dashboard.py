#!/usr/bin/env python3
"""
dashboard.py - Servidor web local de solo lectura para ver el estado del
bot en tiempo real (candidatos, posiciones abiertas, saldo ficticio,
historial de operaciones). No recibe input del usuario ni controla al
bot -- es una ventana de vidrio hacia `bot.estado()`.
"""

from aiohttp import web

PAGINA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>pump_sniper</title>
<style>
  :root { color-scheme: dark; }
  body { background:#0b0e14; color:#d8dee9; font:14px/1.4 -apple-system,Segoe UI,sans-serif; margin:0; padding:20px; }
  h1 { font-size:16px; margin:0 0 4px; }
  .sub { color:#8b95a7; font-size:12px; margin-bottom:16px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }
  .dry { background:#1e3a1e; color:#7ee787; }
  .live { background:#3a1e1e; color:#ff7b72; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin-bottom:20px; }
  .stat { background:#151a24; border:1px solid #232a38; border-radius:6px; padding:10px 12px; }
  .stat .v { font-size:20px; font-weight:600; }
  .stat .l { color:#8b95a7; font-size:11px; text-transform:uppercase; }
  table { width:100%; border-collapse:collapse; margin-bottom:24px; font-size:13px; }
  th { text-align:left; color:#8b95a7; font-size:11px; text-transform:uppercase; padding:4px 8px; border-bottom:1px solid #232a38; }
  td { padding:5px 8px; border-bottom:1px solid #181d28; }
  tr:hover td { background:#121620; }
  .pos { color:#7ee787; }
  .neg { color:#ff7b72; }
  h2 { font-size:13px; color:#8b95a7; text-transform:uppercase; margin:0 0 8px; }
  .vacio { color:#5a6474; font-style:italic; padding:8px; }
  a { color:#58a6ff; }
</style>
</head>
<body>
  <h1>pump_sniper <span id="modo" class="badge">...</span></h1>
  <div class="sub" id="wallet"></div>
  <div class="sub" id="wallets-seguidas"></div>

  <div class="grid" id="stats"></div>

  <h2>Candidatos en ventana de filtro</h2>
  <div id="candidatos"></div>

  <h2>Posiciones abiertas</h2>
  <div id="posiciones"></div>

  <h2>Historial reciente</h2>
  <div id="historial"></div>

  <h2>Wallets mas rentables observadas (para copiar)</h2>
  <div class="sub" id="wallets-rastreadas-info"></div>
  <div id="top-wallets"></div>

<script>
function fmt(n, d=2) { return (n === null || n === undefined) ? "-" : Number(n).toFixed(d); }

async function actualizar() {
  let r;
  try { r = await (await fetch("/api/estado")).json(); }
  catch (e) { return; }

  const modo = document.getElementById("modo");
  modo.textContent = r.dry_run ? "DRY RUN" : "LIVE";
  modo.className = "badge " + (r.dry_run ? "dry" : "live");
  document.getElementById("wallet").textContent = "wallet: " + r.wallet + "  |  SOL/USD: $" + fmt(r.precio_sol_usd);

  let stats = "";
  if (r.saldo_ficticio !== null) {
    stats += stat("Saldo ficticio", "$" + fmt(r.saldo_ficticio));
    stats += stat("Operaciones simuladas", r.operaciones_simuladas);
    stats += stat("Kelly actual", fmt(r.kelly_pct, 1) + "%");
  }
  stats += stat("Candidatos en ventana", r.candidatos_en_ventana.length);
  stats += stat("Posiciones abiertas", r.posiciones_abiertas.length);
  document.getElementById("stats").innerHTML = stats;

  document.getElementById("candidatos").innerHTML = tabla(
    ["Simbolo", "Mint", "Edad (s)", "Traders"],
    r.candidatos_en_ventana.map(c => [c.simbolo, corto(c.mint), fmt(c.edad,1), c.traders])
  );

  document.getElementById("posiciones").innerHTML = tabla(
    ["Simbolo", "Mint", "Multiplo", "Edad (s)", "Origen"],
    r.posiciones_abiertas.map(p => [p.simbolo, corto(p.mint), claseMultiplo(p.multiplo), fmt(p.edad,1), p.origen])
  );

  document.getElementById("historial").innerHTML = tabla(
    ["Hora", "Simbolo", "Razon", "Multiplo", "PnL", "Origen"],
    r.historial.map(h => [
      new Date(h.t * 1000).toLocaleTimeString(),
      h.simbolo, h.razon, claseMultiplo(h.multiplo),
      h.pnl_usd === null ? "-" : signo(h.pnl_usd), h.origen || "-"
    ])
  );

  const wSeguidas = document.getElementById("wallets-seguidas");
  wSeguidas.textContent = r.wallets_seguidas.length
    ? "Copiando: " + r.wallets_seguidas.join(", ")
    : "No se esta copiando a ninguna wallet (PUMP_COPY_WALLETS vacio)";

  document.getElementById("wallets-rastreadas-info").textContent =
    r.wallets_rastreadas + " wallets observadas hasta ahora -- entran al ranking con 3+ operaciones cerradas";
  document.getElementById("top-wallets").innerHTML = tabla(
    ["Wallet (completa, para copiar)", "PnL (SOL)", "Operaciones", "Tasa acierto", "Volumen (SOL)"],
    r.top_wallets.map(w => [w.wallet, signoSol(w.pnl_sol), w.operaciones, fmt(w.tasa_acierto,0) + "%", fmt(w.volumen_sol)])
  );
}

function stat(l, v) {
  return `<div class="stat"><div class="v">${v}</div><div class="l">${l}</div></div>`;
}
function corto(mint) { return mint ? mint.slice(0,4) + "..." + mint.slice(-4) : "-"; }
function claseMultiplo(m) {
  const cls = m >= 1 ? "pos" : "neg";
  return `<span class="${cls}">x${fmt(m,2)}</span>`;
}
function signo(v) {
  const cls = v >= 0 ? "pos" : "neg";
  return `<span class="${cls}">${v >= 0 ? "+" : ""}$${fmt(v)}</span>`;
}
function signoSol(v) {
  const cls = v >= 0 ? "pos" : "neg";
  return `<span class="${cls}">${v >= 0 ? "+" : ""}${fmt(v,4)} SOL</span>`;
}
function tabla(cabeceras, filas) {
  if (!filas.length) return '<div class="vacio">nada por ahora</div>';
  const th = cabeceras.map(c => `<th>${c}</th>`).join("");
  const trs = filas.map(f => "<tr>" + f.map(c => `<td>${c}</td>`).join("") + "</tr>").join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`;
}

actualizar();
setInterval(actualizar, 1500);
</script>
</body>
</html>
"""


def crear_app(bot):
    app = web.Application()

    async def index(request):
        return web.Response(text=PAGINA, content_type="text/html")

    async def api_estado(request):
        return web.json_response(bot.estado())

    app.router.add_get("/", index)
    app.router.add_get("/api/estado", api_estado)
    return app


async def iniciar(bot, puerto, log=print):
    app = crear_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", puerto)
    await site.start()
    log(f"dashboard en http://0.0.0.0:{puerto}  "
        f"(en Codespaces: pestaña PORTS -> abrir en el navegador)")
    return runner
