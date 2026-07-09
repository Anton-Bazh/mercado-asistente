/* EtiquetaFlow — lógica de frontend (JS vanilla, sin dependencias).
   Conecta el diseño con el backend real de Mercado Asistente. */

// ===== Estado =====
const state = {
  tab: 'cola',
  orders: [],            // ventas ready_to_ship (API), cada una con .pending y .due
  selected: new Set(),   // shipment_id marcados para el lote (dentro de "A imprimir")
  advanced: new Set(),   // shipment_id de "Próximos" adelantados manualmente a hoy
  printedToday: 0,       // contador de impresiones exitosas de hoy (backend)
  doneRecent: [],        // mini-historial reciente (columna Impresos)
  status: { configured: false, connected: false, site: '—', nickname: null,
            token_expires_in: null },
  format: localStorage.getItem('ef_format') || 'pdf',
  printer: localStorage.getItem('ef_printer') || '',
  operador: localStorage.getItem('ef_operador') || '',   // quién imprime (unificación con el Extractor)
  printers: [],
  showOffPrinters: false, // Dispositivos: ver las no disponibles (solo administrar)
  cups: false,
  batch: null,           // estado del lote en curso (progreso)
  riskTotal: 0,          // etiquetas en riesgo (para el aviso)
  auto: null,            // estado del modo automático (reglas)
  autoView: 'basica',    // 'basica' | 'avanzada'
  rulesDraft: null,      // copia de trabajo de las reglas (editor)
  manualSel: new Set(),  // pedidos multi-unidad seleccionados (Separación)
  accountsMeta: [],      // tiendas presentes en la cola (para el filtro)
  ordersErrors: [],      // tiendas que fallaron al leer
  storeFilter: 'all',    // filtro de tienda en la Cola
  searchQ: '',           // buscador de etiquetas de la Cola
  accounts: [],          // cuentas configuradas (pestaña Cuentas)
  providers: [],         // catálogo de proveedores
  lastSync: null,
  logFilter: 'info',     // nivel mínimo del visor de logs
  logRange: '24',        // horas hacia atrás ('0' = todo el log)
  logModule: '',         // módulo ('' = todos)
  logQuery: '',          // búsqueda de texto
  histFilter: { from: '', to: '', format: '', result: '' },
  stamp: '',
};

const TITLES = {
  cola: ['Cola en tiempo real', 'Las columnas siguen la fecha límite de Mercado Libre: hoy en «A imprimir», los siguientes días en «Próximos»'],
  separacion: ['Separación', 'Pedidos multi-unidad para gestión manual (separar o imprimir)'],
  automatico: ['Impresión automática', 'El servidor imprime solo, según las reglas de la semana'],
  dispositivos: ['Dispositivos', 'Destino de impresión disponible'],
  etiquetas: ['Etiquetas', 'Talón de control, vista previa del acomodo e importación de PDFs'],
  conexion: ['Cuentas / Tiendas', 'Conecta tus tiendas de cada marketplace'],
  historial: ['Historial de impresión', 'Registro persistente de todas las etiquetas impresas'],
  logs: ['Logs del sistema', 'Registro del servidor · últimas 24 h; usa los filtros para ver más'],
};

// ===== Utilidades =====
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    // detail puede ser texto (la mayoría de los errores) u objeto estructurado
    // (p. ej. el 409 de saldo negativo: {message, items}) — se preserva tal
    // cual en err.detail para que el llamador pueda distinguirlo sin perder
    // los datos (new Error() solo admite un string en el mensaje).
    const detail = data && data.detail;
    const err = new Error((typeof detail === 'string' && detail) || resp.statusText);
    err.status = resp.status;
    err.detail = detail;
    throw err;
  }
  return data;
}
function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function showBanner(msg, type = 'info') {
  const el = $('banner');
  const styles = { info:['#eef4ff','#2f56c0'], success:['#eaf6ef','#15824a'], error:['#fbe9e9','#c43232'] };
  const [bg, fg] = styles[type] || styles.info;
  el.style.background = bg; el.style.color = fg; el.textContent = msg; el.style.display = 'block';
  clearTimeout(showBanner._t);
  showBanner._t = setTimeout(() => { el.style.display = 'none'; }, 6000);
}
// Filtro por tienda (Cola). 'all' = todas.
function inStore(o) { return state.storeFilter === 'all' || String(o.account_id) === String(state.storeFilter); }
// ===== Buscador de etiquetas (Cola) =====
// Busca sin acentos ni mayúsculas; varias palabras = todas deben coincidir.
function deacc(s) {
  return String(s == null ? '' : s).toLowerCase()
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '');
}
function textMatches(haystack) {
  const words = deacc(state.searchQ).split(/\s+/).filter(Boolean);
  return words.every(w => haystack.includes(w));
}
// Campos por tienda: ML (venta, envío, pack, cliente, dirección, SKU),
// Walmart (además PO de la guía FedEx y tracking), TikTok (pedido, package id).
function matchesSearch(o) {
  if (!state.searchQ) return true;
  const parts = [o.order_id, o.shipment_id, o.pack_id, o.po, o.buyer_name,
                 o.address, o.account_name, o.total_amount];
  for (const p of (o.products || [])) parts.push(p.title, p.sku);
  return textMatches(deacc(parts.filter(v => v != null && v !== '').join(' ')));
}
function doneMatchesSearch(h) {
  if (!state.searchQ) return true;
  const parts = [h.shipment_id, h.order_id, h.buyer_name, h.product_summary, h.account];
  return textMatches(deacc(parts.filter(v => v != null && v !== '').join(' ')));
}
// Pool de pendientes por imprimir (excluye multi-unidad → van a Separación).
function pendingPool() { return state.orders.filter(o => o.pending && !o.multi_unit && inStore(o) && matchesSearch(o)); }
// Multi-unidad pendientes: gestión manual (separar/imprimir) en «Separación».
function manualList() { return state.orders.filter(o => o.pending && o.multi_unit); }
// Las columnas siguen el dato de ML (row.due, por fecha límite de despacho):
// A imprimir = hoy/vencidas (+ adelantadas a mano); Próximos = siguientes días.
// printable=false (en procesamiento/programada) nunca pasa a «A imprimir».
function isForToday(o) {
  if (o.printable === false) return false;
  return o.due !== 'upcoming' || state.advanced.has(String(o.shipment_id));
}
function nextList() {
  return pendingPool().filter(o => !isForToday(o))
    .sort((a, b) => String(a.handling_limit || '').localeCompare(String(b.handling_limit || '')));
}
function todayList() {
  const rank = { overdue: 0, today: 1 };
  return pendingPool().filter(isForToday)
    .sort((a, b) => (rank[a.due] ?? 1) - (rank[b.due] ?? 1));
}
function selectedList() { return todayList().filter(o => state.selected.has(String(o.shipment_id))); }
// Pools sin filtro de tienda, para los contadores por cuenta de cada columna.
function poolAllStores() { return state.orders.filter(o => o.pending && !o.multi_unit); }
function productSummary(o) {
  const p = (o.products && o.products[0]) || null;
  if (!p) return '—';
  const extra = o.products.length > 1 ? ` +${o.products.length - 1}` : '';
  return `${p.title} x${p.quantity}${extra}`;
}
// Metadatos por envío para impresión/historial (JSON keyed por shipment_id).
function buildMeta(orders) {
  const m = {};
  for (const o of orders) {
    m[String(o.shipment_id)] = {
      order_id: o.order_id, buyer_name: o.buyer_name,
      product_summary: productSummary(o),
      account_id: o.account_id, account_name: o.account_name,
    };
  }
  return JSON.stringify(m);
}

// ===== Acerca de (sello del sistema) =====
async function loadStamp() {
  try { const d = await api('/api/stamp'); state.stamp = d.stamp || ''; } catch {}
}
const STAMP_LOGO_SVG = '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="3" width="12" height="6" rx="1"></rect><path d="M6 14H4a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-2"></path><rect x="6" y="14" width="12" height="7" rx="1"></rect></svg>';
function showStamp({ title, msg, critical }) {
  const pre = $('stamp-art'); if (pre) { pre.textContent = state.stamp || '(sello no disponible)'; pre.style.color = critical ? '#c43232' : 'var(--accent)'; }
  const head = $('stamp-head'), icon = $('stamp-icon');
  head.style.background = critical ? '#fbe9e9' : 'var(--accent-tint)';
  icon.style.background = critical ? '#d33a3a' : 'var(--accent)';
  icon.innerHTML = critical ? '✕' : STAMP_LOGO_SVG;
  $('stamp-title').textContent = title || 'Acerca de EtiquetaFlow';
  $('stamp-msg').textContent = msg || '';
  $('stamp-msg').style.display = msg ? 'block' : 'none';
  // créditos del creador solo en el «Acerca de»; en fallo crítico estorban
  $('stamp-credits').style.display = critical ? 'none' : 'block';
  $('stamp-foot').textContent = critical ? 'EtiquetaFlow · sello de sistema'
    : '© 2026 Antonio Baeza · EtiquetaFlow para INMATMEX';
  $('stamp-overlay').style.display = 'flex';
}
function hideStamp() { $('stamp-overlay').style.display = 'none'; }
// Los eventos que el backend ya registra (impresión confirmada/bloqueada,
// lotes, altas de impresoras, cuentas…) NO se re-loguean desde el navegador:
// saldrían duplicados en la pestaña Logs. Aquí solo se loguea lo que únicamente
// el navegador conoce (respaldo en navegador, formato/impresora elegidos, etc.).
function showCritical(msg) {
  showStamp({ title: 'Fallo crítico de impresión', msg, critical: true });
}

// ===== Diagnóstico de impresora =====
function diagCatColor(cat) {
  return ({ OK:['#eaf6ef','#15824a'], Hardware:['#fbf2dd','#b07400'],
            Sistema:['#eef4ff','#2f56c0'], Software:['#efe9fb','#6d4ee0'],
            Revisar:['#fbe9e9','#c43232'] })[cat] || ['#eef0f3','#6a7280'];
}
function showDiag(d) {
  $('diag-sub').textContent = `${d.name} · ${d.make_model}`;
  const verdict = (d.verdict || []).map(v => {
    const [bg, fg] = diagCatColor(v.cat);
    return `<div style="display:flex;gap:8px;align-items:baseline;margin-top:6px"><span class="chip" style="background:${bg};color:${fg};flex:0 0 auto">${esc(v.cat)}</span><span style="font-size:12.5px;color:var(--ink3)">${esc(v.detail)}</span></div>`;
  }).join('');
  const rows = [
    ['Estado', d.state], ['state-reasons', d.state_reasons], ['Conexión', d.connection],
    ['URI dispositivo', d.uri], ['Interfaz / PPD', d.interface], ['Conexión CUPS', d.cups_connection],
    ['Modo de color', d.color_mode], ['Acepta trabajos', d.accepting], ['Habilitada desde', d.enabled_since],
  ];
  Object.entries(d.defaults || {}).forEach(([k, v]) => rows.push(['Predet. ' + k, v]));
  const table = rows.filter(r => r[1]).map(([k, v]) =>
    `<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--line)"><span style="flex:0 0 148px;font-size:12px;font-weight:600;color:var(--ink2)">${esc(k)}</span><span style="flex:1;min-width:0;font-size:12px;font-family:var(--mono);color:var(--ink3);word-break:break-all">${esc(v)}</span></div>`).join('');
  $('diag-body').innerHTML =
    `<div style="margin-bottom:14px;padding:12px 14px;border-radius:10px;background:#fafbfc;border:1px solid var(--line)"><div style="font-size:11px;font-weight:600;color:var(--label);letter-spacing:.4px">LECTURA DE DIAGNÓSTICO</div>${verdict}</div>${table}<div style="margin-top:10px;font-size:11px;color:var(--label)">Generado: ${esc(d.timestamp || '')}</div>`;
  $('diag-overlay').style.display = 'flex';
}
function hideDiag() { $('diag-overlay').style.display = 'none'; }

// ===== Logs (registro del servidor; los eventos del navegador se suman vía API) =====
function log(level, module, message) {
  const body = new FormData();
  body.set('level', level); body.set('module', module); body.set('message', message);
  fetch('/api/logs/client', { method: 'POST', body }).catch(() => {});
  if (state.tab === 'logs') { clearTimeout(log._t); log._t = setTimeout(renderLogs, 400); }
}
const LEVEL_STYLE = {
  DEBUG:   ['#eef0f4','#5b6472'],
  INFO:    ['rgba(47,107,240,.12)','#2f6bf0'],
  WARNING: ['#fbf2dd','#b07400'],
  ERROR:   ['#fbe9e9','#c43232'],
  CRITICAL:['#c43232','#ffffff'],
};
// Chips = nivel MÍNIMO mostrado; «Detalle» incluye el DEBUG (traza completa).
const LOG_LEVELS = [['info','Todos'],['warning','Avisos'],['error','Errores'],['debug','Detalle']];
const LOG_RANGES = [['24','Últimas 24 h'],['72','3 días'],['168','7 días'],['0','Todo el log']];
const SEL_STYLE = 'border:1px solid #d8dbe1;border-radius:9px;padding:7px 10px;font:inherit;font-size:12.5px;outline:none;background:#fff';

function paintLogChips() {
  $('log-chips').querySelectorAll('[data-level]').forEach(b => {
    const k = b.dataset.level, active = state.logFilter === k;
    const [bg,fg] = LEVEL_STYLE[k.toUpperCase()] || ['#fff','#5b6472'];
    b.style.cssText = active
      ? (k==='info' ? 'background:var(--accent);color:#fff;border-color:transparent'
                    : `background:${bg};color:${fg};border-color:transparent`)
      : '';
  });
}

function ensureLogFilters() {
  const box = $('log-filters');
  if (box.dataset.built) return;
  box.dataset.built = '1';
  box.innerHTML = `
    <span id="log-chips" style="display:flex;gap:8px;flex-wrap:wrap">${
      LOG_LEVELS.map(([k,label]) => `<button class="lfilter" data-level="${k}">${label}</button>`).join('')}</span>
    <select id="log-range" style="${SEL_STYLE}">${
      LOG_RANGES.map(([v,l]) => `<option value="${v}">${l}</option>`).join('')}</select>
    <select id="log-module" style="${SEL_STYLE};max-width:170px"><option value="">Todos los módulos</option></select>
    <input id="log-q" placeholder="Buscar en el log…" style="${SEL_STYLE};width:180px">
    <button class="btn btn-ghost" id="log-refresh" style="padding:7px 12px;font-size:12px">Actualizar</button>
    <span id="log-count" style="margin-left:auto;font-size:12px;color:var(--muted)"></span>`;
  paintLogChips();
  $('log-chips').addEventListener('click', e => {
    const b = e.target.closest('[data-level]');
    if (b) { state.logFilter = b.dataset.level; paintLogChips(); renderLogs(); }
  });
  $('log-range').addEventListener('change', e => { state.logRange = e.target.value; renderLogs(); });
  $('log-module').addEventListener('change', e => { state.logModule = e.target.value; renderLogs(); });
  $('log-q').addEventListener('input', e => {
    clearTimeout(ensureLogFilters._t);
    ensureLogFilters._t = setTimeout(() => { state.logQuery = e.target.value.trim(); renderLogs(); }, 300);
  });
  $('log-refresh').addEventListener('click', () => renderLogs());
}

