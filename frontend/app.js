/* EtiquetaFlow — lógica de frontend (JS vanilla, sin dependencias).
   Conecta el diseño con el backend real de Mercado Asistente. */

// ===== Estado =====
const state = {
  tab: 'cola',
  orders: [],            // ventas ready_to_ship (API), cada una con .pending
  selected: new Set(),   // shipment_id elegidos manualmente para "A imprimir"
  printedToday: 0,       // contador de impresiones exitosas de hoy (backend)
  doneRecent: [],        // mini-historial reciente (columna Impresos)
  status: { configured: false, connected: false, site: '—', nickname: null,
            token_expires_in: null },
  format: localStorage.getItem('ef_format') || 'pdf',
  printer: localStorage.getItem('ef_printer') || '',
  printers: [],
  cups: false,
  batch: null,           // estado del lote en curso (progreso)
  riskTotal: 0,          // etiquetas en riesgo (para el aviso)
  lastSync: null,
  logFilter: 'todos',
  histFilter: { from: '', to: '', format: '', result: '' },
  stamp: '',
};

const TITLES = {
  cola: ['Cola en tiempo real', 'Elige impresora y formato arriba; imprime seleccionadas o todo lo pendiente'],
  automatico: ['Impresión automática', 'El servidor imprime solo, por horario y solo hojas completas'],
  dispositivos: ['Dispositivos', 'Destino de impresión disponible'],
  conexion: ['Conexión Mercado Libre', 'Gestión de la integración con la API'],
  historial: ['Historial de impresión', 'Registro persistente de todas las etiquetas impresas'],
  logs: ['Logs del sistema', 'Registro de actividad de esta sesión'],
};