async function renderLogs() {
  ensureLogFilters();
  const p = new URLSearchParams({ hours: state.logRange, level: state.logFilter,
                                  module: state.logModule, q: state.logQuery, limit: 400 });
  let data;
  try { data = await api('/api/logs?' + p); }
  catch (e) {
    $('log-rows').innerHTML = `<div class="empty"><span>No se pudo leer el log: ${esc(e.message)}</span></div>`;
    return;
  }
  // módulos disponibles en el rango (conserva la selección)
  $('log-module').innerHTML = `<option value="">Todos los módulos</option>` +
    (data.loggers || []).map(m =>
      `<option value="${esc(m)}"${m === state.logModule ? ' selected' : ''}>${esc(m)}</option>`).join('');
  const rangeLabel = (LOG_RANGES.find(r => r[0] === state.logRange) || [,'—'])[1];
  $('log-count').textContent = `${data.total} evento(s) · ${rangeLabel}` +
    (data.total > data.entries.length ? ` · mostrando los ${data.entries.length} más recientes` : '');
  if (!data.entries.length) {
    $('log-rows').innerHTML = `<div class="empty"><span>Sin eventos con estos filtros.</span>
      <span style="font-size:12px">Amplía el rango o baja el nivel (chip «Detalle»).</span></div>`;
    return;
  }
  const today = new Date().toDateString();
  $('log-rows').innerHTML = data.entries.map(l => {
    const d = new Date(l.ts * 1000);
    const hm = d.toLocaleTimeString('es-MX', { hour12: false });
    const time = d.toDateString() === today ? hm
      : d.toLocaleDateString('es-MX', { day: '2-digit', month: 'short' }) + ' ' + hm;
    const [bg,fg] = LEVEL_STYLE[l.level] || LEVEL_STYLE.INFO;
    const detail = l.detail ? `<details style="margin-top:4px"><summary style="cursor:pointer;font-size:11.5px;color:var(--muted)">traza completa</summary><pre style="margin:6px 0 0;font-size:11px;line-height:1.45;overflow-x:auto;background:#f7f8fa;padding:8px 10px;border-radius:8px">${esc(l.detail)}</pre></details>` : '';
    return `<div class="log-row" style="align-items:flex-start">
      <span style="width:112px;flex:0 0 auto;font-family:var(--mono);color:var(--muted2);font-size:12px">${time}</span>
      <span style="width:84px;flex:0 0 auto"><span class="lvl" style="background:${bg};color:${fg}">${l.level}</span></span>
      <span style="width:110px;flex:0 0 auto;font-family:var(--mono);font-size:12px;color:var(--ink3)">${esc(l.logger)}</span>
      <span style="flex:1;min-width:0;color:#2b303a;word-break:break-word">${esc(l.message)}${detail}</span></div>`;
  }).join('');
}

// ===== Impresión =====
function openLabelUrl(url, download) {
  if (download) {
    const a = document.createElement('a');
    a.href = url; a.download = ''; document.body.appendChild(a); a.click(); a.remove();
  } else {
    window.open(url, '_blank', 'noopener');
  }
}
function canServerPrint() { return state.printers.length > 0; }

// Refresca lo que depende del backend tras imprimir: cola (para que 'pending'
// se actualice) y el mini-historial de la columna Impresos.
async function afterPrint(printedOrders) {
  for (const o of printedOrders) {
    state.selected.delete(String(o.shipment_id));
    state.advanced.delete(String(o.shipment_id));
  }
  await Promise.all([loadOrders(), loadDoneRecent()]);
  renderCola();
}

// Imprime un envío individual (reimpresión) POR EL MOTOR DE LOTES: una
// reposición debe salir idéntica a como habría salido la original — folio,
// estampado, registro y vigilancia incluidos (pedido de Antonio, 09-jul).
async function printOne(order, fmt) {
  if (!order) return;
  fmt = fmt || state.format;
  if (canServerPrint()) {
    await startBatch([order], `Reimpresión #${order.order_id || order.shipment_id}`,
                     false, fmt);
  } else {
    openLabelUrl(`/api/label/${encodeURIComponent(order.shipment_id)}?format=${fmt}`, fmt === 'zpl');
    log('WARN', 'impresion', `Sin impresora: etiqueta ${fmt.toUpperCase()} de #${order.order_id} abierta en el navegador.`);
  }
}

// Arranca un lote en segundo plano (motor seguro hoja por hoja) y sigue su progreso.
// confirmed: reintento tras el modal de saldo negativo (saldo_negativo_confirmado=1).
// fmtOverride: formato explícito (reimpresiones respetan el de la fila original).
async function startBatch(orders, labelWhat, confirmed = false, fmtOverride = null) {
  if (!orders.length) { showBanner('No hay ventas para imprimir.', 'info'); return; }
  const fmt = fmtOverride || state.format;
  if (!canServerPrint()) {
    const ids = orders.map(o => o.shipment_id).join(',');
    openLabelUrl(`/api/labels?ids=${encodeURIComponent(ids)}&format=${fmt}`, fmt === 'zpl');
    log('WARN', 'impresion', `Sin impresora: ${labelWhat} (${orders.length}) abierto en el navegador.`);
    return;
  }
  // Unificación con el Extractor: el operador es obligatorio para registrar
  // el lote (backend lo valida contra el padrón; esto solo evita el viaje
  // redondo si quedó vacío).
  const operador = (state.operador || '').trim();
  if (!operador) {
    showBanner('Escribe quién imprime («Imprimió», arriba) antes de continuar.', 'error');
    $('op-operador').focus();
    return;
  }
  try {
    const fd = new FormData();
    fd.append('ids', orders.map(o => o.shipment_id).join(','));
    fd.append('format', fmt); fd.append('printer', state.printer);
    fd.append('meta', buildMeta(orders));
    fd.append('operador', operador);
    fd.append('saldo_negativo_confirmado', confirmed ? '1' : '0');
    const r = await api('/api/print-batch', { method:'POST', body:fd });
    showBanner(`Imprimiendo ${orders.length} etiqueta(s) en «${r.printer}» · lote ${r.code_lote}…`, 'info');
    pollBatch();
  } catch (e) {
    // 409 de saldo negativo: detail = {message, items}; distinto del 409 de
    // "ya hay un lote imprimiéndose" (detail = string) — ver api().
    if (e.status === 409 && e.detail && Array.isArray(e.detail.items)) {
      openLowMarginModal(e.detail.items, () => startBatch(orders, labelWhat, true, fmtOverride));
    } else if (e.status === 409) {
      showBanner(e.message, 'error');
    } else {
      showCritical(`${labelWhat}: ${e.message}`);
    }
  }
}

// Modal de saldo negativo (gate de auditoría, unificación con el Extractor):
// cruza los envíos afectados (solo shipment_id/pack_id/markup, del backend)
// contra state.orders (ya cargados en la Cola) para mostrar producto/tienda.
let _lowMarginConfirm = null;
function openLowMarginModal(items, onConfirm) {
  const byId = new Map(state.orders.map(o => [String(o.shipment_id), o]));
  $('lowmargin-sub').textContent =
    `${items.length} venta${items.length === 1 ? '' : 's'} con saldo negativo — requiere confirmar antes de imprimir`;
  $('lowmargin-body').innerHTML = items.map(it => {
    const o = byId.get(String(it.shipment_id));
    const label = (o && (o.product_summary || o.buyer_name)) || `envío #${it.shipment_id}`;
    const store = o && o.account_name ? ` · ${esc(o.account_name)}` : '';
    return `<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;padding:8px 0;border-bottom:1px solid var(--line);font-size:12.5px">
      <span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(label)}${store}</span>
      <span style="font-family:var(--mono);color:var(--err);flex:0 0 auto;font-weight:600">markup ${esc(it.markup)}</span>
    </div>`;
  }).join('');
  $('lowmargin-ack').checked = false;
  $('lowmargin-confirm').disabled = true;
  _lowMarginConfirm = onConfirm;
  $('lowmargin-overlay').style.display = 'flex';
}
function hideLowMarginModal() { $('lowmargin-overlay').style.display = 'none'; _lowMarginConfirm = null; }

function printSelected() {
  const sel = selectedList();
  if (!sel.length) { showBanner('No hay ventas seleccionadas.', 'info'); return; }
  startBatch(sel, 'Selección');
}
// Imprime lo que ML marca para hoy (columna «A imprimir»); los Próximos no entran.
function printAllToday() {
  const q = todayList();
  if (!q.length) { showBanner('No hay etiquetas para hoy.', 'info'); return; }
  startBatch(q, 'Todo lo de hoy');
}
// ===== Progreso del lote (motor en segundo plano) =====
const BATCH_ITEM_STYLE = {
  done:     ['#eaf6ef','#15824a','Impresa'],
  printing: ['rgba(47,107,240,.12)','#2f6bf0','Imprimiendo'],
  pending:  ['#eef0f3','#6a7280','En espera'],
  risk:     ['#fbf2dd','#b07400','En riesgo'],
  blocked:  ['#eef0f3','#8a92a0','Pendiente'],
  canceled: ['#eef0f3','#8a92a0','Cancelada'],
};
async function pollBatch() {
  if (pollBatch._t) return;               // ya hay un poller activo
  const tick = async () => {
    let s;
    try { s = await api('/api/batch/status'); } catch { s = null; }
    state.batch = s;
    renderBatch();
    if (!s || !s.active || s.finished) {
      clearInterval(pollBatch._t); pollBatch._t = null;
      await loadOrders();                 // refresca 'pending' con lo impreso
      await loadDoneRecent();
      renderCola();
      if (s && s.finished) {
        const risk = (s.counts && s.counts.risk) || 0;
        if (risk) showBanner(`Lote terminado con ${risk} etiqueta(s) EN RIESGO: revísalas.`, 'error');
        else showBanner('Lote terminado.', 'success');
      }
    }
  };
  await tick();
  if (state.batch && state.batch.active && !state.batch.finished) {
    pollBatch._t = setInterval(tick, 1500);
  }
}
function renderBatch() {
  const panel = $('batch-panel'); if (!panel) return;
  const s = state.batch;
  if (!s || !s.active) { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  const c = s.counts || {};
  const done = (c.done || 0), risk = (c.risk || 0), total = s.total || 0;
  const finishedCount = done + risk + (c.blocked || 0) + (c.canceled || 0);
  $('batch-title').textContent = s.finished
    ? (risk ? 'Lote terminado — revisa las de riesgo' : 'Lote terminado')
    : `Imprimiendo lote — hoja ${s.current_sheet || 0}`;
  $('batch-msg').textContent = s.message || '';
  $('batch-done').textContent = done;
  $('batch-total').textContent = total;
  $('batch-bar').style.width = total ? Math.round(finishedCount / total * 100) + '%' : '0%';
  $('batch-bar').style.background = risk ? 'var(--warn)' : 'var(--accent)';
  $('batch-dot').style.animation = s.finished ? 'none' : 'efpulse 1.4s infinite';
  $('batch-dot').style.background = risk ? 'var(--warn)' : 'var(--accent)';
  $('btn-batch-stop').style.display = s.finished ? 'none' : '';
  $('btn-batch-close').style.display = s.finished ? '' : 'none';
  // origen del lote (automático con su regla, o manual)
  const ob = $('batch-origin');
  if (ob) {
    ob.style.display = 'inline-block';
    ob.textContent = s.origin === 'auto'
      ? 'AUTOMÁTICO' + (state.auto && state.auto.mode_now ? ' · ' +
          ({ ahorro: 'AHORRO', forzar: 'VACIADO', pausa: 'PAUSA' }[state.auto.mode_now] || '') : '')
      : 'MANUAL';
  }
  // código de lote (unificación con el Extractor) — mismo identificador que
  // queda en etiquetas_i/v_code, útil para ubicar el lote después.
  const cl = $('batch-code-lote');
  if (cl) {
    cl.style.display = s.code_lote ? 'inline-block' : 'none';
    cl.textContent = s.code_lote ? `lote ${s.code_lote}` : '';
  }
  // qué etiqueta está saliendo ahora (con su tienda)
  const nowEl = $('batch-now');
  if (nowEl) {
    const cur = !s.finished && (s.items || []).find(i => i.status === 'printing');
    nowEl.style.display = cur ? 'block' : 'none';
    if (cur) nowEl.innerHTML = `Ahora: <b>${esc(cur.product_summary || ('#' + cur.shipment_id))}</b>${cur.account_name ? ' · ' + esc(cur.account_name) : ''}`;
  }
  // resumen por estado
  const order = ['done','printing','pending','risk','blocked','canceled'];
  $('batch-chips').innerHTML = order.filter(k => c[k]).map(k => {
    const [bg,fg,label] = BATCH_ITEM_STYLE[k];
    return `<span class="chip" style="background:${bg};color:${fg}">${label}: ${c[k]}</span>`;
  }).join('');
}

// ===== Render: Cola (kanban de 3 columnas) =====
const MARKET_COLOR = { ml: '#e6a700', walmart: '#0071ce', tiktok: '#16181d' };
const MARKET_SHORT = { ml: 'ML', walmart: 'Walmart', tiktok: 'TikTok' };
function mkColor(provider) { return MARKET_COLOR[provider] || '#cfd3da'; }
function stubActive(provider, accountId) {
  if (provider !== 'walmart' && provider !== 'tiktok') return false;
  const c = state.stubCfg;
  if (!c) return true;    // default del sistema: activo para walmart/tiktok
  if (accountId != null && c.accounts && c.accounts[accountId] !== undefined) return !!c.accounts[accountId];
  return !!(c.providers || {})[provider];
}
function timeAgo(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '';
  const m = Math.floor((Date.now() - t) / 60000);
  if (m < 1) return 'ahora';
  if (m < 60) return `hace ${m} min`;
  if (m < 48 * 60) return `hace ${Math.floor(m / 60)} h`;
  return `hace ${Math.floor(m / 1440)} días`;
}
// Etiqueta de fecha límite de despacho (dato de ML): cuándo toca imprimirla.
function dueTag(o) {
  const tags = [];
  if (o.printable === false)
    tags.push('<span class="qtag" style="background:#eef4ff;color:#2f56c0">en procesamiento</span>');
  if (o.due === 'overdue')
    tags.push('<span class="qtag" style="background:#fbe9e9;color:#c43232">vencida</span>');
  else if (o.due === 'upcoming' && o.handling_limit) {
    const d = new Date(o.handling_limit);
    if (!isNaN(d.getTime()))
      tags.push(`<span class="qtag" style="background:#fbf2dd;color:#7a5b00">para ${esc(d.toLocaleDateString('es-MX', { weekday:'short', day:'2-digit', month:'short' }))}</span>`);
  }
  return tags.join('');
}
function qTags(o) {
  const tags = [];
  const due = dueTag(o);
  if (due) tags.push(due);
  if (o.account_name && state.accountsMeta.length > 1)
    tags.push(`<span class="qtag"><i></i>${esc(o.account_name)}</span>`);
  if (stubActive(o.provider, o.account_id)) tags.push('<span class="qtag stub">✂ talón</span>');
  const ago = timeAgo(o.date_created);
  if (ago) tags.push(`<span class="qtag time">${ago}</span>`);
  return tags.length ? `<div class="q-tags">${tags.join('')}</div>` : '';
}
function colaItemHtml(o, kind) {
  const prod = (o.products && o.products[0]) || { title:'—' };
  const sid = String(o.shipment_id);
  const qty = (parseInt(o.units, 10) || 1) > 1 ? `<span class="q-qty">×${o.units}</span>` : '';
  const body = `<div style="flex:1;min-width:0">
      <div class="q-title">${esc(prod.title)}${qty}</div>
      <div class="q-sub">${esc(o.buyer_name)} · #${esc(o.order_id)}</div>
      ${qTags(o)}
    </div>`;
  if (kind === 'next') {
    // Próximos: informativa (la fecha la pone ML); solo una etiqueta ya
    // generada se puede adelantar — «en procesamiento» aún no tiene.
    const adv = o.printable === false ? ''
      : `<button class="xbtn" title="Adelantar a «A imprimir»" data-sid="${esc(sid)}" data-act="advance" style="color:var(--accent);align-self:center;font-size:14px">→</button>`;
    return `<div class="q-item mk" style="--mkc:${mkColor(o.provider)}" data-sid="${esc(sid)}">
      ${body}
      ${adv}
    </div>`;
  }
  // A imprimir (hoy): la casilla arma el lote; la tarjeta no cambia de columna.
  const on = state.selected.has(sid);
  const adv = state.advanced.has(sid);
  return `<div class="q-item mk" style="cursor:pointer;--mkc:${mkColor(o.provider)}" title="${on ? 'Quitar del lote' : 'Marcar para el lote'}" data-sid="${esc(sid)}" data-act="toggle">
    <span class="chk${on ? ' on' : ''}" style="margin-top:2px"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round" style="visibility:${on ? 'visible' : 'hidden'}"><polyline points="20 6 9 17 4 12"></polyline></svg></span>
    ${body}
    ${adv ? `<button class="xbtn" title="Devolver a «Próximos»" data-sid="${esc(sid)}" data-act="unadvance" style="align-self:center">✕</button>` : ''}
  </div>`;
}
// Contadores por cuenta para la cabecera de cada columna (solo si hay >1 tienda).
function storeCountChips(list) {
  const metas = state.accountsMeta || [];
  if (metas.length < 2) return '';
  return metas.map(a => {
    const n = list.filter(o => String(o.account_id) === String(a.id)).length;
    return `<span class="mkchip"><i style="background:${mkColor(a.provider)}"></i>${esc(a.name)} <b>${n}</b></span>`;
  }).join('');
}
function renderStoreChips() {
  const box = $('store-chips'); if (!box) return;
  const metas = state.accountsMeta || [];
  const errs = state.ordersErrors || [];
  const show = metas.length >= 2 || errs.length > 0;
  box.style.display = show ? 'flex' : 'none';
  if (!show) { if (state.storeFilter !== 'all') { state.storeFilter = 'all'; } return; }
  if (state.storeFilter !== 'all' && !metas.some(a => String(a.id) === String(state.storeFilter))) state.storeFilter = 'all';
  const pendAll = state.orders.filter(o => o.pending && !o.multi_unit);
  const chips = [`<button class="fchip${state.storeFilter === 'all' ? ' on' : ''}" data-store="all">Todas <span class="n">${pendAll.length}</span></button>`];
  for (const a of metas) {
    const n = pendAll.filter(o => String(o.account_id) === String(a.id)).length;
    const err = errs.find(e => String(e.account_id) === String(a.id));
    const on = String(state.storeFilter) === String(a.id) ? ' on' : '';
    const badge = err ? '<span class="n" style="background:#fbe9e9;color:#c43232">sin conexión</span>'
                      : `<span class="n">${n}</span>`;
    chips.push(`<button class="fchip${on}" data-store="${esc(a.id)}" ${err ? `title="${esc(err.error)}"` : ''}><i style="background:${mkColor(a.provider)}"></i>${esc(a.name)} ${badge}</button>`);
  }
  box.innerHTML = chips.join('');
}
function renderStoreErrors() {
  const box = $('store-errors'); if (!box) return;
  const errs = state.ordersErrors || [];
  box.style.display = errs.length ? 'flex' : 'none';
  box.innerHTML = errs.map(er => `<div class="card" style="background:#fbe9e9;border-color:#efc7c7;color:#8f2626;padding:11px 16px;font-size:12.5px;display:flex;align-items:center;gap:12px">
    <span style="font-size:15px;flex:0 0 auto">⛔</span>
    <span style="flex:1;min-width:0"><strong>${esc(er.account_name || 'Tienda')}</strong> no responde: ${esc(String(er.error || '').slice(0, 140))} — sus pedidos no aparecen en la cola.</span>
    <button class="btn btn-ghost" data-goto="conexion" style="border-color:#e0b1b1;white-space:nowrap;padding:6px 12px;font-size:12px">Reconectar</button>
  </div>`).join('');
}
function renderAutoKpi() {
  const el = $('kpi-auto'), dot = $('kpi-auto-dot'), sub = $('kpi-auto-sub'), card = $('kpi-auto-card');
  if (!el) return;
  const a = state.auto;
  if (!a || !a.config) { el.textContent = '—'; el.style.color = 'var(--muted2)'; dot.style.background = '#aab0bb'; dot.style.animation = 'none'; sub.textContent = ''; card.style.borderLeft = ''; return; }
  if (!a.config.enabled) {
    el.textContent = 'Apagado'; el.style.color = 'var(--muted2)';
    dot.style.background = '#aab0bb'; dot.style.animation = 'none';
    sub.textContent = 'actívalo en la pestaña Automático'; card.style.borderLeft = '';
    return;
  }
  const MODE = { ahorro: ['Ahorro', '#15a05a', 'imprime hojas llenas'],
                 forzar: ['Vaciado', '#2f6bf0', 'imprime todo lo pendiente'],
                 pausa:  ['Pausa', '#b07400', 'acumula sin imprimir'] };
  const [label, color, desc] = MODE[a.mode_now] || [a.mode_now || '—', 'var(--muted2)', ''];
  el.textContent = label; el.style.color = color;
  dot.style.background = color;
  dot.style.animation = a.state === 'printing' ? 'efpulse 1.4s infinite' : 'none';
  sub.textContent = a.state === 'printing' ? 'imprimiendo ahora…'
    : `${desc} · revisa cada ${a.config.interval_min || '?'} min`;
  card.style.borderLeft = `4px solid ${color}`;
}
const HIST_STATUS = {
  ok:      ['#eaf6ef','#15824a','✓','Impresa'],
  risk:    ['#fbf2dd','#b07400','!','En riesgo'],
  blocked: ['#eef0f3','#8a92a0','·','Pendiente'],
  error:   ['#fbe9e9','#c43232','✕','Error'],
};
function histStyle(h) { return HIST_STATUS[h.status] || (h.ok ? HIST_STATUS.ok : HIST_STATUS.error); }
function doneItemHtml(h) {
  const time = new Date(h.ts * 1000).toLocaleTimeString('es-MX', { hour12:false });
  const [bg,fg,txt] = histStyle(h);
  const canReprint = h.status === 'risk' || h.status === 'ok';
  const meta = (state.accountsMeta || []).find(a => a.name === h.account);
  const color = mkColor(meta && meta.provider);
  const origin = h.origin === 'auto' ? ' · <b style="color:var(--ink2)">automático</b>' : (h.origin ? ' · manual' : '');
  const store = h.account && state.accountsMeta.length > 1
    ? `<div class="q-tags"><span class="qtag"><i style="background:${color}"></i>${esc(h.account)}</span></div>` : '';
  return `<div class="q-item mk" style="--mkc:${color};cursor:default" title="${esc(h.error || '')}">
    <span class="chip" style="background:${bg};color:${fg};flex:0 0 auto;margin-top:1px">${txt}</span>
    <div style="flex:1;min-width:0">
      <div class="q-title">${esc(h.product_summary || ('#'+h.shipment_id))}</div>
      <div class="q-sub">${esc(h.buyer_name || ('venta '+(h.order_id||'')))} · ${time}${origin}</div>
      ${store}
    </div>
    ${canReprint ? `<button class="xbtn" title="Reimprimir" data-sid="${esc(h.shipment_id)}" data-fmt="${esc(h.format||'pdf')}" data-oid="${esc(h.order_id||'')}" data-buyer="${esc(h.buyer_name||'')}" data-prod="${esc(h.product_summary||'')}" data-aid="${esc(h.account_id||'')}" data-aname="${esc(h.account||'')}" data-act="reprint-done" style="color:var(--accent);align-self:center">⟳</button>`
      : `<span class="q-sub" style="flex:0 0 auto;align-self:center">${esc((h.format||'').toUpperCase())}</span>`}
  </div>`;
}
function renderCola() {
  const pend = pendingPool();
  const next = nextList();
  const hoy = todayList();
  const sel = selectedList();
  // limpia selección/adelantos de ids que ya no están pendientes/presentes
  const present = new Set(pend.map(o => String(o.shipment_id)));
  for (const id of [...state.selected]) if (!present.has(id)) state.selected.delete(id);
  for (const id of [...state.advanced]) if (!present.has(id)) state.advanced.delete(id);

  $('nav-queue-count').textContent = pend.length;
  const mlist = manualList();
  const mbadge = $('nav-manual-count');
  if (mbadge) { mbadge.textContent = mlist.length; mbadge.style.display = mlist.length ? 'inline-block' : 'none'; }
  $('kpi-pendientes').textContent = pend.length;
  $('kpi-encola').textContent = hoy.length;
  $('kpi-encola-break').innerHTML = sel.length ? `<span class="mkchip">${sel.length} en el lote</span>` : '';
  $('kpi-hoy').textContent = state.printedToday;
  // desglose de pendientes por marketplace (solo si hay más de uno)
  const provCount = {};
  for (const o of state.orders) if (o.pending && !o.multi_unit)
    provCount[o.provider || 'ml'] = (provCount[o.provider || 'ml'] || 0) + 1;
  const provKeys = Object.keys(provCount);
  $('kpi-pend-break').innerHTML = provKeys.length > 1
    ? provKeys.map(p => `<span class="mkchip"><i style="background:${mkColor(p)}"></i>${MARKET_SHORT[p] || p} ${provCount[p]}</span>`).join('') : '';
  // desglose de impresas hoy por origen
  const sp = state.printedSplit;
  $('kpi-hoy-break').innerHTML = (sp && sp.total > 0 && sp.auto > 0)
    ? `<span class="mkchip">${sp.auto} automático</span><span class="mkchip">${sp.manual} manual</span>` : '';
  renderAutoKpi();
  $('print-pending-count').textContent = hoy.length;
  $('print-sel-count').textContent = sel.length;
  $('col-next-count').textContent = next.length;
  $('col-sel-count').textContent = hoy.length;
  $('col-done-count').textContent = state.printedToday;
  $('btn-print-pending').disabled = !hoy.length;
  $('btn-print-selected').disabled = !sel.length;
  $('btn-select-all').disabled = !hoy.length;

  // contadores por cuenta (dato de ML, sin filtro de tienda)
  const allPool = poolAllStores();
  const nextStores = $('col-next-stores'), selStores = $('col-sel-stores');
  if (nextStores) {
    const html = storeCountChips(allPool.filter(o => !isForToday(o)));
    nextStores.innerHTML = html; nextStores.style.display = html ? 'flex' : 'none';
  }
  if (selStores) {
    const html = storeCountChips(allPool.filter(isForToday));
    selStores.innerHTML = html; selStores.style.display = html ? 'flex' : 'none';
  }

  // Próximos (siguientes días según ML)
  const buscando = !!state.searchQ;
  const cNext = $('col-next');
  if (!state.status.connected) cNext.innerHTML = idle('Conecta tu cuenta para ver las ventas.');
  else if (!next.length) cNext.innerHTML = idle(buscando ? 'Sin coincidencias aquí.' : 'Nada programado para los siguientes días.');
  else cNext.innerHTML = next.map(o => colaItemHtml(o, 'next')).join('');

  // A imprimir (hoy según ML)
  const cSel = $('col-sel');
  cSel.innerHTML = hoy.length ? hoy.map(o => colaItemHtml(o, 'today')).join('')
    : idle(buscando ? 'Sin coincidencias aquí.' : 'No hay etiquetas para hoy.');

  // Impresos (mini-historial reciente, también filtrado por el buscador)
  const doneShown = state.doneRecent.filter(doneMatchesSearch);
  const cDone = $('col-done');
  cDone.innerHTML = doneShown.length ? doneShown.map(doneItemHtml).join('')
    : idle(buscando ? 'Sin coincidencias aquí.' : 'Aún no imprimes nada hoy.');

  // resumen del buscador
  const sc = $('q-search-count'), scl = $('q-search-clear');
  if (sc) {
    if (buscando) {
      sc.textContent = `${next.length + hoy.length} en cola · ${doneShown.length} impresas`;
      sc.style.display = 'inline'; if (scl) scl.style.display = 'inline-block';
    } else {
      sc.style.display = 'none'; if (scl) scl.style.display = 'none';
    }
  }

  renderFormatSeg();
  renderStoreChips();
  renderStoreErrors();
  updatePrinterStatus();
  renderBatch();
  renderRiskAlert();
  updateLayoutHint();
  updateFolioHint();
}
// Folio sugerido (unificación con el Extractor): solo tiene sentido con UNA
// tienda filtrada — el folio es consecutivo POR EMPRESA (D1), así que con
// "Todas" seleccionadas no hay una única organización que sugerir.
// Informativo nada más: el folio real lo asigna el motor de lotes al imprimir.
async function updateFolioHint() {
  const hint = $('op-folio-hint'); if (!hint) return;
  if (state.storeFilter === 'all') { hint.style.display = 'none'; updateFolioHint._for = null; return; }
  if (updateFolioHint._for === state.storeFilter) return;   // ya consultado
  updateFolioHint._for = state.storeFilter;
  const acc = (state.accountsMeta || []).find(a => String(a.id) === String(state.storeFilter));
  if (!acc) { hint.style.display = 'none'; return; }
  try {
    const d = await api(`/api/enrich/next-folio?organization=${encodeURIComponent(acc.name)}`);
    hint.textContent = `folio sugerido: ${d.next_folio} (${d.organization})`;
    hint.style.display = 'inline';
  } catch { hint.style.display = 'none'; }
}
function renderRiskAlert() {
  const el = $('risk-alert'); if (!el) return;
  if (state.riskTotal > 0) {
    el.style.display = 'flex';
    $('risk-count').textContent = state.riskTotal;
  } else {
    el.style.display = 'none';
  }
}
// Texto en vivo del acomodo: "N etiquetas → K por hoja → M hojas".
async function updateLayoutHint() {
  const row = $('layout-hint-row'); if (!row) return;
  const count = selectedList().length || todayList().length;
  if (!count || state.format === 'zpl') { row.style.display = 'none'; return; }
  const key = count + ':' + state.format;
  if (updateLayoutHint._key === key) { row.style.display = 'flex'; return; }
  try {
    const p = await api('/api/layout-plan?count=' + count);
    const src = p.size_source === 'real' ? `tamaño real ${p.label_cm}` : `tamaño estimado ${p.label_cm}`;
    $('layout-hint').innerHTML = `<strong>${count}</strong> etiqueta(s) → <strong>${p.labels_per_sheet}</strong> por hoja → <strong>${p.sheets}</strong> hoja(s) Carta · ${esc(src)}`;
    updateLayoutHint._key = key;
    row.style.display = 'flex';
  } catch { row.style.display = 'none'; }
}
function idle(text) {
  return `<div class="empty" style="height:160px;padding:24px 8px">
    <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="6" y="3" width="12" height="6" rx="1"></rect><path d="M6 14H4a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-2"></path><rect x="6" y="14" width="12" height="7" rx="1"></rect></svg>
    <span style="font-size:12.5px;font-weight:500;max-width:220px">${text}</span></div>`;
}

// ===== Historial de impresión =====
async function loadDoneRecent() {
  try {
    const d = await api('/api/print-history?limit=12');
    state.doneRecent = d.items || [];
    if (typeof d.printed_today === 'number') state.printedToday = d.printed_today;
    if (typeof d.risk_total === 'number') state.riskTotal = d.risk_total;
  } catch { state.doneRecent = []; }
}
function histQuery() {
  const f = state.histFilter, p = new URLSearchParams();
  p.set('limit', '300');
  if (f.from) p.set('date_from', String(Math.floor(new Date(f.from + 'T00:00:00').getTime() / 1000)));
  if (f.to) p.set('date_to', String(Math.floor(new Date(f.to + 'T23:59:59').getTime() / 1000)));
  if (f.format) p.set('format', f.format);
  if (f.result) p.set('result', f.result);
  return p.toString();
}
async function renderHistorial(opts = {}) {
  const rows = $('hist-rows');
  // silent = refresco en 2.º plano: no parpadear con «Cargando…»
  if (!opts.silent) rows.innerHTML = `<div class="empty" style="padding:36px 0"><span>Cargando…</span></div>`;
  let data;
  try { data = await api('/api/print-history?' + histQuery()); }
  catch (e) { rows.innerHTML = `<div class="empty" style="padding:36px 0"><span>${esc(e.message)}</span></div>`; return; }
  const items = data.items || [];
  state.printedToday = (typeof data.printed_today === 'number') ? data.printed_today : state.printedToday;
  if (typeof data.risk_total === 'number') state.riskTotal = data.risk_total;
  const riskTxt = state.riskTotal ? ` · ${state.riskTotal} en riesgo` : '';
  $('hist-total').textContent = `${data.total} registro(s) · ${state.printedToday} hoy${riskTxt}`;
  if (!items.length) { rows.innerHTML = `<div class="empty" style="padding:44px 0"><span>Sin impresiones registradas.</span></div>`; return; }
  rows.innerHTML = items.map(h => {
    const dt = new Date(h.ts * 1000);
    const when = dt.toLocaleDateString('es-MX') + ' ' + dt.toLocaleTimeString('es-MX', { hour12:false });
    const [bg,fg,,txt] = histStyle(h);
    return `<div class="log-row">
      <span style="width:150px;font-family:var(--mono);color:var(--muted2);font-size:12px">${esc(when)}</span>
      <span style="width:104px;font-family:var(--mono);font-size:12px;color:var(--ink3)">#${esc(h.order_id || h.shipment_id)}</span>
      <span style="flex:1;min-width:0;color:#2b303a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(h.error || '')}">${esc(h.buyer_name || '—')}${h.product_summary ? ' · ' + esc(h.product_summary) : ''}</span>
      <span style="width:58px;font-family:var(--mono);font-size:12px">${esc((h.format||'').toUpperCase())}</span>
      <span style="width:132px;font-size:12px;color:var(--ink3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(h.printer||'')}">${esc(h.printer || '—')}</span>
      <span style="width:52px;font-family:var(--mono);font-size:12px">${h.sheets != null ? esc(h.sheets) : '—'}</span>
      <span style="width:78px"><span class="lvl" style="background:${bg};color:${fg}">${txt}</span></span>
      <span style="width:92px"><button class="btn btn-ghost" style="padding:4px 9px;font-size:11px" data-sid="${esc(h.shipment_id)}" data-fmt="${esc(h.format||'pdf')}" data-oid="${esc(h.order_id||'')}" data-buyer="${esc(h.buyer_name||'')}" data-prod="${esc(h.product_summary||'')}" data-aid="${esc(h.account_id||'')}" data-aname="${esc(h.account||'')}" data-act="reprint">Reimprimir</button></span>
    </div>`;
  }).join('');
}

// ===== Formato (toggle segmentado en la barra de la Cola) =====
function renderFormatSeg() {
  document.querySelectorAll('#format-seg .seg-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.format === state.format));
}
function setFormat(f) {
  state.format = f; localStorage.setItem('ef_format', f);
  renderFormatSeg(); renderCola();
  log('INFO', 'impresion', `Formato de etiqueta: ${f.toUpperCase()}.`);
}

// ===== Dispositivos / impresoras =====
function diffPrinters(oldList, newList) {
  const oldMap = new Map(oldList.map(p => [p.name, p]));
  const newMap = new Map(newList.map(p => [p.name, p]));
  const ev = [];
  for (const [name, p] of newMap) {
    const o = oldMap.get(name);
    if (!o) ev.push({ lvl: 'INFO', msg: `Impresora «${name}» disponible.` });
    else if (o.state !== p.state)
      ev.push({ lvl: p.state === 'disabled' ? 'WARN' : 'OK', msg: `«${name}»: ${o.state} → ${p.state}.` });
    // cambio de disponibilidad real (sondeo activo) → re-render en vivo
    else if (o.ready !== p.ready)
      ev.push({ lvl: p.ready ? 'OK' : 'WARN',
                msg: p.ready ? `«${name}» ya está lista para imprimir.`
                             : `«${name}» dejó de estar lista: ${p.ready_reason || '—'}.` });
  }
  for (const name of oldMap.keys())
    if (!newMap.has(name)) ev.push({ lvl: 'WARN', msg: `Impresora «${name}» desconectada / ya no disponible.` });
  return ev;
}
async function loadPrinters(opts = {}) {
  let data;
  try { data = await api('/api/printers'); }
  catch (e) { state.printers = []; state.cups = false; renderPrinters(); renderPrinterSelect(); return; }
  const newList = data.printers || [];
  const events = state._printersInit ? diffPrinters(state.printers, newList) : [];
  state.printers = newList; state.cups = !!data.cups;
  // si la impresora seleccionada ya no existe, volver a la predeterminada
  if (state.printer && !newList.some(p => p.name === state.printer)) {
    state.printer = ''; localStorage.removeItem('ef_printer');
  }
  // solo banner: el backend (módulo 'cups') ya loguea estas transiciones
  for (const e of events) if (e.lvl === 'WARN') showBanner(e.msg, 'error');
  if (!state._printersInit || events.length || opts.force) { renderPrinters(); renderPrinterSelect(); }
  state._printersInit = true;
}
function connChip(conn) {
  const map = { 'USB':['#eef4ff','#2f56c0'], 'Red':['#eaf6ef','#15824a'] };
  const [bg,fg] = map[conn] || ['#eef0f3','#6a7280'];
  return `<span class="chip" style="background:${bg};color:${fg}">${conn}</span>`;
}
function renderPrinters() {
  $('cups-state').textContent = state.cups
    ? 'Las etiquetas se envían a la impresora desde el servidor (USB o red).'
    : 'CUPS no está activo. Inícialo con: sudo systemctl start cups';
  const wrap = $('printer-cards');
  if (!state.printers.length) {
    wrap.innerHTML = `<div class="card empty" style="grid-column:1/-1;padding:34px"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="6" y="9" width="12" height="7" rx="1"></rect><path d="M6 9V4h12v5"></path><path d="M6 14H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-2"></path></svg><span style="font-size:13px;font-weight:500">No hay impresoras configuradas.</span><span style="font-size:12px">Agrega una por IP o detéctala por USB arriba.</span></div>`;
    return;
  }
  // Regla: si una impresora NO está disponible (apagada, desconectada,
  // compartida de otro equipo), no se muestra. Un enlace discreto permite
  // verlas solo para administrarlas (p. ej. eliminar una cola muerta).
  const offList = state.printers.filter(p => !p.ready);
  const shown = state.showOffPrinters ? state.printers
                                      : state.printers.filter(p => p.ready);
  const toggle = offList.length
    ? `<div style="grid-column:1/-1;text-align:center;padding:2px 0">
         <button class="btn btn-ghost" data-act="toggle-off" data-name="-" style="padding:6px 12px;font-size:12px;color:var(--muted)">
           ${state.showOffPrinters ? 'Ocultar las no disponibles'
                                   : `Mostrar ${offList.length} no disponible(s) (solo para administrar)`}</button>
       </div>`
    : '';
  if (!shown.length) {
    wrap.innerHTML = `<div class="card empty" style="grid-column:1/-1;padding:34px"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="6" y="9" width="12" height="7" rx="1"></rect><path d="M6 9V4h12v5"></path><path d="M6 14H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-2"></path></svg><span style="font-size:13px;font-weight:500">Ninguna impresora disponible ahora.</span><span style="font-size:12px">Enciende/conecta tu impresora; aparecerá aquí sola en unos segundos.</span></div>` + toggle;
    return;
  }
  wrap.innerHTML = shown.map(p => {
    // Estado real: la lista del backend trae ready/ready_reason/shared.
    const st = p.state === 'printing' ? ['#fbf2dd','#b07400','Imprimiendo']
             : p.ready ? ['#eaf6ef','#15824a','Lista para imprimir']
             : p.shared ? ['#eef4ff','#2f56c0','Compartida (sin confirmar)']
             : ['#fbe9e9','#c43232','No imprime ahora'];
    const stTitle = p.ready ? ''
      : (p.ready_reason || 'No disponible para imprimir en este momento.');
    // Impresora que no imprime → tarjeta atenuada y acciones de impresión bloqueadas.
    const off = !p.ready;
    const dis = off ? 'disabled' : '';
    const iconBg = off ? '#eef0f3' : 'var(--accent-tint)';
    const iconStroke = off ? '#9098a4' : 'var(--accent)';
    return `<div class="card" style="padding:18px${off ? ';opacity:.62;background:#fbfbfc' : ''}">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:12px">
        <div style="display:flex;gap:12px;min-width:0;flex:1">
          <div style="width:40px;height:40px;border-radius:10px;background:${iconBg};display:flex;align-items:center;justify-content:center;flex:0 0 40px">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="${iconStroke}" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="3" width="12" height="6" rx="1"></rect><path d="M6 14H4a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2h-2"></path><rect x="6" y="14" width="12" height="7" rx="1"></rect></svg>
          </div>
          <div style="line-height:1.3;min-width:0;flex:1">
            <div title="${esc(p.name)}" style="font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(p.name)}</div>
            <div style="font-size:11.5px;color:var(--muted2);font-family:var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(p.uri)}">${esc(p.uri||'—')}</div>
            ${off && p.ready_reason ? `<div style="font-size:11.5px;color:var(--err);margin-top:3px">${esc(p.ready_reason)}</div>` : ''}
          </div>
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex:0 0 auto">${connChip(p.connection)}<span class="chip" title="${stTitle}" style="background:${st[0]};color:${st[1]}">${st[2]}</span>${p.is_default?'<span class="chip" style="background:var(--accent-tint);color:var(--accent)">predet.</span>':''}</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-ghost" style="flex:1;padding:8px;font-size:12px" data-act="test" data-name="${esc(p.name)}" ${dis} title="${off ? 'No se puede probar: '+esc(p.ready_reason||'no está lista') : 'Imprimir página de prueba'}">Prueba</button>
        ${p.is_default
          ? `<button class="btn btn-ghost" style="flex:1;padding:8px;font-size:12px;color:var(--accent);border-color:var(--accent)" data-act="undefault" data-name="${esc(p.name)}">Quitar predet.</button>`
          : `<button class="btn btn-ghost" style="flex:1;padding:8px;font-size:12px" data-act="default" data-name="${esc(p.name)}" ${dis}>Predeterminar</button>`}
        <button class="btn btn-danger" style="padding:8px 11px;font-size:12px" data-act="delete" data-name="${esc(p.name)}">✕</button>
      </div>
    </div>`;
  }).join('') + toggle;
}
function renderPrinterSelect() {
  const sel = $('printer-select');
  if (!sel) return;
  // Solo aparecen impresoras que de verdad pueden imprimir AHORA; las no
  // disponibles ni se listan (regla: si no está lista, no se muestra).
  const opts = ['<option value="">Predeterminada del sistema</option>'].concat(
    state.printers.filter(p => p.ready)
      .map(p => `<option value="${esc(p.name)}">${esc(p.name)} · ${p.connection}</option>`)
  );
  sel.innerHTML = opts.join('');
  // si la seleccionada dejó de estar lista, volver a la predeterminada
  const cur = state.printers.find(p => p.name === state.printer);
  if (state.printer && (!cur || !cur.ready)) {
    state.printer = ''; localStorage.removeItem('ef_printer');
  }
  sel.value = state.printer || '';
  updatePrinterStatus();
}
// Punto verde/rojo + nota junto al selector de la Cola.
function updatePrinterStatus() {
  const dot = $('op-printer-dot'), note = $('op-printer-note');
  if (!dot) return;
  let ready, reason;
  if (state.printer) {
    const p = state.printers.find(x => x.name === state.printer);
    ready = p ? p.ready : false; reason = p ? p.ready_reason : 'No encontrada';
  } else {
    // Predeterminada del sistema: lista si hay alguna impresora lista.
    ready = state.printers.some(p => p.ready);
    reason = ready ? '' : 'Ninguna impresora lista';
  }
  dot.style.background = ready ? '#1ba85b' : '#d33a3a';
  note.textContent = ready ? '' : (reason || 'No imprime');
}
async function scanDevices() {
  const list = $('device-list');
  list.innerHTML = '<div style="font-size:12px;color:var(--muted)">Buscando dispositivos…</div>';
  try {
    const d = await api('/api/printers/devices');
    const devs = d.devices || [];
    if (!devs.length) { list.innerHTML = '<div style="font-size:12px;color:var(--muted)">Sin dispositivos nuevos detectados.</div>'; return; }
    window.__devs = devs;
    list.innerHTML = devs.map((dev,i) => `<div class="opt" style="cursor:default" title="${esc(dev.uri)}">
      ${connChip(dev.connection)}
      <span style="flex:1;min-width:0;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(friendlyName(dev.uri))}</span>
      <button class="btn btn-ghost" style="padding:4px 9px;font-size:11px" onclick="window.__addDev(${i})">Agregar</button></div>`).join('');
    window.__addDev = (i) => addDeviceByUri(window.__devs[i]);
  } catch (e) { list.innerHTML = `<div style="font-size:12px;color:var(--err)">${esc(e.message)}</div>`; }
}
function friendlyName(uri) {
  let u = uri || '';
  try { u = decodeURIComponent(u); } catch {}
  let m = u.match(/^usb:\/\/([^/]+)\/([^?]+)/i);                       // usb://Vendor/Modelo?serial=…
  if (m) return (m[1] + ' ' + m[2]).replace(/[+_]/g, ' ').trim();
  m = u.match(/^dnssd:\/\/(.+?)\._(?:ipp|ipps|pdl-datastream|printer)\._tcp/i);  // dnssd://Nombre._ipp._tcp…
  if (m) { let n = m[1]; const at = n.indexOf(' @ '); if (at > 0) n = n.slice(0, at); return n.trim(); }
  m = u.match(/^implicitclass:\/\/([^/]+)/i);                          // cola driverless
  if (m) { let n = m[1]; const at = n.indexOf(' @ '); if (at > 0) n = n.slice(0, at); return n.replace(/_/g, ' ').trim(); }
  m = u.match(/^(?:ipp|ipps|socket|http|https|lpd):\/\/([^/:]+)/i);    // host
  if (m) return m[1];
  return u.slice(0, 40);
}
async function addDeviceByUri(dev) {
  const name = prompt('Nombre para la impresora:', friendlyName(dev.uri));
  if (!name) return;
  const fd = new FormData(); fd.append('name', name); fd.append('uri', dev.uri);
  try {
    await api('/api/printers', { method:'POST', body:fd });
    showBanner('Impresora agregada.', 'success');
    await loadPrinters();
  } catch (e) { showBanner('No se pudo agregar: ' + e.message, 'error'); }
}

// ===== Cuentas / tiendas =====
const PROVIDER_LABEL = { ml:'Mercado Libre', walmart:'Walmart', tiktok:'TikTok Shop' };
// Colores del chip de tienda (distintivo por marketplace).
const PROVIDER_CHIP = { ml:['#fff3c4','#7a6200'], walmart:['#e7f0fe','#0b57d0'], tiktok:['#eceff1','#263238'] };
function fmtToken(sec) {
  if (sec == null) return '—';
  if (sec <= 0) return 'Token expirado';
  if (sec >= 3600) { const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60); return `token ${h}h ${m}m`; }
  const m = Math.floor(sec/60); return `token ${m} min`;
}
async function loadAccounts() {
  try { const d = await api('/api/accounts'); state.accounts = d.accounts || []; } catch { state.accounts = []; }
  if (!state.providers.length) {
    try { const p = await api('/api/providers'); state.providers = p.providers || []; } catch {}
  }
}
function renderConexion() {
  const box = $('accounts-list'); if (!box) return;
  if (!state.accounts.length) {
    box.innerHTML = `<div class="card empty" style="padding:34px"><span style="font-size:13px;font-weight:500">No hay tiendas configuradas.</span><span style="font-size:12px">Pulsa «Agregar tienda» para conectar tu primera cuenta.</span></div>`;
    return;
  }
  box.innerHTML = state.accounts.map(a => {
    const [bg,fg,dot,label] = a.connected
      ? ['#eaf6ef','#15824a','#1ba85b','Conectada']
      : a.has_secret ? ['#fbf2dd','#b07400','#e8a200','Configurada · sin conectar']
                     : ['#eef0f3','#6a7280','#aab0bb','Sin configurar'];
    const tok = a.connected ? ' · ' + fmtToken(a.token_expires_in) : '';
    return `<div class="card" style="padding:16px 18px${a.enabled?'':';opacity:.6'}">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div style="min-width:0">
          <div style="font-size:14px;font-weight:700">${esc(a.name||'Tienda')} <span style="font-size:11px;font-weight:600;color:var(--muted2)">· ${esc(PROVIDER_LABEL[a.provider]||a.provider)}</span></div>
          <div style="font-size:12px;color:var(--muted);font-family:var(--mono)">${a.nickname?esc(a.nickname)+' · ':''}${a.seller_id?'ID '+esc(a.seller_id):esc(a.app_id||'')}${esc(tok)}</div>
        </div>
        <span class="pill" style="background:${bg};color:${fg};font-size:12px;padding:6px 11px"><span class="pdot" style="width:8px;height:8px;background:${dot}"></span>${label}</span>
      </div>
      <div class="btn-row" style="margin-top:14px;flex-wrap:wrap">
        <button class="btn btn-accent" style="padding:7px 12px;font-size:12px" data-acc="${esc(a.id)}" data-act="connect">${a.connected?'Reconectar':'Conectar'}</button>
        ${a.connected?`<button class="btn btn-ghost" style="padding:7px 12px;font-size:12px" data-acc="${esc(a.id)}" data-act="refresh">Renovar token</button>`:''}
        <button class="btn btn-ghost" style="padding:7px 12px;font-size:12px" data-acc="${esc(a.id)}" data-act="edit">Editar</button>
        <button class="btn btn-ghost" style="padding:7px 12px;font-size:12px" data-acc="${esc(a.id)}" data-act="toggle">${a.enabled?'Pausar':'Activar'}</button>
        ${a.provider!=='walmart'?`<button class="btn btn-ghost" style="padding:7px 12px;font-size:12px" data-acc="${esc(a.id)}" data-act="manual">Canjear code</button>`:''}
        ${a.connected?`<button class="btn btn-ghost" style="padding:7px 12px;font-size:12px" data-acc="${esc(a.id)}" data-act="disconnect">Desconectar</button>`:''}
        <button class="btn btn-danger" style="padding:7px 11px;font-size:12px" data-acc="${esc(a.id)}" data-act="delete">✕</button>
      </div>
    </div>`;
  }).join('');
}

// ===== Etiquetas: talón de control + vista previa por marketplace/tienda =====
async function loadStubCard() {
  try { const d = await api('/api/stub-config'); state.stubCfg = { providers: d.providers || {}, accounts: d.accounts || {} }; }
  catch { state.stubCfg = null; }
  await loadAccounts();           // para listar las tiendas de cada marketplace
  renderStubRows();
  updateStubPreviews();
}
function renderStubRows() {
  const box = $('stub-rows'); if (!box) return;
  if (!state.stubCfg) { box.innerHTML = '<div style="font-size:12px;color:var(--muted)">No disponible.</div>'; return; }
  const provs = [
    { id: 'ml', label: 'Mercado Libre', native: true },
    { id: 'walmart', label: 'Walmart', native: false },
    { id: 'tiktok', label: 'TikTok Shop', native: false },
  ];
  box.innerHTML = provs.map(p => {
    const on = !!state.stubCfg.providers[p.id];
    const toggle = p.native
      ? '<span class="chip" style="background:var(--accent-tint);color:var(--accent)">producto impreso nativo</span>'
      : `<label style="display:flex;align-items:center;gap:7px;font-size:12.5px;cursor:pointer"><input type="checkbox" data-stub-prov="${p.id}" ${on ? 'checked' : ''}> Talón activo</label>`;
    // tiendas conectadas/configuradas de este marketplace (excepción por tienda)
    const accs = p.native ? [] : state.accounts.filter(a => a.provider === p.id);
    const accRows = accs.map(a => {
      const ov = state.stubCfg.accounts[a.id];               // excepción (si hay)
      const val = ov === undefined ? '' : (ov ? '1' : '0');
      return `<div style="display:flex;align-items:center;justify-content:space-between;gap:9px;padding:7px 0 0 22px">
        <div style="font-size:12px;color:var(--muted)">↳ ${esc(a.name || 'Tienda')}</div>
        <select data-stub-acc="${esc(a.id)}" style="border:1px solid #d8dbe1;border-radius:8px;padding:4px 8px;font:inherit;font-size:12px;outline:none">
          <option value="" ${val === '' ? 'selected' : ''}>Heredar (${on ? 'talón activo' : 'sin talón'})</option>
          <option value="1" ${val === '1' ? 'selected' : ''}>Talón activo</option>
          <option value="0" ${val === '0' ? 'selected' : ''}>Sin talón</option>
        </select>
      </div>`;
    }).join('');
    return `<div style="border:1px solid #e5e7ec;border-radius:10px;padding:10px 13px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap">
        <div style="font-size:13px;font-weight:700">${esc(p.label)}</div>
        ${toggle}
      </div>
      ${accRows}
      ${!p.native && !accs.length ? '<div style="font-size:11.5px;color:var(--muted2);padding:6px 0 0 22px">↳ sin tiendas dadas de alta todavía (el ajuste del marketplace aplicará a las que conectes)</div>' : ''}
    </div>`;
  }).join('');
}
async function updateStubPreviews() {
  const prov = $('stub-prev-provider') ? $('stub-prev-provider').value : 'walmart';
  const n = parseInt($('stub-count').value, 10) || 8;
  const bust = Date.now();
  // Estampado (unificación con el Extractor): folio/empresa/lote aplican a
  // los 3 marketplaces (Walmart/TikTok dentro del talón, Cambio 3.2); el
  // punto rojo de saldo negativo sigue siendo exclusivo de ML (sin fuente de
  // saldo todavía para Walmart/TikTok).
  const isMl = prov === 'ml';
  $('enrich-controls').style.display = 'flex';
  $('enrich-low-wrap').style.display = isMl ? 'flex' : 'none';
  const comp = encodeURIComponent($('enrich-company').value || 'INMATMEX');
  const folio = parseInt($('enrich-folio').value, 10) || 101;
  const low = isMl && $('enrich-low').checked ? '1' : '0';
  const enrich = `&company=${comp}&folio=${folio}&low=${low}`;
  $('stub-frame-label').src = `/api/stub-preview?provider=${prov}${enrich}&_=${bust}`;
  $('stub-frame-sheet').src = `/api/layout-preview?count=${n}&provider=${prov}${enrich}&_=${bust}`;
  try {
    const pl = await api(`/api/layout-plan?count=${n}&provider=${prov}`);
    $('stub-plan-text').textContent =
      `→ ${pl.labels_per_sheet}/hoja → ${pl.sheets} hoja${pl.sheets === 1 ? '' : 's'} · ${pl.label_cm}${pl.stub ? ' + talón' : ''} (${pl.size_source})`;
  } catch { $('stub-plan-text').textContent = ''; }
}

// ===== Modo automático (reglas por horario) =====
const AUTO_STATE = {
  off:               ['#aab0bb', 'Desactivado'],
  printing:          ['#2f6bf0', 'Imprimiendo'],
  paused:            ['#8a92a0', 'En pausa'],
  idle_ok:           ['#15a05a', 'Activo · sin pendientes'],
  waiting_interval:  ['#15a05a', 'Activo'],
  waiting_fill:      ['#b07400', 'Esperando etiquetas'],
  no_conn:           ['#c43232', 'Sin conexión'],
  no_printer:        ['#c43232', 'Sin impresora'],
  printer_not_ready: ['#c43232', 'Impresora no lista'],
  no_size:           ['#b07400', 'Falta calibrar'],
  error:             ['#c43232', 'Error'],
};
const DAYS_ES = ['Lun','Mar','Mié','Jue','Vie','Sáb','Dom'];
const MODE_COLOR = { ahorro:'#15824a', forzar:'#b07400', pausa:'#c4c9d2' };
const MODE_NAME = { ahorro:'Ahorro', forzar:'Forzar', pausa:'Pausa' };
function hmMin(s) { const [h,m] = (s||'0:0').split(':'); return (+h)*60 + (+m); }
function minHm(n) { return String(Math.floor(n/60)).padStart(2,'0') + ':' + String(n%60).padStart(2,'0'); }

async function loadAuto() {
  try { state.auto = await api('/api/auto'); } catch { state.auto = null; }
  const dot = $('auto-nav-dot');
  if (dot) dot.style.display = (state.auto && state.auto.config.enabled) ? 'block' : 'none';
}
function fillAutoForm() {
  if (!state.auto) return;
  const active = document.activeElement;
  if (active && ['auto-enabled','auto-interval','auto-threshold'].includes(active.id)) return;
  const c = state.auto.config;
  $('auto-enabled').checked = c.enabled;
  $('auto-interval').value = c.interval_min;
  $('auto-threshold').value = c.multiunit_threshold;
}
// Solo la parte VIVA de la pestaña Automático (punto, mensaje, pill,
// requisitos). Es lo único que refresca el bucle en tiempo real: el
// formulario, la rejilla y el editor de reglas NO se tocan para no pisar
// lo que el operador esté editando.
function renderAutoStatus() {
  if (!state.auto) return;
  const s = state.auto;
  const [color, label] = AUTO_STATE[s.state] || AUTO_STATE.off;
  const on = s.config.enabled;
  $('auto-dot').style.background = color;
  $('auto-dot').style.animation = s.state === 'printing' ? 'efpulse 1.4s infinite' : 'none';
  $('auto-state-msg').textContent = s.message || '—';
  const pill = $('auto-pill');
  pill.style.background = on ? 'rgba(47,107,240,.1)' : '#eef0f3';
  pill.style.color = on ? 'var(--accent)' : '#6a7280';
  pill.querySelector('.pdot').style.background = on ? color : '#aab0bb';
  const modeTxt = s.mode_now ? ` · ahora: ${MODE_NAME[s.mode_now]||s.mode_now}` : '';
  $('auto-pill-label').textContent = (on ? label : 'Desactivado') + (on ? modeTxt : '');
  renderAutoReqs();
}

function renderAuto() {
  if (!state.auto) return;
  fillAutoForm();
  renderAutoStatus();
  renderAutoGrid(state.auto.config.rules);
  if (!state.rulesDraft) state.rulesDraft = JSON.parse(JSON.stringify(state.auto.config.rules));
  if (state.autoView === 'avanzada') renderAutoEditor();
}
function autoDayBar(segs) {
  const sorted = [...(segs||[])].sort((a,b) => hmMin(a.start) - hmMin(b.start));
  const parts = []; let cur = 0;
  for (const s of sorted) {
    const a = hmMin(s.start), b = hmMin(s.end);
    if (a > cur) parts.push({ mode:'pausa', a:cur, b:a, label:'' });
    parts.push({ mode:s.mode, a, b, label:s.label||'' });
    cur = b;
  }
  if (cur < 1440) parts.push({ mode:'pausa', a:cur, b:1440, label:'' });
  return parts.map(p => `<div title="${minHm(p.a)}–${minHm(p.b)} · ${MODE_NAME[p.mode]}${p.label?' ('+esc(p.label)+')':''}" style="flex:${p.b-p.a} 0 0;background:${MODE_COLOR[p.mode]};height:22px"></div>`).join('');
}
function renderAutoGrid(rules) {
  const box = $('auto-grid'); if (!box) return;
  const days = (rules && rules.days) || {};
  const axis = `<div style="display:flex;align-items:center;gap:10px;margin-top:4px"><span style="width:34px"></span><div style="flex:1;display:flex;justify-content:space-between;font-size:10px;color:var(--label);font-family:var(--mono)"><span>0</span><span>6</span><span>12</span><span>18</span><span>24</span></div></div>`;
  box.innerHTML = DAYS_ES.map((n,d) => `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <span style="width:34px;font-size:12px;font-weight:600;color:var(--ink2)">${n}</span>
      <div style="flex:1;display:flex;border-radius:5px;overflow:hidden;border:1px solid var(--line)">${autoDayBar(days[String(d)])}</div>
    </div>`).join('') + axis;
}
function autoSegRow(s) {
  s = s || { start:'00:00', end:'23:59', mode:'ahorro', label:'' };
  const opt = m => `<option value="${m}"${s.mode===m?' selected':''}>${MODE_NAME[m]}</option>`;
  return `<div class="auto-seg" style="display:flex;gap:7px;align-items:center;margin-bottom:6px;flex-wrap:wrap">
    <input type="time" class="seg-start hist-inp" value="${esc(s.start)}">
    <span style="color:var(--muted)">→</span>
    <input type="time" class="seg-end hist-inp" value="${esc(s.end)}">
    <select class="seg-mode hist-inp">${opt('ahorro')}${opt('forzar')}${opt('pausa')}</select>
    <input type="text" class="seg-label hist-inp" placeholder="etiqueta (opcional)" value="${esc(s.label||'')}" style="flex:1;min-width:120px">
    <button type="button" class="xbtn auto-del" title="Quitar tramo">✕</button>
  </div>`;
}
function renderAutoEditor() {
  const box = $('auto-editor'); if (!box) return;
  const days = (state.rulesDraft && state.rulesDraft.days) || {};
  box.innerHTML = DAYS_ES.map((n,d) => `
    <div style="border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px">
      <div style="font-weight:600;margin-bottom:8px">${n}</div>
      <div class="auto-day-rows" data-day="${d}">${(days[String(d)]||[]).map(autoSegRow).join('')}</div>
      <button type="button" class="btn btn-ghost auto-add" data-day="${d}" style="padding:5px 10px;font-size:12px;margin-top:4px">+ Agregar tramo</button>
    </div>`).join('');
}
function collectRules() {
  const days = {};
  document.querySelectorAll('.auto-day-rows').forEach(dc => {
    const segs = [];
    dc.querySelectorAll('.auto-seg').forEach(row => {
      const start = row.querySelector('.seg-start').value;
      const end = row.querySelector('.seg-end').value;
      const mode = row.querySelector('.seg-mode').value;
      const label = row.querySelector('.seg-label').value;
      if (start && end) segs.push({ start, end, mode, label });
    });
    days[dc.dataset.day] = segs;
  });
  return { days };
}
function renderAutoReqs() {
  const box = $('auto-reqs'); if (!box) return;
  const c = (state.auto && state.auto.checks) || {};
  const reqs = [
    [c.connected, 'Cuenta de Mercado Libre conectada', 'Conéctala en «Conexión Mercado Libre».'],
    [c.printer_ready, 'Impresora predeterminada lista', 'Enciende la impresora y márcala como predeterminada en «Dispositivos».'],
    [c.size_known, 'Tamaño de etiqueta aprendido', 'Imprime una etiqueta manualmente una vez para calibrar el acomodo.'],
  ];
  box.innerHTML = reqs.map(([ok, txt, hint]) => `
    <div style="display:flex;align-items:flex-start;gap:9px">
      <span style="flex:0 0 auto;width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;background:${ok?'#15a05a':'#c9ced6'}">${ok?'✓':'!'}</span>
      <div style="min-width:0"><div style="font-weight:${ok?'500':'600'};color:${ok?'var(--ink2)':'var(--text)'}">${esc(txt)}</div>${ok?'':`<div style="font-size:12px;color:var(--muted)">${esc(hint)}</div>`}</div>
    </div>`).join('');
}
async function saveAuto(rulesData) {
  const fd = new FormData();
  fd.append('enabled', $('auto-enabled').checked ? '1' : '0');
  fd.append('interval_min', $('auto-interval').value || '30');
  fd.append('multiunit_threshold', $('auto-threshold').value || '1');
  if (rulesData) fd.append('rules', JSON.stringify(rulesData));
  return api('/api/auto', { method:'POST', body:fd });
}
async function loadPreset() {
  try {
    const d = await api('/api/auto/default');
    state.rulesDraft = d;
    await saveAuto(d); await loadAuto(); renderAuto();
    showBanner('Reglas recomendadas cargadas.', 'success');
    log('OK','automatico','Reglas recomendadas cargadas.');
  } catch (e) { showBanner('No se pudo: ' + e.message, 'error'); }
}

// ===== Separación (multi-unidad) =====
function renderSeparacion() {
  const list = manualList();
  const badge = $('nav-manual-count');
  if (badge) { badge.textContent = list.length; badge.style.display = list.length ? 'inline-block' : 'none'; }
  const cnt = $('manual-count'); if (cnt) cnt.textContent = list.length;
  const box = $('manual-rows'); if (!box) return;
  // limpia selección de los que ya no están
  const present = new Set(list.map(o => String(o.shipment_id)));
  for (const id of [...state.manualSel]) if (!present.has(id)) state.manualSel.delete(id);
  if (!list.length) { box.innerHTML = `<div class="empty" style="padding:44px 0"><span>No hay pedidos multi-unidad pendientes.</span></div>`; return; }
  box.innerHTML = list.map(o => {
    const sid = String(o.shipment_id), on = state.manualSel.has(sid);
    const prod = (o.products && o.products[0]) || { title:'—' };
    // Distintivo de tienda: de qué cuenta viene el pedido (clave en multi-cuenta;
    // además, «Separar en ML» solo aplica a los de Mercado Libre).
    const [pbg, pfg] = PROVIDER_CHIP[o.provider] || ['#eef0f3', '#5b6472'];
    const store = `<span class="chip" style="background:${pbg};color:${pfg};flex:0 0 auto;max-width:150px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc((PROVIDER_LABEL[o.provider] || o.provider || '') + ' · ' + (o.account_name || ''))}">${esc(o.account_name || PROVIDER_LABEL[o.provider] || '—')}</span>`;
    return `<div class="log-row" style="gap:12px">
      <label style="display:flex;flex:0 0 auto"><input type="checkbox" class="manual-chk" data-sid="${esc(sid)}" ${on?'checked':''} style="width:16px;height:16px;accent-color:var(--accent)"></label>
      <span style="width:92px;font-family:var(--mono);font-size:12px;color:var(--ink3)">#${esc(o.order_id)}</span>
      ${store}
      <span style="flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(prod.title)}</span>
      <span class="chip" style="background:#fbf2dd;color:#b07400;flex:0 0 auto">${esc(o.units)} uds</span>
      <span style="width:160px;color:var(--ink3);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(o.buyer_name)}</span>
    </div>`;
  }).join('');
  const allChk = $('manual-all'); if (allChk) allChk.checked = list.length > 0 && list.every(o => state.manualSel.has(String(o.shipment_id)));
}

// ===== Topbar / estado =====
function updateConnPills() {
  const st = state.status;
  const n = st.accounts_connected || 0;
  const [bg,fg,dot,label] = st.connected
    ? ['#eaf6ef','#15824a','#1ba85b', n > 1 ? `${n} tiendas` : 'Tienda conectada']
    : (st.accounts_total ? ['#fbf2dd','#b07400','#e8a200','Sin conectar']
                         : ['#eef0f3','#6a7280','#aab0bb','Sin tiendas']);
  const pill = $('conn-pill'); pill.style.background=bg; pill.style.color=fg;
  pill.querySelector('.pdot').style.background=dot; $('conn-pill-label').textContent=label;
}
async function refreshStatus() {
  try { state.status = await api('/api/status'); } catch (e) { /* silencioso */ }
  updateConnPills();
}

// ===== Datos =====
async function loadOrders(opts = {}) {
  if (!state.status.connected) { state.orders = []; renderCola(); return; }
  try {
    const data = await api('/api/orders');
    const newOrders = data.orders || [];
    if (typeof data.printed_today === 'number') state.printedToday = data.printed_today;
    state.printedSplit = data.printed_split || null;
    state.accountsMeta = data.accounts || [];
    state.ordersErrors = data.errors || [];
    // config del talón (para el chip ✂ en las tarjetas), una sola vez
    if (!state.stubCfg) {
      api('/api/stub-config')
        .then(d => { state.stubCfg = { providers: d.providers || {}, accounts: d.accounts || {} }; renderCola(); })
        .catch(() => {});
    }
    const newIds = new Set(newOrders.map(o => String(o.shipment_id)));
    const added = state._orderIds ? newOrders.filter(o => !state._orderIds.has(String(o.shipment_id))).length : 0;
    const removed = state._orderIds ? [...state._orderIds].filter(id => !newIds.has(id)).length : 0;
    state.orders = newOrders; state._orderIds = newIds; state.lastSync = Date.now();
    if (opts.manual) log('OK', 'cola', `Cola sincronizada · ${newOrders.length} venta(s) de ${state.accountsMeta.length} tienda(s).`);
    else if (state._ordersInit) {
      if (added) log('INFO', 'cola', `${added} venta(s) nueva(s) lista(s) para enviar.`);
      if (removed) log('INFO', 'cola', `${removed} venta(s) salieron de la cola (enviadas/canceladas).`);
    }
    // Los errores por tienda NO se registran aquí: la cola se sondea cada
    // ~15 s y lo inundaría todo; el backend ya los loguea deduplicados
    // (módulo 'tiendas': avisa al aparecer/cambiar y al recuperarse).
    state._ordersInit = true;
  } catch (e) {
    if (opts.manual) showBanner('No se pudieron cargar las ventas: ' + e.message, 'error');
    log('ERROR', 'cola', 'Error al sincronizar la cola: ' + e.message);
  }
  renderCola();
}

// ===== Navegación =====
function go(tab) {
  state.tab = tab;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab===tab));
  document.querySelectorAll('.tab').forEach(s => s.classList.toggle('active', s.dataset.tab===tab));
  $('page-title').textContent = TITLES[tab][0];
  $('page-sub').textContent = TITLES[tab][1];
  if (tab === 'cola') renderCola();
  else if (tab === 'separacion') { loadOrders().then(renderSeparacion); }
  else if (tab === 'automatico') { loadAuto().then(renderAuto); }
  else if (tab === 'dispositivos') loadPrinters({ force: true });
  else if (tab === 'conexion') { loadAccounts().then(renderConexion); }
  else if (tab === 'etiquetas') { loadStubCard(); }
  else if (tab === 'historial') renderHistorial();
  else if (tab === 'logs') renderLogs();
}