// ===== Utilidades =====
async function api(path, options = {}) {
  const resp = await fetch(path, options);
  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    const err = new Error((data && data.detail) || resp.statusText);
    err.status = resp.status;
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
// Pool de pendientes por imprimir (lo marca Mercado Libre vía backend).
function pendingPool() { return state.orders.filter(o => o.pending); }
// Próximos = pendientes aún no seleccionados; A imprimir = pendientes seleccionados.
function nextList() { return pendingPool().filter(o => !state.selected.has(String(o.shipment_id))); }
function selectedList() { return pendingPool().filter(o => state.selected.has(String(o.shipment_id))); }
function productSummary(o) {
  const p = (o.products && o.products[0]) || null;
  if (!p) return '—';
  const extra = o.products.length > 1 ? ` +${o.products.length - 1}` : '';
  return `${p.title} x${p.quantity}${extra}`;
}
// Metadatos por envío para el historial (JSON keyed por shipment_id).
function buildMeta(orders) {
  const m = {};
  for (const o of orders) {
    m[String(o.shipment_id)] = {
      order_id: o.order_id, buyer_name: o.buyer_name,
      product_summary: productSummary(o),
    };
  }
  return JSON.stringify(m);
}

// ===== Sello del sistema =====
async function loadStamp() {
  try { const d = await api('/api/stamp'); state.stamp = d.stamp || ''; } catch {}
}
function showStamp({ title, msg, critical }) {
  const pre = $('stamp-art'); if (pre) { pre.textContent = state.stamp || '(sello no disponible)'; pre.style.color = critical ? '#c43232' : 'var(--accent)'; }
  const head = $('stamp-head'), icon = $('stamp-icon');
  head.style.background = critical ? '#fbe9e9' : 'var(--accent-tint)';
  icon.style.background = critical ? '#d33a3a' : 'var(--accent)';
  icon.textContent = critical ? '✕' : '✦';
  $('stamp-title').textContent = title || 'Sello del sistema';
  $('stamp-msg').textContent = msg || '';
  $('stamp-msg').style.display = msg ? 'block' : 'none';
  $('stamp-overlay').style.display = 'flex';
}
function hideStamp() { $('stamp-overlay').style.display = 'none'; }
function showCritical(msg) {
  showStamp({ title: 'Fallo crítico de impresión', msg, critical: true });
  log('ERROR', 'impresion', 'FALLO CRÍTICO: ' + msg);
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

// ===== Logs (cliente, persistidos por sesión-día) =====
function loadLogs() { try { return JSON.parse(localStorage.getItem('ef_logs') || '[]'); } catch { return []; } }
function log(level, module, message) {
  const logs = loadLogs();
  logs.unshift({ ts: Date.now(), level, module, message });
  localStorage.setItem('ef_logs', JSON.stringify(logs.slice(0, 100)));
  if (state.tab === 'logs') renderLogs();
}
const LEVEL_STYLE = {
  OK:   ['#eaf6ef','#15824a'], INFO: ['rgba(47,107,240,.12)','#2f6bf0'],
  WARN: ['#fbf2dd','#b07400'], ERROR:['#fbe9e9','#c43232'],
};
function renderLogs() {
  const logs = loadLogs();
  const fmap = { todos:()=>true, OK:l=>l.level==='OK', INFO:l=>l.level==='INFO', WARN:l=>l.level==='WARN', ERROR:l=>l.level==='ERROR' };
  const rows = logs.filter(fmap[state.logFilter] || fmap.todos);

  // filtros
  const defs = [['todos','Todos'],['OK','Éxito'],['INFO','Info'],['WARN','Advert.'],['ERROR','Errores']];
  $('log-filters').innerHTML = defs.map(([k,label]) => {
    const count = k==='todos' ? logs.length : logs.filter(l=>l.level===k).length;
    const active = state.logFilter === k;
    const [bg,fg] = LEVEL_STYLE[k] || ['#fff','#5b6472'];
    const style = active
      ? (k==='todos' ? 'background:var(--accent);color:#fff;border-color:transparent'
                     : `background:${bg};color:${fg};border-color:transparent`)
      : '';
    return `<button class="lfilter" data-filter="${k}" style="${style}">${label} <span class="c">${count}</span></button>`;
  }).join('');

  // filas
  if (!rows.length) {
    $('log-rows').innerHTML = `<div class="empty"><span>Sin eventos registrados.</span></div>`;
    return;
  }
  $('log-rows').innerHTML = rows.map(l => {
    const [bg,fg] = LEVEL_STYLE[l.level] || LEVEL_STYLE.INFO;
    const time = new Date(l.ts).toLocaleTimeString('es-MX', { hour12:false });
    return `<div class="log-row">
      <span style="width:96px;font-family:var(--mono);color:var(--muted2);font-size:12px">${time}</span>
      <span style="width:74px"><span class="lvl" style="background:${bg};color:${fg}">${l.level}</span></span>
      <span style="width:130px;font-family:var(--mono);font-size:12px;color:var(--ink3)">${esc(l.module)}</span>
      <span style="flex:1;color:#2b303a">${esc(l.message)}</span></div>`;
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
  for (const o of printedOrders) state.selected.delete(String(o.shipment_id));
  await Promise.all([loadOrders(), loadDoneRecent()]);
  renderCola();
}

// Imprime un envío individual (reimpresión). Verifica antes de pedir a ML.
async function printOne(order, fmt) {
  if (!order) return;
  fmt = fmt || state.format;
  if (canServerPrint()) {
    try {
      const fd = new FormData();
      fd.append('format', fmt); fd.append('printer', state.printer);
      fd.append('meta', buildMeta([order]));
      const r = await api(`/api/print/${encodeURIComponent(order.shipment_id)}`, { method:'POST', body:fd });
      log('OK', 'impresion', `Etiqueta ${fmt.toUpperCase()} de la venta #${order.order_id} enviada a «${r.printer}».`);
      showBanner(`Enviado a impresora: ${r.printer}`, 'success');
      await afterPrint([order]);
    } catch (e) {
      if (e.status === 409) {
        // Bloqueo preventivo: la impresora no está lista. Nada se perdió.
        showBanner(e.message, 'error');
        log('WARN', 'impresion', `Bloqueado (#${order.order_id}): ${e.message}`);
        await loadPrinters({ force: true });
      } else {
        showCritical(`Venta #${order.order_id}: ${e.message}`);
      }
      await loadDoneRecent();
    }
  } else {
    openLabelUrl(`/api/label/${encodeURIComponent(order.shipment_id)}?format=${fmt}`, fmt === 'zpl');
    log('WARN', 'impresion', `Sin impresora: etiqueta ${fmt.toUpperCase()} de #${order.order_id} abierta en el navegador.`);
  }
}

// Arranca un lote en segundo plano (motor seguro hoja por hoja) y sigue su progreso.
async function startBatch(orders, labelWhat) {
  if (!orders.length) { showBanner('No hay ventas para imprimir.', 'info'); return; }
  const fmt = state.format;
  if (!canServerPrint()) {
    const ids = orders.map(o => o.shipment_id).join(',');
    openLabelUrl(`/api/labels?ids=${encodeURIComponent(ids)}&format=${fmt}`, fmt === 'zpl');
    log('WARN', 'impresion', `Sin impresora: ${labelWhat} (${orders.length}) abierto en el navegador.`);
    return;
  }
  try {
    const fd = new FormData();
    fd.append('ids', orders.map(o => o.shipment_id).join(','));
    fd.append('format', fmt); fd.append('printer', state.printer);
    fd.append('meta', buildMeta(orders));
    const r = await api('/api/print-batch', { method:'POST', body:fd });
    log('INFO', 'impresion', `${labelWhat}: lote de ${orders.length} etiqueta(s) ${fmt.toUpperCase()} iniciado en «${r.printer}».`);
    showBanner(`Imprimiendo ${orders.length} etiqueta(s) en «${r.printer}»…`, 'info');
    pollBatch();
  } catch (e) {
    if (e.status === 409) showBanner(e.message, 'error');
    else showCritical(`${labelWhat}: ${e.message}`);
  }
}

function printSelected() {
  const sel = selectedList();
  if (!sel.length) { showBanner('No hay ventas seleccionadas.', 'info'); return; }
  startBatch(sel, 'Selección');
}
function printAllPending() {
  const q = pendingPool();
  if (!q.length) { showBanner('No hay pendientes por imprimir.', 'info'); return; }
  startBatch(q, 'Todo lo pendiente');
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
        log(risk ? 'WARN' : 'OK', 'impresion', s.message || 'Lote terminado.');
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
  // resumen por estado
  const order = ['done','printing','pending','risk','blocked','canceled'];
  $('batch-chips').innerHTML = order.filter(k => c[k]).map(k => {
    const [bg,fg,label] = BATCH_ITEM_STYLE[k];
    return `<span class="chip" style="background:${bg};color:${fg}">${label}: ${c[k]}</span>`;
  }).join('');
}

// ===== Render: Cola (kanban de 3 columnas) =====
function colaItemHtml(o, kind) {
  const prod = (o.products && o.products[0]) || { title:'—' };
  const sid = String(o.shipment_id);
  if (kind === 'next') {
    return `<div class="q-item" style="cursor:pointer" title="Marcar para imprimir" data-sid="${esc(sid)}" data-act="add">
      <span class="chk"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round" style="visibility:hidden"><polyline points="20 6 9 17 4 12"></polyline></svg></span>
      <div style="flex:1;min-width:0"><div class="q-title">${esc(prod.title)}</div><div class="q-sub">${esc(o.buyer_name)} · #${esc(o.order_id)}</div></div>
    </div>`;
  }
  // seleccionadas
  return `<div class="q-item" data-sid="${esc(sid)}">
    <span class="chk on"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg></span>
    <div style="flex:1;min-width:0"><div class="q-title">${esc(prod.title)}</div><div class="q-sub">${esc(o.buyer_name)} · #${esc(o.order_id)}</div></div>
    <button class="xbtn" title="Quitar de la selección" data-sid="${esc(sid)}" data-act="remove">✕</button>
  </div>`;
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
  return `<div class="q-item" title="${esc(h.error || '')}">
    <span class="chip" style="background:${bg};color:${fg};flex:0 0 auto">${txt}</span>
    <div style="flex:1;min-width:0"><div class="q-title">${esc(h.product_summary || ('#'+h.shipment_id))}</div><div class="q-sub">${esc(h.buyer_name || ('venta '+(h.order_id||'')))} · ${time}</div></div>
    ${canReprint ? `<button class="xbtn" title="Reimprimir" data-sid="${esc(h.shipment_id)}" data-fmt="${esc(h.format||'pdf')}" data-oid="${esc(h.order_id||'')}" data-buyer="${esc(h.buyer_name||'')}" data-prod="${esc(h.product_summary||'')}" data-act="reprint-done" style="color:var(--accent)">⟳</button>`
      : `<span class="q-sub" style="flex:0 0 auto">${esc((h.format||'').toUpperCase())}</span>`}
  </div>`;
}
function renderCola() {
  const pend = pendingPool();
  const next = nextList();
  const sel = selectedList();
  // limpia selección de ids que ya no están pendientes/presentes
  const present = new Set(pend.map(o => String(o.shipment_id)));
  for (const id of [...state.selected]) if (!present.has(id)) state.selected.delete(id);

  $('nav-queue-count').textContent = pend.length;
  $('kpi-pendientes').textContent = pend.length;
  $('kpi-encola').textContent = sel.length;
  $('kpi-formato').textContent = state.format.toUpperCase();
  $('kpi-hoy').textContent = state.printedToday;
  $('print-pending-count').textContent = pend.length;
  $('print-sel-count').textContent = sel.length;
  $('col-next-count').textContent = next.length;
  $('col-sel-count').textContent = sel.length;
  $('col-done-count').textContent = state.printedToday;
  $('btn-print-pending').disabled = !pend.length;
  $('btn-print-selected').disabled = !sel.length;
  $('btn-select-all').disabled = !next.length;

  // Próximos
  const cNext = $('col-next');
  if (!state.status.connected) cNext.innerHTML = idle('Conecta tu cuenta para ver las ventas.');
  else if (!next.length) cNext.innerHTML = idle(pend.length ? 'Todo lo pendiente está seleccionado.' : 'No hay ventas por imprimir.');
  else cNext.innerHTML = next.map(o => colaItemHtml(o, 'next')).join('');

  // A imprimir
  const cSel = $('col-sel');
  cSel.innerHTML = sel.length ? sel.map(o => colaItemHtml(o, 'sel')).join('')
    : idle('Marca envíos en «Próximos» para armar un lote.');

  // Impresos (mini-historial reciente)
  const cDone = $('col-done');
  cDone.innerHTML = state.doneRecent.length ? state.doneRecent.map(doneItemHtml).join('')
    : idle('Aún no imprimes nada hoy.');

  renderFormatSeg();
  updatePrinterStatus();
  renderBatch();
  renderRiskAlert();
  updateLayoutHint();
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
  const count = selectedList().length || pendingPool().length;
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
async function renderHistorial() {
  const rows = $('hist-rows');
  rows.innerHTML = `<div class="empty" style="padding:36px 0"><span>Cargando…</span></div>`;
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
      <span style="width:92px"><button class="btn btn-ghost" style="padding:4px 9px;font-size:11px" data-sid="${esc(h.shipment_id)}" data-fmt="${esc(h.format||'pdf')}" data-oid="${esc(h.order_id||'')}" data-buyer="${esc(h.buyer_name||'')}" data-prod="${esc(h.product_summary||'')}" data-act="reprint">Reimprimir</button></span>
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
  for (const e of events) { log(e.lvl, 'dispositivos', e.msg); if (e.lvl === 'WARN') showBanner(e.msg, 'error'); }
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
  wrap.innerHTML = state.printers.map(p => {
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
  }).join('');
}
function renderPrinterSelect() {
  const sel = $('printer-select');
  if (!sel) return;
  // Solo se pueden elegir impresoras que de verdad pueden imprimir. Las no
  // listas aparecen deshabilitadas con su motivo (no se pueden seleccionar).
  const opts = ['<option value="">Predeterminada del sistema</option>'].concat(
    state.printers.map(p => p.ready
      ? `<option value="${esc(p.name)}">${esc(p.name)} · ${p.connection}</option>`
      : `<option value="${esc(p.name)}" disabled>${esc(p.name)} — ${esc(p.ready_reason || 'no disponible')}</option>`)
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
    showBanner('Impresora agregada.', 'success'); log('OK','dispositivos',`Impresora «${name}» dada de alta (${dev.connection}).`);
    await loadPrinters();
  } catch (e) { showBanner('No se pudo agregar: ' + e.message, 'error'); }
}

// ===== Render: Conexión =====
function fmtToken(sec) {
  if (sec == null) return '—';
  if (sec <= 0) return 'Expirado';
  if (sec >= 3600) { const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60); return `Expira en ${h}h ${m}m`; }
  const m = Math.floor(sec/60), s = sec%60; return `Expira en ${m}:${String(s).padStart(2,'0')} min`;
}
function renderConexion() {
  const st = state.status;
  $('conn-site').textContent = st.site || '—';
  $('conn-account').textContent = st.connected ? `${st.nickname || 'vendedor'} · ID ${st.seller_id || '—'}` : 'Sin conectar';

  // pill cuenta
  const [pbg,pfg,pdot,plabel] = st.connected
    ? ['#eaf6ef','#15824a','#1ba85b','API conectada']
    : st.configured ? ['#fbf2dd','#b07400','#e8a200','Configurado · sin conectar']
                    : ['#eef0f3','#6a7280','#aab0bb','Sin configurar'];
  const p2 = $('conn-pill-2'); p2.style.background=pbg; p2.style.color=pfg;
  p2.querySelector('.pdot').style.background=pdot; $('conn-pill-2-label').textContent=plabel;

  // token
  const tk = $('stat-token');
  tk.textContent = st.connected ? fmtToken(st.token_expires_in) : '—';
  tk.style.color = (st.token_expires_in==null) ? 'var(--muted)' : st.token_expires_in<300 ? '#d33a3a' : st.token_expires_in<900 ? '#c77700' : '#15a05a';
  $('stat-sync').textContent = state.lastSync ? 'hace ' + Math.max(0, Math.round((Date.now()-state.lastSync)/1000)) + ' s' : '—';
  $('stat-queue').textContent = state.orders.length;

  // botones
  $('btn-connect').disabled = !st.configured || st.connected;
  $('btn-disconnect').disabled = !st.connected;
  $('btn-renew').disabled = !st.connected;
  $('btn-sync').disabled = !st.connected;
}

// ===== Modo automático =====
const AUTO_STATE = {
  off:               ['#aab0bb', 'Desactivado'],
  printing:          ['#2f6bf0', 'Imprimiendo'],
  waiting_window:    ['#b07400', 'Fuera de horario'],
  waiting_interval:  ['#15a05a', 'En horario'],
  waiting_fill:      ['#b07400', 'Esperando etiquetas'],
  no_conn:           ['#c43232', 'Sin conexión'],
  no_printer:        ['#c43232', 'Sin impresora'],
  printer_not_ready: ['#c43232', 'Impresora no lista'],
  no_size:           ['#b07400', 'Falta calibrar'],
  error:             ['#c43232', 'Error'],
};
async function loadAuto() {
  try { state.auto = await api('/api/auto'); } catch { state.auto = null; }
  const dot = $('auto-nav-dot');
  if (dot) dot.style.display = (state.auto && state.auto.config.enabled) ? 'block' : 'none';
}
function fillAutoForm() {
  if (!state.auto) return;
  const active = document.activeElement;
  const inForm = active && ['auto-enabled','auto-start','auto-end','auto-interval'].includes(active.id);
  if (inForm) return;                     // no pisar mientras el usuario edita
  const c = state.auto.config;
  $('auto-enabled').checked = c.enabled;
  $('auto-start').value = c.start;
  $('auto-end').value = c.end;
  $('auto-interval').value = c.interval_min;
}
function renderAuto() {
  fillAutoForm();
  const s = state.auto || { state:'off', message:'—', config:{ enabled:false } };
  const [color, label] = AUTO_STATE[s.state] || AUTO_STATE.off;
  const on = s.config.enabled;
  $('auto-dot').style.background = color;
  $('auto-dot').style.animation = s.state === 'printing' ? 'efpulse 1.4s infinite' : 'none';
  $('auto-state-msg').textContent = s.message || '—';
  const pill = $('auto-pill');
  pill.style.background = on ? 'rgba(47,107,240,.1)' : '#eef0f3';
  pill.style.color = on ? 'var(--accent)' : '#6a7280';
  pill.querySelector('.pdot').style.background = on ? color : '#aab0bb';
  $('auto-pill-label').textContent = on ? label : 'Desactivado';
  renderAutoReqs();
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

// ===== Topbar / estado =====
function updateConnPills() {
  const st = state.status;
  const [bg,fg,dot,label] = st.connected
    ? ['#eaf6ef','#15824a','#1ba85b','API conectada']
    : st.configured ? ['#fbf2dd','#b07400','#e8a200','Sin conectar']
                    : ['#eef0f3','#6a7280','#aab0bb','Sin configurar'];
  const pill = $('conn-pill'); pill.style.background=bg; pill.style.color=fg;
  pill.querySelector('.pdot').style.background=dot; $('conn-pill-label').textContent=label;
}
async function refreshStatus() {
  try {
    state.status = await api('/api/status');
    if (state.status.token_expires_in != null) state._tokenLocal = state.status.token_expires_in;
  } catch (e) { /* silencioso */ }
  updateConnPills();
  if (state.tab === 'conexion') renderConexion();
}

// ===== Datos =====
async function loadOrders(opts = {}) {
  if (!state.status.connected) { renderCola(); return; }
  try {
    const data = await api('/api/orders');
    const newOrders = data.orders || [];
    if (typeof data.printed_today === 'number') state.printedToday = data.printed_today;
    const newIds = new Set(newOrders.map(o => String(o.shipment_id)));
    const added = state._orderIds ? newOrders.filter(o => !state._orderIds.has(String(o.shipment_id))).length : 0;
    const removed = state._orderIds ? [...state._orderIds].filter(id => !newIds.has(id)).length : 0;
    state.orders = newOrders; state._orderIds = newIds; state.lastSync = Date.now();
    if (opts.manual) log('OK', 'cola', `Cola sincronizada · ${newOrders.length} venta(s) lista(s).`);
    else if (state._ordersInit) {
      if (added) log('INFO', 'cola', `${added} venta(s) nueva(s) lista(s) para enviar.`);
      if (removed) log('INFO', 'cola', `${removed} venta(s) salieron de la cola (enviadas/canceladas).`);
    }
    state._ordersInit = true;
  } catch (e) {
    if (opts.manual) showBanner('No se pudieron cargar las ventas: ' + e.message, 'error');
    log('ERROR', 'cola', 'Error al sincronizar la cola: ' + e.message);
  }
  renderCola();
  if (state.tab === 'conexion') renderConexion();
}

// ===== Navegación =====
function go(tab) {
  state.tab = tab;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab===tab));
  document.querySelectorAll('.tab').forEach(s => s.classList.toggle('active', s.dataset.tab===tab));
  $('page-title').textContent = TITLES[tab][0];
  $('page-sub').textContent = TITLES[tab][1];
  if (tab === 'cola') renderCola();
  else if (tab === 'automatico') { loadAuto().then(renderAuto); }
  else if (tab === 'dispositivos') loadPrinters({ force: true });
  else if (tab === 'conexion') renderConexion();
  else if (tab === 'historial') renderHistorial();
  else if (tab === 'logs') renderLogs();
}

// ===== Conexión: acciones =====
async function loadConfig() {
  try {
    const c = await api('/api/config');
    $('app_id').value = c.app_id || '';
    if (c.redirect_uri) $('redirect_uri').value = c.redirect_uri;
    if (c.has_secret) $('client_secret').placeholder = '•••• (guardado — vacío para conservar)';
  } catch {}
}

// ===== Init =====
function init() {
  // reloj
  const tick = () => {
    $('clock').textContent = new Date().toLocaleTimeString('es-MX', { hour12:false });
    if (state.status.connected && state._tokenLocal != null) {
      state._tokenLocal = Math.max(0, state._tokenLocal - 1);
      state.status.token_expires_in = state._tokenLocal;
      if (state.tab === 'conexion') {
        const tk = $('stat-token'); tk.textContent = fmtToken(state._tokenLocal);
        tk.style.color = state._tokenLocal<300?'#d33a3a':state._tokenLocal<900?'#c77700':'#15a05a';
      }
    }
  };
  tick(); setInterval(tick, 1000);

  // navegación
  document.querySelectorAll('.nav-btn').forEach(b => b.addEventListener('click', () => go(b.dataset.tab)));

  // cola (kanban)
  $('btn-reload').addEventListener('click', () => loadOrders({ manual: true }));
  $('btn-print-pending').addEventListener('click', printAllPending);
  $('btn-print-selected').addEventListener('click', printSelected);
  $('btn-select-all').addEventListener('click', () => {
    nextList().forEach(o => state.selected.add(String(o.shipment_id)));
    renderCola();
  });
  $('col-next').addEventListener('click', e => {
    const row = e.target.closest('[data-act="add"]'); if (!row) return;
    state.selected.add(row.dataset.sid); renderCola();
  });
  $('col-sel').addEventListener('click', e => {
    const b = e.target.closest('[data-act="remove"]'); if (!b) return;
    state.selected.delete(b.dataset.sid); renderCola();
  });
  $('link-history').addEventListener('click', () => go('historial'));
  // reimprimir desde la columna Impresos (⟳)
  $('col-done').addEventListener('click', async e => {
    const b = e.target.closest('[data-act="reprint-done"]'); if (!b) return;
    await printOne({ shipment_id: b.dataset.sid, order_id: b.dataset.oid, buyer_name: b.dataset.buyer,
                     products: b.dataset.prod ? [{ title: b.dataset.prod, quantity: 1 }] : [] }, b.dataset.fmt);
  });

  // formato (toggle) y control del lote
  $('format-seg').addEventListener('click', e => { const b = e.target.closest('[data-format]'); if (b) setFormat(b.dataset.format); });
  $('btn-batch-stop').addEventListener('click', async () => { await api('/api/batch/stop', { method:'POST' }).catch(()=>{}); });
  $('btn-batch-close').addEventListener('click', () => { state.batch = null; renderBatch(); });
  $('btn-layout-preview').addEventListener('click', () => {
    const count = selectedList().length || pendingPool().length || 4;
    window.open('/api/layout-preview?count=' + count, '_blank', 'noopener');
  });

  // automático
  $('auto-form').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const r = await api('/api/auto', { method:'POST', body:new FormData(e.target) });
      await loadAuto(); renderAuto();
      showBanner(r.config.enabled ? 'Impresión automática activada.' : 'Impresión automática desactivada.', 'success');
      log('OK','automatico', r.config.enabled
        ? `Automático activado · ${r.config.start}–${r.config.end}, cada ${r.config.interval_min} min.`
        : 'Automático desactivado.');
    } catch (err) { showBanner('No se pudo guardar: ' + err.message, 'error'); }
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
      e.target.reset(); showBanner('Impresora de red agregada.', 'success'); log('OK','dispositivos','Impresora de red dada de alta.');
      await loadPrinters();
    } catch (err) { showBanner('No se pudo agregar: ' + err.message, 'error'); }
  });
  $('printer-cards').addEventListener('click', async e => {
    const b = e.target.closest('[data-act]'); if (!b) return;
    const name = b.dataset.name, act = b.dataset.act;
    try {
      if (act === 'test') {
        const r = await api(`/api/printers/${encodeURIComponent(name)}/test`, { method:'POST' });
        showDiag(r.diagnostics || {});
        const cats = (r.diagnostics && r.diagnostics.verdict || []).map(v => v.cat).join(', ');
        log('OK','dispositivos',`Prueba enviada a «${name}» (job ${r.job}). Diagnóstico: ${cats || '—'}.`);
      }
      else if (act === 'default') { await api(`/api/printers/${encodeURIComponent(name)}/default`, { method:'POST' }); log('OK','dispositivos',`«${name}» fijada como predeterminada.`); await loadPrinters({ force:true }); }
      else if (act === 'undefault') { await api(`/api/printers/${encodeURIComponent(name)}/undefault`, { method:'POST' }); log('INFO','dispositivos',`Predeterminada quitada (ninguna fijada).`); await loadPrinters({ force:true }); }
      else if (act === 'delete') { if (!confirm(`¿Eliminar la impresora «${name}»?`)) return; await api(`/api/printers/${encodeURIComponent(name)}`, { method:'DELETE' }); log('WARN','dispositivos',`Impresora «${name}» eliminada.`); await loadPrinters({ force:true }); }
    } catch (err) {
      if (act === 'test' && err.status === 409) showBanner(err.message, 'error');
      else if (act === 'test') showCritical(`Prueba en «${name}»: ${err.message}`);
      else showBanner('Error: ' + err.message, 'error');
      log('ERROR','dispositivos',`Acción «${act}» en «${name}»: ${err.message}`);
    }
  });

  // logs: filtros (delegación)
  $('log-filters').addEventListener('click', e => { const b = e.target.closest('[data-filter]'); if (b) { state.logFilter = b.dataset.filter; renderLogs(); } });

  // conexión
  $('btn-sync').addEventListener('click', async () => { await loadOrders({ manual: true }); renderConexion(); showBanner('Cola sincronizada.', 'success'); });
  $('btn-connect').addEventListener('click', async () => {
    try { const { authorization_url } = await api('/api/connect'); window.location.href = authorization_url; }
    catch (e) { showBanner('No se pudo iniciar la conexión: ' + e.message, 'error'); }
  });
  $('btn-renew').addEventListener('click', async () => {
    try { const r = await api('/api/refresh', { method:'POST' }); state.status.token_expires_in = r.token_expires_in; state._tokenLocal = r.token_expires_in; renderConexion(); showBanner('Token renovado.', 'success'); log('OK','conexion','Token de acceso renovado manualmente.'); }
    catch (e) { showBanner('No se pudo renovar el token: ' + e.message, 'error'); log('ERROR','conexion','Error al renovar el token: ' + e.message); }
  });
  $('btn-disconnect').addEventListener('click', async () => {
    try { await api('/api/disconnect', { method:'POST' }); log('WARN','conexion','Cuenta desconectada por el operador.'); state.orders=[]; state.selected.clear(); await refreshStatus(); renderCola(); renderConexion(); showBanner('Cuenta desconectada.', 'info'); }
    catch (e) { showBanner('Error al desconectar: ' + e.message, 'error'); }
  });
  $('config-form').addEventListener('submit', async e => {
    e.preventDefault();
    try { await api('/api/config', { method:'POST', body:new FormData(e.target) }); $('client_secret').value=''; await loadConfig(); await refreshStatus(); renderConexion(); showBanner('Credenciales guardadas (secreto cifrado).', 'success'); log('OK','conexion','Credenciales de la aplicación guardadas.'); }
    catch (err) { showBanner('No se pudieron guardar: ' + err.message, 'error'); }
  });
  $('manual-form').addEventListener('submit', async e => {
    e.preventDefault();
    try { await api('/api/connect/manual', { method:'POST', body:new FormData(e.target) }); $('manual_code').value=''; await refreshStatus(); renderConexion(); await loadOrders({ manual: true }); showBanner('Código canjeado. Cuenta conectada.', 'success'); log('OK','conexion','Cuenta conectada (canje manual de código).'); }
    catch (err) { showBanner('No se pudo canjear el código: ' + err.message, 'error'); }
  });

  // sello del sistema
  $('stamp-close').addEventListener('click', hideStamp);
  $('stamp-overlay').addEventListener('click', e => { if (e.target === $('stamp-overlay')) hideStamp(); });
  $('diag-close').addEventListener('click', hideDiag);
  $('diag-overlay').addEventListener('click', e => { if (e.target === $('diag-overlay')) hideDiag(); });
  $('side-foot').addEventListener('click', () => showStamp({ title: 'Sello del sistema', msg: 'EtiquetaFlow · ' + location.host, critical: false }));

  // arranque
  renderFormatSeg();
  go('cola');
  (async () => {
    await loadStamp();
    await loadConfig();
    await loadPrinters();
    await refreshStatus();
    await loadDoneRecent();
    await loadAuto();
    if (state.status.connected) await loadOrders({ manual: true });
    else renderCola();
    // reanudar el seguimiento si quedó un lote imprimiéndose
    try { const s = await api('/api/batch/status'); if (s && s.active && !s.finished) { state.batch = s; pollBatch(); } } catch {}
  })();

  // --- Actualización en tiempo real (polling adaptativo) ---
  let ticking = false, lastOrders = Date.now();
  async function liveTick() {
    if (ticking || document.hidden) return;       // sin solapes; pausa si la pestaña no se ve
    ticking = true;
    try {
      await refreshStatus();                        // conexión / token (barato)
      await loadPrinters();                         // detecta conexión/desconexión de impresoras
      await loadAuto();                             // estado del automático + punto del nav
      if (state.tab === 'automatico') renderAuto();
      if (state.tab === 'cola' && state.status.connected && Date.now() - lastOrders > 15000) {
        lastOrders = Date.now();
        await loadOrders();                         // refresco silencioso de la cola
        await loadDoneRecent();                     // y del mini-historial (Impresos)
        renderCola();
      }
    } catch (_) { /* el siguiente tick reintenta */ }
    finally { ticking = false; }
  }
  setInterval(liveTick, 4000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) liveTick(); });
}

document.addEventListener('DOMContentLoaded', init);