// ===== Init =====
function init() {
  localStorage.removeItem('ef_logs');   // los logs ahora viven en el servidor

  // reloj
  const tick = () => { $('clock').textContent = new Date().toLocaleTimeString('es-MX', { hour12:false }); };
  tick(); setInterval(tick, 1000);

  // navegación
  document.querySelectorAll('.nav-btn').forEach(b => b.addEventListener('click', () => go(b.dataset.tab)));

  // cola (kanban)
  $('btn-reload').addEventListener('click', () => loadOrders({ manual: true }));
  $('btn-print-pending').addEventListener('click', printAllToday);
  $('btn-print-selected').addEventListener('click', printSelected);
  // operador que imprime (unificación con el Extractor): se recuerda entre
  // sesiones, igual que impresora/formato — evita retipearlo en cada lote.
  $('op-operador').value = state.operador;
  $('op-operador').addEventListener('input', e => {
    state.operador = e.target.value;
    if (state.operador.trim()) localStorage.setItem('ef_operador', state.operador);
    else localStorage.removeItem('ef_operador');
  });
  $('btn-select-all').addEventListener('click', () => {
    todayList().forEach(o => state.selected.add(String(o.shipment_id)));
    renderCola();
  });
  // Próximos: solo permite adelantar una etiqueta a «A imprimir»
  $('col-next').addEventListener('click', e => {
    const b = e.target.closest('[data-act="advance"]'); if (!b) return;
    state.advanced.add(b.dataset.sid); renderCola();
  });
  // A imprimir: la tarjeta marca/desmarca el lote; ✕ devuelve una adelantada
  $('col-sel').addEventListener('click', e => {
    const un = e.target.closest('[data-act="unadvance"]');
    if (un) {
      state.advanced.delete(un.dataset.sid);
      state.selected.delete(un.dataset.sid);
      renderCola(); return;
    }
    const row = e.target.closest('[data-act="toggle"]'); if (!row) return;
    const sid = row.dataset.sid;
    if (state.selected.has(sid)) state.selected.delete(sid);
    else state.selected.add(sid);
    renderCola();
  });
  $('link-history').addEventListener('click', () => go('historial'));
  // reimprimir desde la columna Impresos (⟳)
  $('col-done').addEventListener('click', async e => {
    const b = e.target.closest('[data-act="reprint-done"]'); if (!b) return;
    await printOne({ shipment_id: b.dataset.sid, order_id: b.dataset.oid, buyer_name: b.dataset.buyer,
                     account_id: b.dataset.aid || '', account_name: b.dataset.aname || '',
                     products: b.dataset.prod ? [{ title: b.dataset.prod, quantity: 1 }] : [] }, b.dataset.fmt);
  });

  // filtro de tienda
  $('store-chips').addEventListener('click', e => {
    const b = e.target.closest('[data-store]'); if (!b) return;
    state.storeFilter = b.dataset.store; state.selected.clear(); renderCola();
  });

  // buscador de etiquetas (Cola): filtra las tres columnas mientras escribes
  const qs = $('q-search');
  qs.addEventListener('input', () => {
    clearTimeout(qs._t);
    qs._t = setTimeout(() => { state.searchQ = qs.value.trim(); renderCola(); }, 150);
  });
  qs.addEventListener('keydown', e => {
    if (e.key === 'Escape') { qs.value = ''; state.searchQ = ''; renderCola(); }
  });
  $('q-search-clear').addEventListener('click', () => {
    qs.value = ''; state.searchQ = ''; renderCola(); qs.focus();
  });
  $('store-errors').addEventListener('click', e => {
    if (e.target.closest('[data-goto="conexion"]')) go('conexion');
  });

  // formato (toggle) y control del lote
  $('format-seg').addEventListener('click', e => { const b = e.target.closest('[data-format]'); if (b) setFormat(b.dataset.format); });
  $('btn-batch-stop').addEventListener('click', async () => { await api('/api/batch/stop', { method:'POST' }).catch(()=>{}); });
  $('btn-batch-close').addEventListener('click', () => { state.batch = null; renderBatch(); });
  $('btn-layout-preview').addEventListener('click', () => {
    const count = selectedList().length || pendingPool().length || 4;
    window.open('/api/layout-preview?count=' + count, '_blank', 'noopener');
  });

  // automático: activar / parámetros
  $('auto-enabled').addEventListener('change', async () => {
    try { const r = await saveAuto(); await loadAuto(); renderAuto();
      showBanner(r.config.enabled ? 'Impresión automática activada.' : 'Impresión automática desactivada.', 'success');
    } catch (e) { showBanner('No se pudo: ' + e.message, 'error'); $('auto-enabled').checked = !$('auto-enabled').checked; }
  });
  $('auto-interval').addEventListener('change', async () => {
    try { await saveAuto(); showBanner('Intervalo guardado.', 'success'); } catch (e) { showBanner(e.message, 'error'); }
  });
  $('auto-threshold').addEventListener('change', async () => {
    try { await saveAuto(); await loadOrders(); renderSeparacion(); showBanner('Umbral guardado.', 'success'); } catch (e) { showBanner(e.message, 'error'); }
  });
  // vista básica / avanzada
  $('auto-view-seg').addEventListener('click', e => {
    const b = e.target.closest('[data-view]'); if (!b) return;
    state.autoView = b.dataset.view;
    document.querySelectorAll('#auto-view-seg .seg-btn').forEach(x => x.classList.toggle('active', x.dataset.view === state.autoView));
    $('auto-basic').style.display = state.autoView === 'basica' ? 'block' : 'none';
    $('auto-advanced').style.display = state.autoView === 'avanzada' ? 'block' : 'none';
    if (state.autoView === 'avanzada') { state.rulesDraft = JSON.parse(JSON.stringify(state.auto.config.rules)); renderAutoEditor(); }
  });
  // editor de reglas
  $('auto-editor').addEventListener('click', e => {
    const add = e.target.closest('.auto-add');
    if (add) { document.querySelector(`.auto-day-rows[data-day="${add.dataset.day}"]`).insertAdjacentHTML('beforeend', autoSegRow()); return; }
    const del = e.target.closest('.auto-del');
    if (del) { del.closest('.auto-seg').remove(); return; }
  });
  $('auto-save').addEventListener('click', async () => {
    try { await saveAuto(collectRules()); await loadAuto(); renderAuto();
      showBanner('Reglas guardadas.', 'success');
    } catch (e) { showBanner('No se pudieron guardar: ' + e.message, 'error'); }
  });
  $('auto-preset').addEventListener('click', loadPreset);
  $('auto-preset-2').addEventListener('click', loadPreset);

  // separación (multi-unidad)
  $('manual-all').addEventListener('change', e => {
    const list = manualList();
    if (e.target.checked) list.forEach(o => state.manualSel.add(String(o.shipment_id)));
    else state.manualSel.clear();
    renderSeparacion();
  });
  $('manual-rows').addEventListener('change', e => {
    const c = e.target.closest('.manual-chk'); if (!c) return;
    if (c.checked) state.manualSel.add(c.dataset.sid); else state.manualSel.delete(c.dataset.sid);
    renderSeparacion();
  });
  $('manual-print').addEventListener('click', () => {
    const sel = manualList().filter(o => state.manualSel.has(String(o.shipment_id)));
    if (!sel.length) { showBanner('Selecciona al menos un pedido.', 'info'); return; }
    startBatch(sel, 'Multi-unidad (manual)');
    state.manualSel.clear();
  });
  $('manual-split').addEventListener('click', async () => {
    const chosen = manualList().filter(o => state.manualSel.has(String(o.shipment_id)));
    if (!chosen.length) { showBanner('Selecciona al menos un pedido.', 'info'); return; }
    // La separación es una función de Mercado Libre: Walmart/TikTok no la tienen.
    const sel = chosen.filter(o => o.provider === 'ml');
    const skipped = chosen.length - sel.length;
    if (!sel.length) { showBanner('Separar solo aplica a pedidos de Mercado Libre (los seleccionados son de otra tienda).', 'error'); return; }
    if (!confirm(`Separar ${sel.length} pedido(s) en Mercado Libre.` +
                 (skipped ? `\n(${skipped} seleccionado(s) de Walmart/TikTok se omiten: no soportan separación.)` : '') +
                 `\n\nEs IRREVERSIBLE y notifica al comprador. ¿Continuar?`)) return;
    const qStr = prompt('¿Cuántas unidades separar a un segundo paquete (por pedido)?', '1');
    const q = parseInt(qStr, 10); if (!q || q < 1) return;
    let ok = 0, fail = 0, lastErr = '';
    for (const o of sel) {
      const qty = Math.max(1, Math.min(q, (o.units || 2) - 1));
      try {
        const fd = new FormData();
        fd.append('shipment_id', o.shipment_id); fd.append('order_id', o.order_id);
        fd.append('account_id', o.account_id || '');
        fd.append('quantity', qty); fd.append('reason', 'DIMENSIONS_EXCEEDED');
        await api('/api/auto/split', { method:'POST', body:fd }); ok++;
      } catch (e) { fail++; lastErr = e.message; }
    }
    showBanner(`Separadas: ${ok}${fail ? ` · Errores: ${fail} (${lastErr})` : ''}`,
               fail ? 'error' : 'success');
    state.manualSel.clear(); await loadOrders(); renderSeparacion();
  });
  $('risk-review').addEventListener('click', () => { state.histFilter = { from:'', to:'', format:'', result:'risk' }; go('historial'); const r=$('hist-result'); if(r) r.value='risk'; });

  // historial: filtros y reimpresión
  $('hist-apply').addEventListener('click', () => {
    state.histFilter = {
      from: $('hist-from').value, to: $('hist-to').value,
      format: $('hist-format').value, result: $('hist-result').value,
    };
    renderHistorial();
  });
  $('hist-clear').addEventListener('click', () => {
    $('hist-from').value = ''; $('hist-to').value = '';
    $('hist-format').value = ''; $('hist-result').value = '';
    state.histFilter = { from:'', to:'', format:'', result:'' };
    renderHistorial();
  });
  $('hist-rows').addEventListener('click', async e => {
    const b = e.target.closest('[data-act="reprint"]'); if (!b) return;
    const order = { shipment_id: b.dataset.sid, order_id: b.dataset.oid,
                    buyer_name: b.dataset.buyer,
                    account_id: b.dataset.aid || '', account_name: b.dataset.aname || '',
                    products: b.dataset.prod ? [{ title: b.dataset.prod, quantity: 1 }] : [] };
    await printOne(order, b.dataset.fmt);
    renderHistorial();
  });

  // selector de impresora (barra de la Cola)
  $('printer-select').addEventListener('change', e => {
    state.printer = e.target.value;
    if (state.printer) localStorage.setItem('ef_printer', state.printer); else localStorage.removeItem('ef_printer');
    updatePrinterStatus();
    log('INFO','impresion', state.printer ? `Impresora destino: «${state.printer}».` : 'Impresora destino: predeterminada del sistema.');
  });

  // dispositivos
  $('btn-printers-refresh').addEventListener('click', loadPrinters);
  $('btn-scan').addEventListener('click', scanDevices);
  $('add-net-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      await api('/api/printers', { method:'POST', body:new FormData(e.target) });
      e.target.reset(); showBanner('Impresora de red agregada.', 'success');
      await loadPrinters();
    } catch (err) { showBanner('No se pudo agregar: ' + err.message, 'error'); }
  });
  $('printer-cards').addEventListener('click', async e => {
    const b = e.target.closest('[data-act]'); if (!b) return;
    const name = b.dataset.name, act = b.dataset.act;
    if (act === 'toggle-off') { state.showOffPrinters = !state.showOffPrinters; renderPrinters(); return; }
    try {
      if (act === 'test') {
        const r = await api(`/api/printers/${encodeURIComponent(name)}/test`, { method:'POST' });
        showDiag(r.diagnostics || {});
        const cats = (r.diagnostics && r.diagnostics.verdict || []).map(v => v.cat).join(', ');
        log('OK','dispositivos',`Prueba enviada a «${name}» (job ${r.job}). Diagnóstico: ${cats || '—'}.`);
      }
      else if (act === 'default') { await api(`/api/printers/${encodeURIComponent(name)}/default`, { method:'POST' }); log('OK','dispositivos',`«${name}» fijada como predeterminada.`); await loadPrinters({ force:true }); }
      else if (act === 'undefault') { await api(`/api/printers/${encodeURIComponent(name)}/undefault`, { method:'POST' }); log('INFO','dispositivos',`Predeterminada quitada (ninguna fijada).`); await loadPrinters({ force:true }); }
      else if (act === 'delete') { if (!confirm(`¿Eliminar la impresora «${name}»?`)) return; await api(`/api/printers/${encodeURIComponent(name)}`, { method:'DELETE' }); await loadPrinters({ force:true }); }
    } catch (err) {
      if (act === 'test' && err.status === 409) showBanner(err.message, 'error');
      else if (act === 'test') showCritical(`Prueba en «${name}»: ${err.message}`);
      else showBanner('Error: ' + err.message, 'error');
      log('ERROR','dispositivos',`Acción «${act}» en «${name}»: ${err.message}`);
    }
  });

  // logs: los filtros se construyen y cablean en ensureLogFilters()
  ensureLogFilters();

  // cuentas / tiendas
  const DEFAULT_REDIRECT = location.origin + '/callback';
  // El campo Redirect URI solo aplica a proveedores que lo registran aquí (ML);
  // Walmart va por client_credentials y TikTok lo registra en su Partner Center.
  const ACC_FORM_HINTS = {
    walmart: ['Client ID', 'Client Secret',
      'Walmart no usa Redirect URI: genera el Client ID y Client Secret en developer.walmart.com (mercado MX) y pulsa «Conectar» tras guardar.'],
    tiktok: ['App Key', 'App Secret',
      'TikTok Shop: crea la app en partner.tiktokshop.com y pega su App Key y App Secret. El Redirect se registra allá; si al autorizar no regresas aquí, copia el parámetro «code» de la URL de retorno y usa «Canjear code».'],
  };
  function syncAccFormFields() {
    const p = state.providers.find(x => x.id === $('acc-provider').value) || {};
    const needsRedirect = p.needs_redirect !== false;
    const hint = ACC_FORM_HINTS[p.id];
    $('acc-redirect-field').style.display = needsRedirect ? '' : 'none';
    $('acc-cc-hint').style.display = hint ? '' : 'none';
    $('acc-cc-hint').textContent = hint ? hint[2] : '';
    $('acc-app-id-label').textContent = hint ? hint[0] : 'App ID (Client ID)';
    $('acc-secret-label').textContent = hint ? hint[1] : 'Client Secret';
    $('acc-redirect').required = needsRedirect;
  }
  function openAccForm(acc) {
    // llena el selector de proveedor (solo disponibles)
    const sel = $('acc-provider');
    const avail = state.providers.filter(p => p.available);
    sel.innerHTML = (avail.length ? avail : [{ id:'ml', label:'Mercado Libre' }])
      .map(p => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');
    $('acc-form-title').textContent = acc ? `Editar «${acc.name || 'tienda'}»` : 'Agregar tienda';
    $('acc-id').value = acc ? acc.id : '';
    $('acc-provider').value = acc ? acc.provider : (avail[0] ? avail[0].id : 'ml');
    $('acc-name').value = acc ? (acc.name || '') : '';
    $('acc-app-id').value = acc ? (acc.app_id || '') : '';
    $('acc-secret').value = '';
    $('acc-secret').placeholder = acc && acc.has_secret ? '•••• (vacío = conservar)' : '';
    $('acc-redirect').value = acc ? (acc.redirect_uri || DEFAULT_REDIRECT) : DEFAULT_REDIRECT;
    syncAccFormFields();
    $('acc-form-card').style.display = 'block';
    $('acc-form-card').scrollIntoView({ behavior:'smooth', block:'center' });
  }
  $('acc-provider').addEventListener('change', syncAccFormFields);
  $('acc-add-open').addEventListener('click', () => openAccForm(null));
  $('acc-cancel').addEventListener('click', () => { $('acc-form-card').style.display = 'none'; });
  $('acc-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      await api('/api/accounts', { method:'POST', body:new FormData(e.target) });
      $('acc-form-card').style.display = 'none';
      await loadAccounts(); renderConexion();
      showBanner('Tienda guardada.', 'success');
    } catch (err) { showBanner('No se pudo guardar: ' + err.message, 'error'); }
  });
  $('acc-manual-form').addEventListener('submit', async e => {
    e.preventDefault();
    const id = $('acc-manual-form').dataset.acc, code = $('acc-manual-code').value.trim();
    if (!id || !code) return;
    try {
      const fd = new FormData(); fd.append('code', code);
      await api(`/api/accounts/${encodeURIComponent(id)}/connect/manual`, { method:'POST', body:fd });
      $('acc-manual-code').value = ''; $('acc-manual-card').style.display = 'none';
      await loadAccounts(); await refreshStatus(); renderConexion();
      showBanner('Código canjeado. Tienda conectada.', 'success');
    } catch (err) { showBanner('No se pudo canjear: ' + err.message, 'error'); }
  });
  $('accounts-list').addEventListener('click', async e => {
    const b = e.target.closest('[data-acc]'); if (!b) return;
    const id = b.dataset.acc, act = b.dataset.act;
    const acc = state.accounts.find(a => a.id === id);
    try {
      if (act === 'connect') {
        const res = await api(`/api/accounts/${encodeURIComponent(id)}/connect`);
        if (res.connected) {
          // Proveedor sin redirect (Walmart): las credenciales ya quedaron validadas.
          await loadAccounts(); await refreshStatus(); renderConexion();
          showBanner('Credenciales validadas. Tienda conectada.', 'success');
        } else {
          window.location.href = res.authorization_url;
        }
      } else if (act === 'refresh') {
        await api(`/api/accounts/${encodeURIComponent(id)}/refresh`, { method:'POST' });
        await loadAccounts(); renderConexion(); showBanner('Token renovado.', 'success');
      } else if (act === 'disconnect') {
        await api(`/api/accounts/${encodeURIComponent(id)}/disconnect`, { method:'POST' });
        await loadAccounts(); await refreshStatus(); renderConexion(); showBanner('Tienda desconectada.', 'info');
      } else if (act === 'toggle') {
        const fd = new FormData(); fd.append('enabled', acc.enabled ? '0' : '1');
        await api(`/api/accounts/${encodeURIComponent(id)}/enabled`, { method:'POST', body:fd });
        await loadAccounts(); renderConexion();
      } else if (act === 'edit') {
        openAccForm(acc);
      } else if (act === 'manual') {
        $('acc-manual-form').dataset.acc = id; $('acc-manual-name').textContent = acc.name || 'tienda';
        $('acc-manual-card').style.display = 'block'; $('acc-manual-card').scrollIntoView({ behavior:'smooth', block:'center' });
      } else if (act === 'delete') {
        if (!confirm(`¿Eliminar la tienda «${acc.name || id}»? Se borran sus credenciales.`)) return;
        await api(`/api/accounts/${encodeURIComponent(id)}`, { method:'DELETE' });
        await loadAccounts(); await refreshStatus(); renderConexion(); showBanner('Tienda eliminada.', 'info');
      }
    } catch (err) { showBanner('Error: ' + err.message, 'error'); }
  });

  // etiquetas: talón por marketplace y por tienda + vista previa en vivo
  $('stub-rows').addEventListener('change', async e => {
    const fd = new FormData();
    const prov = e.target.closest('[data-stub-prov]');
    const acc = e.target.closest('[data-stub-acc]');
    if (!prov && !acc) return;
    try {
      let msg;
      if (prov) {
        fd.append('provider', prov.dataset.stubProv);
        fd.append('enabled', prov.checked ? '1' : '0');
        msg = `Talón ${prov.checked ? 'activado' : 'desactivado'} para ${PROVIDER_LABEL[prov.dataset.stubProv] || prov.dataset.stubProv}.`;
      } else {
        fd.append('account_id', acc.dataset.stubAcc);
        fd.append('enabled', acc.value);   // '' = heredar del marketplace
        msg = acc.value === '' ? 'La tienda hereda el ajuste del marketplace.'
          : `Talón ${acc.value === '1' ? 'activado' : 'desactivado'} para la tienda.`;
      }
      const d = await api('/api/stub-config', { method: 'POST', body: fd });
      state.stubCfg = { providers: d.providers || {}, accounts: d.accounts || {} };
      showBanner(msg, 'success');
      log('OK', 'etiquetas', msg);
      renderStubRows();
      updateStubPreviews();
    } catch (err) { showBanner('No se pudo guardar: ' + err.message, 'error'); loadStubCard(); }
  });
  $('stub-count').addEventListener('change', updateStubPreviews);
  $('stub-prev-provider').addEventListener('change', updateStubPreviews);

  // estampado (unificación con el Extractor): empresas desde el backend
  (async () => {
    try {
      const cfg = await api('/api/enrich-config');
      $('enrich-company').innerHTML = (cfg.companies || [])
        .map(c => `<option value="${esc(c)}" ${c === 'INMATMEX' ? 'selected' : ''}>${esc(c)}</option>`).join('');
      $('enrich-day').textContent = `folio de hoy: ${cfg.day_color}`;
      $('enrich-day').style.color = cfg.day_color;
    } catch { /* sin catálogo: el select queda vacío y el backend usa INMATMEX */ }
  })();
  $('enrich-company').addEventListener('change', updateStubPreviews);
  $('enrich-folio').addEventListener('change', updateStubPreviews);
  $('enrich-low').addEventListener('change', updateStubPreviews);

  // importar PDF de etiquetas (TikTok Shop / Walmart)
  const PDF_IMPORT_HINTS = {
    tiktok: 'PDF de guías: hoja Carta con 2 envíos (guía + packing list); se recorta cada guía y se lee su producto. El Picking List (PDF) adjunto sirve de segunda fuente/validación por Order ID.',
    walmart: 'Las guías FedEx de Walmart NO traen el producto. Adjunta el Excel «Pedidos_*.xlsx» del seller center: se cruza por PO/cliente (o por posición si la guía es imagen sin OCR). También cruza con la API si hay cuenta conectada; lo que falte se captura a mano.',
  };
  function syncPdfImportHint() {
    $('pdf-import-hint').textContent = PDF_IMPORT_HINTS[$('pdf-import-provider').value] || '';
  }
  syncPdfImportHint();
  $('pdf-import-provider').addEventListener('change', syncPdfImportHint);
  const INP = 'border:1px solid #d8dbe1;border-radius:8px;padding:5px 8px;font:inherit;font-size:12px;outline:none';
  function renderImportResult(d) {
    const box = $('pdf-import-result');
    const lay = d.layout || {};
    const editable = d.without_product > 0;
    const items = (d.items || []);
    const shown = editable ? items : items.slice(0, 6);
    const rows = shown.map((m, i) => {
      const who = [m.order_id ? '#' + m.order_id : '', m.buyer || '', m.tracking ? 'TRK ' + m.tracking : '']
        .filter(Boolean).join(' · ') || `guía ${i + 1}`;
      const p = (m.products || [])[0];
      if (p) {
        const SRC = { api: ' · API', manual: ' · manual', excel: ' · packing list', posicion: ' · ⚠ por posición' };
        const src = SRC[m.matched] || '';
        const extra = (m.products.length > 1) ? ` (+${m.products.length - 1} más)` : '';
        return `<div style="font-size:11.5px;color:${m.matched === 'posicion' ? '#b07400' : 'var(--muted)'};font-family:var(--mono);padding:3px 0">${esc(who)} → ${p.quantity || 1}× ${esc((p.title || '').slice(0, 44))}${extra}${src}</div>`;
      }
      const opts = (d.packing_orders || []).map((o, j) => {
        const p0 = o.products[0] || {};
        const extra = o.products.length > 1 ? ` +${o.products.length - 1}` : '';
        const lbl = [o.po || o.order_id, o.buyer, `${p0.quantity || 1}× ${(p0.title || '').slice(0, 34)}${extra}`]
          .filter(Boolean).join(' · ');
        return `<option value="${j}">${esc(lbl)}</option>`;
      }).join('');
      const picker = opts
        ? `<select data-f="pick" style="${INP};flex:1;min-width:230px"><option value="">— elegir pedido del packing list —</option>${opts}</select>` : '';
      return `<div data-imp-row="${i}" style="display:flex;gap:7px;align-items:center;flex-wrap:wrap;padding:5px 0;border-bottom:1px dashed #eceef2">
        <span style="font-size:11.5px;color:var(--muted);font-family:var(--mono);flex:0 0 200px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(who)}">${esc(who)}</span>
        ${picker}
        <input data-f="title" placeholder="Producto (ej. WPC Gris 1M)" style="${INP};flex:1;min-width:150px">
        <input data-f="sku" placeholder="SKU" style="${INP};width:110px">
        <input data-f="quantity" type="number" value="1" min="1" title="Piezas" style="${INP};width:56px">
      </div>`;
    }).join('');
    const warn = editable
      ? `<div style="font-size:11.5px;color:#b07400;margin:8px 0 2px">⚠ ${d.without_product} guía(s) sin producto: adjunta el packing list (Excel/PDF) o captúralo arriba y pulsa «Aplicar talones».${d.ocr === false ? ' · OCR no instalado (sudo apt install tesseract-ocr tesseract-ocr-spa) para leer PO/destinatario de guías escaneadas.' : ''}</div>` : '';
    const pk = d.packing
      ? `<div style="font-size:11.5px;color:var(--muted);margin-top:2px">Packing list: ${d.packing.orders} pedido(s) leídos · ${d.packing.matched} cruzado(s) por identificador${d.packing.positional ? ` · <span style="color:#b07400">${d.packing.positional} por POSICIÓN — verifica que el orden del PDF coincida con el del packing list antes de imprimir</span>` : ''}</div>` : '';
    box.innerHTML = `
      <div style="font-size:13px;font-weight:700;margin-bottom:6px">${d.guides} guías detectadas → ${lay.sheets || '?'} hoja(s) Carta (${lay.labels_per_sheet || '?'} por hoja)${d.stub ? ' · talón activo' : ''}</div>
      ${pk}
      ${rows}${!editable && items.length > 6 ? `<div style="font-size:11.5px;color:var(--muted)">… y ${items.length - 6} más</div>` : ''}
      ${warn}
      <div class="btn-row" style="margin-top:11px">
        ${editable ? '<button class="btn btn-accent" data-imp-act="apply">Aplicar talones</button>' : ''}
        <button class="btn btn-ghost" data-imp-act="view">Ver PDF listo</button>
        <button class="btn ${editable ? 'btn-ghost' : 'btn-accent'}" data-imp-act="print">Imprimir</button>
      </div>`;
  }
  $('pdf-import-btn').addEventListener('click', async () => {
    const f = $('pdf-import-file').files[0];
    const prov = $('pdf-import-provider').value;
    if (!f) { showBanner('Elige el PDF del seller center.', 'info'); return; }
    const box = $('pdf-import-result');
    box.style.display = 'block';
    box.innerHTML = '<span style="font-size:12.5px;color:var(--muted)">Procesando…</span>';
    try {
      const fd = new FormData(); fd.append('file', f); fd.append('provider', prov);
      const pk = $('pdf-import-packing').files[0];
      if (pk) fd.append('packing', pk);
      const d = await api('/api/labels/import', { method: 'POST', body: fd });
      state.pdfImport = d;
      renderImportResult(d);
    } catch (err) {
      box.innerHTML = `<span style="font-size:12.5px;color:#c0392b">${esc(err.message)}</span>`;
    }
  });
  // al elegir un pedido del packing list, refleja sus datos en los campos
  $('pdf-import-result').addEventListener('change', e => {
    const sel = e.target.closest('[data-f="pick"]'); if (!sel) return;
    const row = sel.closest('[data-imp-row]');
    const o = sel.value !== '' ? (state.pdfImport.packing_orders || [])[parseInt(sel.value, 10)] : null;
    const p = o ? (o.products[0] || {}) : {};
    row.querySelector('[data-f="title"]').value = p.title || '';
    row.querySelector('[data-f="sku"]').value = p.sku || '';
    row.querySelector('[data-f="quantity"]').value = p.quantity || 1;
  });
  $('pdf-import-result').addEventListener('click', async e => {
    const b = e.target.closest('[data-imp-act]'); if (!b || !state.pdfImport) return;
    const tok = state.pdfImport.token;
    if (b.dataset.impAct === 'apply') {
      const items = [];
      $('pdf-import-result').querySelectorAll('[data-imp-row]').forEach(row => {
        const pick = row.querySelector('[data-f="pick"]');
        const chosen = pick && pick.value !== '' ? (state.pdfImport.packing_orders || [])[parseInt(pick.value, 10)] : null;
        const title = row.querySelector('[data-f="title"]').value.trim();
        if (chosen) {
          items.push({
            index: parseInt(row.dataset.impRow, 10), products: chosen.products,
            order_id: chosen.po || chosen.order_id || '', buyer: chosen.buyer || '',
          });
        } else if (title) {
          items.push({
            index: parseInt(row.dataset.impRow, 10), title,
            sku: row.querySelector('[data-f="sku"]').value.trim(),
            quantity: parseInt(row.querySelector('[data-f="quantity"]').value, 10) || 1,
          });
        }
      });
      if (!items.length) { showBanner('Captura al menos un producto.', 'info'); return; }
      b.disabled = true;
      try {
        const r = await api(`/api/labels/import/${tok}/products`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ items }),
        });
        state.pdfImport = { ...state.pdfImport, ...r };
        renderImportResult(state.pdfImport);
        showBanner(`Talones aplicados a ${items.length} guía(s).`, 'success');
        log('OK', 'etiquetas', `Importación: ${items.length} talón(es) capturados a mano.`);
      } catch (err) { showBanner('No se pudo aplicar: ' + err.message, 'error'); b.disabled = false; }
      return;
    }
    if (b.dataset.impAct === 'view') { window.open(`/api/labels/import/${tok}/pdf`, '_blank'); return; }
    if (b.dataset.impAct === 'print') {
      const sheets = (state.pdfImport.layout || {}).sheets || '?';
      if (!confirm(`¿Imprimir ${state.pdfImport.guides} guías (${sheets} hojas)?`)) return;
      b.disabled = true;
      try {
        const fd = new FormData(); fd.append('printer', '');
        const r = await api(`/api/labels/import/${tok}/print`, { method: 'POST', body: fd });
        showBanner(`Enviado a impresora: ${r.printer}`, 'success');
      } catch (err) { showBanner('Error al imprimir: ' + err.message, 'error'); }
      b.disabled = false;
    }
  });

  // sello del sistema
  $('stamp-close').addEventListener('click', hideStamp);
  $('stamp-overlay').addEventListener('click', e => { if (e.target === $('stamp-overlay')) hideStamp(); });
  $('diag-close').addEventListener('click', hideDiag);
  $('diag-overlay').addEventListener('click', e => { if (e.target === $('diag-overlay')) hideDiag(); });

  // modal de saldo negativo (gate de auditoría)
  $('lowmargin-ack').addEventListener('change', e => { $('lowmargin-confirm').disabled = !e.target.checked; });
  $('lowmargin-cancel').addEventListener('click', hideLowMarginModal);
  $('lowmargin-confirm').addEventListener('click', () => {
    const cb = _lowMarginConfirm;
    hideLowMarginModal();
    if (cb) cb();
  });
  $('lowmargin-overlay').addEventListener('click', e => { if (e.target === $('lowmargin-overlay')) hideLowMarginModal(); });
  $('side-foot').addEventListener('click', () => showStamp({ msg: 'EtiquetaFlow · ' + location.host, critical: false }));

  // arranque
  renderFormatSeg();
  go('cola');
  (async () => {
    await loadStamp();
    await loadPrinters();
    await refreshStatus();
    await loadDoneRecent();
    await loadAuto();
    if (state.status.connected) await loadOrders({ manual: true });
    else renderCola();
    // reanudar el seguimiento si quedó un lote imprimiéndose
    try { const s = await api('/api/batch/status'); if (s && s.active && !s.finished) { state.batch = s; pollBatch(); } } catch {}
  })();

  // --- Actualización en tiempo real ---
  // Bucle async/await: cada ciclo ESPERA a que termine el anterior (sin
  // solapes por construcción, sin banderas). Lo global se refresca cada
  // ciclo; los datos de la pantalla activa, según su cadencia (`due`).
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const lastAt = {};                    // último refresco por recurso
  const due = (key, ms) => {
    if (Date.now() - (lastAt[key] || 0) < ms) return false;
    lastAt[key] = Date.now();
    return true;
  };
  async function liveRefresh() {
    await refreshStatus();                        // conexión / token (barato)
    await loadPrinters();                         // conexión/desconexión de impresoras
    await loadAuto();                             // estado del automático + punto del nav
    switch (state.tab) {
      case 'cola':
        renderAutoKpi();                          // KPI «Automático» de la cola
        if (state.status.connected && due('orders', 15000)) {
          // Impresos primero: es SQLite local (ms). Antes esperaba detrás de
          // la consulta a ML y la columna tardaba lo que tardara la Cola.
          await loadDoneRecent();
          renderCola();
          await loadOrders();                     // refresco silencioso de la cola
          renderCola();
        }
        break;
      case 'separacion':
        if (state.status.connected && due('orders', 15000)) {
          await loadOrders(); renderSeparacion();
        }
        break;
      case 'automatico':
        renderAutoStatus();                       // SOLO estado (no pisa el editor)
        break;
      case 'historial':
        if (due('hist', 15000)) await renderHistorial({ silent: true });
        break;
      case 'logs':
        if (due('logs', 12000)) await renderLogs();
        break;
      case 'conexion':
        if (due('accounts', 15000)) { await loadAccounts(); renderConexion(); }
        break;
      // 'etiquetas' y 'dispositivos': su config solo cambia desde esta misma
      // pantalla; impresoras ya se refrescan arriba en cada ciclo.
    }
  }
  (async () => {
    while (true) {
      if (!document.hidden) {                     // pausa si la pestaña no se ve
        try { await liveRefresh(); } catch (_) { /* el siguiente ciclo reintenta */ }
      }
      await sleep(4000);
    }
  })();
}

document.addEventListener('DOMContentLoaded', init);
