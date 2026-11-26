/* Multi-Warehouse Inventory — single page app (no build step).
   All text goes through t(); all DOM is built with h() (textContent only, so no HTML injection). */
(() => {
  'use strict';

  const S = {
    token: localStorage.getItem('wms_token'),
    lang: localStorage.getItem('wms_lang') || 'th',
    me: null, units: [], whs: [], categories: [],
  };
  const DOC_TYPES = ['RECEIVING', 'SALE', 'WITHDRAWAL', 'CUSTOMER_RETURN', 'SUPPLIER_RETURN', 'ADJUSTMENT'];
  const ADJ_REASONS = ['DAMAGED', 'EXPIRED', 'LOST', 'FOUND', 'RECOUNT', 'SAMPLE', 'OTHER'];
  const ADJ_DEFAULT_DIR = { FOUND: 'IN' };   // everything else usually lowers the stock
  const PARTY = { SALE: 'customer', CUSTOMER_RETURN: 'customer', RECEIVING: 'supplier', SUPPLIER_RETURN: 'supplier' };

  // ---------------------------------------------------------------- helpers
  function t(key, vars) {
    let s = (I18N[S.lang] && I18N[S.lang][key]);
    if (s === undefined) s = I18N.en[key];
    if (s === undefined) s = key;
    if (vars) for (const k of Object.keys(vars)) s = s.split('{' + k + '}').join(vars[k]);
    return s;
  }
  const hasKey = (k) => (I18N[S.lang] && I18N[S.lang][k] !== undefined) || I18N.en[k] !== undefined;

  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    let value;
    for (const [k, v] of Object.entries(props || {})) {
      if (k === 'class') el.className = v;
      else if (k === 'value') value = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, '');
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    if (value !== undefined && value !== null) el.value = value;
    return el;
  }

  const fmtNum = (n) => Number(n).toLocaleString('en-US', { maximumFractionDigits: 3 });
  const fmtDate = (d) => (d ? String(d).slice(0, 10) : '');
  const fmtDateTime = (d) => (d ? String(d).replace('T', ' ').slice(0, 16) : '');
  const today = () => new Date().toISOString().slice(0, 10);
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));

  /** 53 PC with CT=24, PK=6 -> "2 CT + 1 PK + 5 PC" */
  function breakdown(base, conversions) {
    base = Number(base);
    const convs = (conversions || []).slice().sort((a, b) => b.factor_to_base - a.factor_to_base);
    if (!convs.length || base === 0) return '';
    let rem = Math.abs(base);
    const parts = [];
    for (const c of convs) {
      const n = Math.floor(rem / c.factor_to_base);
      if (n > 0) { parts.push(n + ' ' + c.unit_code); rem -= n * c.factor_to_base; }
    }
    if (rem > 0 || !parts.length) parts.push(rem + ' PC');
    return parts.join(' + ');
  }

  async function api(path, opt = {}) {
    const headers = Object.assign(opt.form ? {} : { 'Content-Type': 'application/json' }, opt.headers || {});
    if (S.token) headers.Authorization = 'Bearer ' + S.token;
    const res = await fetch('/api' + path, {
      method: opt.method || 'GET', headers,
      body: opt.form ? opt.form : (opt.body !== undefined ? JSON.stringify(opt.body) : undefined),
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty body */ }
    if (!res.ok) {
      if (res.status === 401 && S.token) { logout(); }
      let err = data && data.error;
      if (!err) err = { code: data && data.detail ? 'VALIDATION_ERROR' : 'HTTP_' + res.status, message: 'HTTP ' + res.status };
      throw Object.assign(new Error(err.message), { code: err.code, detail: err, status: res.status });
    }
    return data;
  }
  const qs = (o) => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(o)) if (v !== '' && v != null && v !== false) p.set(k, v);
    const s = p.toString();
    return s ? '?' + s : '';
  };

  function errText(e) {
    const d = e.detail || {};
    let key = 'err.' + (d.code_hint || e.code);
    let msg = hasKey(key) ? t(key) : (e.message || t('err.UNKNOWN'));
    if (e.code === 'INSUFFICIENT_STOCK' && Array.isArray(d.items)) {
      msg += ' — ' + d.items.map((i) => `${i.product_code} (${t('err.available')} ${fmtNum(i.available)}, ${t('err.required')} ${fmtNum(i.required)})`).join('; ');
    }
    if (d.line) msg = t('lbl.line') + ' ' + d.line + ': ' + msg;
    return msg;
  }

  let toastTimer;
  function toast(msg, kind = 'ok') {
    const el = document.getElementById('toast');
    el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
    el.setAttribute('aria-live', kind === 'err' ? 'assertive' : 'polite');
    el.textContent = msg;
    el.className = 'toast show ' + kind;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = 'toast'; }, kind === 'err' ? 8000 : 3000);
  }
  const fail = (e) => toast(errText(e), 'err');

  const main = () => document.getElementById('main');
  const present = (nodes) => nodes.flat(Infinity).filter((n) => n != null && n !== false);
  /** Turns the leading [back link] + h2 + [hint] + [actions] of a page into one tidy header:
      title and subtitle on the left, the page's buttons on the right. */
  function shapePage(nodes) {
    const list = present(nodes);
    let i = 0;
    const crumb = list[0] && list[0].tagName === 'P' && list[0].children.length === 1 && list[0].firstElementChild.tagName === 'A' ? list[i++] : null;
    if (!list[i] || list[i].tagName !== 'H2') return list;
    const old = list[i++];
    const title = h('h1', { class: 'page-title' }); title.append(...old.childNodes);
    const sub = list[i] && list[i].tagName === 'P' && list[i].classList.contains('hint') ? list[i++] : null;
    let acts = list[i] && list[i].tagName === 'DIV' && list[i].classList.contains('actions') ? list[i++] : null;
    // a "create" link sitting in the filter bar belongs in the header
    const fl = list[i];
    if (!acts && fl && fl.tagName === 'DIV' && fl.classList.contains('filters')) {
      const lift = [...fl.children].filter((c) => c.matches('a.btn.primary'));
      if (lift.length) acts = h('div', {}, lift);
    }
    if (crumb) crumb.className = 'crumb';
    if (sub) sub.className = 'page-sub';
    if (acts) acts.className = 'ph-actions';
    const rest = list.slice(i).map((n) => {
      // on a phone a long filter bar folds away behind one "Filters" button so the list stays in view
      if (n.tagName !== 'DIV' || !n.classList.contains('filters') || n.querySelectorAll('.field').length < 3) return n;
      const box = h('details', { class: 'filterbox', open: !window.matchMedia('(max-width: 640px)').matches },
        h('summary', {}, icon('filter'), t('lbl.filters')), n);
      return box;
    });
    return [h('header', { class: 'page-head' }, h('div', { class: 'ph-main' }, crumb, title, sub), acts), ...rest];
  }
  function view(...nodes) { const m = main(); m.replaceChildren(...shapePage(nodes)); window.scrollTo(0, 0); }
  /** For pages that fetch before rendering: ignore the render if the user has navigated away meanwhile
      (route() builds a fresh <main> every time, so a changed element means this page is stale). */
  function viewFor() {
    const el = main();
    const v = (...nodes) => { if (main() !== el) return; el.replaceChildren(...shapePage(nodes)); window.scrollTo(0, 0); };
    v.el = el;
    return v;
  }

  /** Pick many products at once: search, filter by category / in-stock, tick, add.
      opts: { warehouseId, inStock (default filter), have: Set(product_id already on the form), avail: {id: PC}, onAdd(products) } */
  function productPicker(opts) {
    const chosen = new Map();
    const f = { q: '', cat: '', inStock: !!opts.inStock };
    const dlg = h('dialog', { class: 'picker' + (S.categories.some((c) => c.is_active) ? ' has-chips' : ''), 'aria-labelledby': 'pk-title' });
    const close = () => { dlg.close(); dlg.remove(); };
    dlg.addEventListener('close', () => dlg.remove());
    const body = h('tbody');
    const box = h('div', { class: 'pk-list' });
    const count = h('b', { class: 'pk-count' });
    const addBtn = h('button', { type: 'button', class: 'primary', onclick: () => {
      if (!chosen.size) return;
      const picked = [...chosen.values()];   // [{ product, q: [unit1, unit2, PC] }]
      close();                     // first, so the dialog hands focus back before the form moves it to the new line
      opts.onAdd(picked);
    } });
    const pg = makePager(() => load(true));
    const allBox = h('input', { type: 'checkbox', 'aria-label': t('pk.selectPage'), onchange: (e) => {
      shown.filter((p) => !opts.have.has(p.product_id)).forEach((p) => { if (e.target.checked) pick(p); else chosen.delete(p.product_id); });
      draw();
    } });
    let shown = [];
    const pick = (p) => { if (!chosen.has(p.product_id)) chosen.set(p.product_id, { product: p, q: emptyQ() }); };
    const refreshCount = () => {
      count.textContent = t('pk.chosen', { n: fmtNum(chosen.size) });
      addBtn.replaceChildren(icon('plus'), t('pk.add', { n: fmtNum(chosen.size) }));
      addBtn.disabled = !chosen.size;
    };
    function syncAll() {
      const pickable = shown.filter((p) => !opts.have.has(p.product_id));
      allBox.checked = pickable.length > 0 && pickable.every((p) => chosen.has(p.product_id));
      allBox.indeterminate = !allBox.checked && pickable.some((p) => chosen.has(p.product_id));
      allBox.disabled = !pickable.length;
    }
    function draw() {
      syncAll();
      body.replaceChildren(...shown.map((p) => {
        const already = opts.have.has(p.product_id);
        const on = chosen.has(p.product_id);
        const cb = h('input', { type: 'checkbox', checked: on || already, disabled: already, 'aria-label': p.product_code + ' ' + p.name,
          onchange: (e) => { if (e.target.checked) pick(p); else chosen.delete(p.product_id); draw(); } });
        const have = opts.avail ? (opts.avail[p.product_id] || 0) : null;
        // quantity boxes, one per unit of this product (largest first); typing a number ticks the row
        const q = on ? chosen.get(p.product_id).q : emptyQ();
        const qtyCell = h('td', { class: 'pk-qty' }, already ? null : h('div', { class: 'pk-qbox' }, unitSlots(p).map((sl, i) => (sl ? h('label', { class: 'pk-q' },
          h('input', { type: 'number', min: '0', step: 'any', inputmode: 'decimal', value: q[i], placeholder: '0',
            'aria-label': p.product_code + ' ' + unitName(sl.code),
            oninput: (e) => {
              pick(p); chosen.get(p.product_id).q[i] = e.target.value;
              const tr = e.target.closest('tr'); tr.classList.add('picked'); tr.querySelector('.pk-cb input').checked = true;
              refreshCount(); syncAll();
            },
            onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); const all = [...body.querySelectorAll('.pk-q input')]; const n = all[all.indexOf(e.target) + 1]; if (n) n.focus(); } } }),
          h('span', {}, unitName(sl.code))) : null))));
        return h('tr', { class: 'click' + (on ? ' picked' : '') + (already ? ' already' : ''), onclick: (e) => {
          if (already || e.target === cb || e.target.closest('.pk-qty')) return;
          if (chosen.has(p.product_id)) chosen.delete(p.product_id); else pick(p);
          draw();
        } },
          h('td', { class: 'pk-cb' }, cb), td(p.product_code), h('td', {}, p.name, already ? h('span', { class: 'badge pk-in' }, t('pk.already')) : null),
          td(p.category_name || ''), td(have == null ? '' : availBadges(p, have), true), qtyCell);
      }));
      refreshCount();
    }
    async function load(keep) {
      if (!keep) pg.page = 0;
      try {
        const rows = pg.take(await api('/products' + qs({ q: f.q, category_id: f.cat, in_stock_warehouse_id: f.inStock ? opts.warehouseId : '', ...pg.q() })));
        shown = rows;
        box.replaceChildren(rows.length ? h('div', { class: 'tablewrap' }, h('table', {},
          h('thead', {}, h('tr', {}, h('th', { class: 'pk-cb' }, allBox), h('th', {}, t('lbl.code')), h('th', {}, t('lbl.product')),
            h('th', {}, t('lbl.category')), h('th', { class: 'num' }, t('uq.available')), h('th', {}, t('pk.qty')))), body))
          : h('div', { class: 'empty' }, icon('inbox'), h('span', {}, t('lbl.noData'))));
        draw();
      } catch (e) { fail(e); }
    }
    const cats = S.categories.filter((c) => c.is_active);
    const search = h('input', { type: 'search', placeholder: t('pk.searchPh'), 'aria-label': t('pk.searchPh'),
      oninput: debounce((e) => { f.q = e.target.value.trim(); load(); }, 250),
      onkeydown: (e) => { if (e.key === 'Enter') e.preventDefault(); } });
    dlg.append(
      h('div', { class: 'pk-head' }, h('h3', { id: 'pk-title' }, icon('list'), t('pk.title')),
        h('button', { type: 'button', class: 'link pk-x', 'aria-label': t('btn.cancel'), onclick: close }, '✕')),
      h('div', { class: 'pk-tools' },
        h('div', { class: 'pk-search' }, icon('search'), search),
        field(t('lbl.category'), select([['', t('lbl.all')], ...cats.map((c) => [c.category_id, c.name]), ['0', t('pk.noCategory')]], '',
          { onchange: (e) => { f.cat = e.target.value; load(); } })),
        opts.warehouseId ? h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: f.inStock, onchange: (e) => { f.inStock = e.target.checked; load(); } }), t('pk.inStock')) : null),
      cats.length ? h('div', { class: 'pk-chips', role: 'group', 'aria-label': t('lbl.category') },
        [['', t('lbl.all')], ...cats.map((c) => [String(c.category_id), c.name])].map(([v, l]) => h('button', { type: 'button', class: 'pk-chip' + (v === f.cat ? ' on' : ''),
          'aria-pressed': String(v === f.cat), onclick: (e) => {
            f.cat = v; dlg.querySelector('.pk-tools select').value = v;
            dlg.querySelectorAll('.pk-chip').forEach((x) => { const on = x === e.currentTarget; x.classList.toggle('on', on); x.setAttribute('aria-pressed', String(on)); });
            load();
          } }, l))) : null,
      box, pg.el,
      h('div', { class: 'pk-foot' }, count,
        h('button', { type: 'button', class: 'link', onclick: () => { chosen.clear(); draw(); } }, t('pk.clear')),
        h('span', { class: 'spacer' }), h('button', { type: 'button', onclick: close }, t('btn.cancel')), addBtn));
    // keep the chip row in step when the drop-down is used
    dlg.querySelector('.pk-tools select').addEventListener('change', (e) => dlg.querySelectorAll('.pk-chip').forEach((x, i) => {
      const v = i === 0 ? '' : String(cats[i - 1] && cats[i - 1].category_id);
      const on = v === e.target.value; x.classList.toggle('on', on); x.setAttribute('aria-pressed', String(on));
    }));
    document.body.append(dlg);
    dlg.showModal();
    search.focus();
    refreshCount();
    load();
  }

  /** Put picked products on a line form: fill empty rows first, skip ones already there. Returns the index of the first one. */
  function addPickedLines(lines, picked, blank) {
    let first = -1;
    for (const { product: p, q } of picked) {
      if (lines.some((l) => l.product && l.product.product_id === p.product_id)) continue;
      const fill = { product: p, q: q.map((x) => (numOr0(x) > 0 ? String(x) : '')) };
      let i = lines.findIndex((l) => !l.product && !l.q.some((x) => numOr0(x)));
      if (i >= 0) Object.assign(lines[i], blank(), fill); else { lines.push({ ...blank(), ...fill }); i = lines.length - 1; }
      if (first < 0) first = i;
    }
    return first;
  }
  function pickerButton(getOpts) {
    return h('button', { type: 'button', class: 'pick-btn', onclick: () => productPicker(getOpts()) }, icon('check-list'), t('pk.open'));
  }

  // ---------------------------------------------------------------- cancel a posted document
  /** Find a posted document by its number and cancel it: every line is reversed, so goods that left
      (sales, withdrawals, supplier returns) come back into the warehouse and goods that came in go out again. */
  async function pageCancel(id) {
    const V = viewFor();
    const head = [h('p', {}, h('a', { href: '#/docs' }, '← ' + t('nav.docs'))), h('h2', {}, t('nav.cancelDoc')), h('p', { class: 'hint' }, t('cx.hint'))];
    if (!isManager()) { V(...head, h('div', { class: 'banner warn' }, t('cx.managerOnly'))); return; }
    if (!id) {
      const box = h('div');
      const pg = makePager(() => load(true));
      const f = { q: '' };
      async function load(keep) {
        if (!keep) pg.page = 0;
        try {
          const rows = pg.take(await api('/documents' + qs({ q: f.q, status: 'POSTED', ...pg.q() })));
          box.replaceChildren(table([t('lbl.date'), t('lbl.docType'), t('lbl.docNo'), t('lbl.warehouse'), t('lbl.party'), { label: t('lbl.lines'), num: true }, ''],
            rows.filter((r) => r.reverses_document_id == null).map((r) => h('tr', { class: 'click', onclick: () => { location.hash = '#/cancel/' + r.document_id; } },
              td(fmtDate(r.doc_date)), td(docTypeLabel(r.doc_type)), td(h('b', {}, r.doc_no)), td(r.warehouse_name),
              td(r.customer_name || r.supplier_name || ''), td(r.line_count, true),
              h('td', { class: 'num' }, h('a', { class: 'btn small', href: '#/cancel/' + r.document_id }, t('cx.pick')))))));
        } catch (e) { fail(e); }
      }
      const inp = h('input', { type: 'search', class: 'bigsearch', placeholder: t('doc.searchPh'), 'aria-label': t('doc.searchPh'),
        oninput: debounce((e) => { f.q = e.target.value.trim(); load(); }) });
      V(...head, h('div', { class: 'filtercard' }, h('div', { class: 'searchrow' }, h('div', { class: 'searchbox' }, icon('search'), inp),
        barcodeCameraButton((code) => { inp.value = code; f.q = code; load(); }))),
        h('h3', { class: 'sect' }, t('cx.pickTitle')), box, pg.el);
      inp.focus();
      load();
      return;
    }
    let d;
    try { d = await api('/documents/' + id); } catch (e) { fail(e); location.hash = '#/cancel'; return; }
    const back = h('a', { class: 'btn', href: '#/cancel' }, '← ' + t('cx.other'));
    const groups = groupItems(d.items);
    const outflow = OUTFLOW.includes(d.doc_type);
    // effect of cancelling on each line: + comes back into the warehouse, − is taken out again
    const effect = (g) => {
      const base = g.items.reduce((a, i) => a + Number(i.base_qty), 0);
      const sign = d.doc_type === 'ADJUSTMENT' ? -(g.adj_sign || 1) : outflow ? 1 : -1;
      return h('span', { class: 'badge ' + (sign > 0 ? 'adj-in' : 'adj-out') }, (sign > 0 ? '+' : '−') + fmtNum(base) + ' ' + (sign > 0 ? t('cx.backIn') : t('cx.takeOut')));
    };
    const summary = h('div', { class: 'card grid' },
      h('div', { class: 'kv' }, h('span', {}, t('lbl.docNo')), h('b', {}, d.doc_no)),
      h('div', { class: 'kv' }, h('span', {}, t('lbl.docType')), h('b', {}, docTypeLabel(d.doc_type))),
      h('div', { class: 'kv' }, h('span', {}, t('lbl.date')), h('b', {}, fmtDate(d.doc_date))),
      h('div', { class: 'kv' }, h('span', {}, t('lbl.warehouse')), h('b', {}, d.warehouse_code + ' — ' + d.warehouse_name)),
      h('div', { class: 'kv' }, h('span', {}, t('lbl.party')), h('b', {}, d.customer_name || d.supplier_name || '—')),
      h('div', { class: 'kv' }, h('span', {}, t('lbl.status')), h('b', {}, statusBadge(d.status))));
    const items = table(['#', t('lbl.code'), t('lbl.product'), t('lbl.qty'), { label: t('uq.total'), num: true }, t('cx.effect')],
      groups.map((g, n) => h('tr', {}, td(n + 1), td(g.product_code), td(g.product_name),
        td(g.items.map((i) => fmtNum(i.qty) + ' ' + unitName(i.unit_code)).join(' + ')),
        td(fmtNum(g.items.reduce((a, i) => a + Number(i.base_qty), 0)), true), h('td', {}, effect(g)))));
    if (d.reverses_document_id != null || d.status !== 'POSTED') {
      const why = d.status === 'REVERSED' ? t('cx.already') : d.reverses_document_id != null ? t('cx.isReversal') : t('cx.notPosted');
      V(...head, h('div', { class: 'banner warn' }, why, ' ',
        d.reversed_by_document_id ? h('a', { href: '#/doc/' + d.reversed_by_document_id }, 'REV-' + d.doc_no) : h('a', { href: '#/doc/' + d.document_id }, d.doc_no)),
        summary, items, h('div', { class: 'actions' }, back));
      return;
    }
    const reason = h('textarea', { rows: 2, required: true, maxlength: 500, placeholder: t('cx.reasonPh') });
    const go = h('button', { type: 'button', class: 'danger-solid', onclick: async () => {
      if (!reason.value.trim()) { toast(t('cx.reasonRequired'), 'err'); reason.focus(); return; }
      if (!confirm(t('cx.confirm', { no: d.doc_no }))) return;
      go.disabled = true;
      try {
        const r = await api(`/documents/${id}/reverse`, { method: 'POST', body: { reason: reason.value.trim() }, headers: { 'Idempotency-Key': 'rev-' + id } });
        toast(t('cx.done', { no: d.doc_no }));
        V(...head, h('div', { class: 'banner ok' }, icon('check'), ' ', t('cx.doneLong', { no: d.doc_no }), ' ',
          h('a', { href: '#/doc/' + r.reversal_document_id }, r.document.doc_no)),
          summary, items, h('div', { class: 'actions' }, back, h('a', { class: 'btn', href: '#/doc/' + id }, t('cx.viewDoc'))));
      } catch (e) { go.disabled = false; fail(e); }
    } }, icon('ban'), t('cx.go'));
    const openDeps = (d.deposits || []).filter((x) => x.status !== 'CANCELLED' && x.status !== 'COMPLETED');
    V(...head, summary,
      openDeps.length ? h('div', { class: 'banner warn' }, t('cx.deposits', { n: openDeps.length })) : null,
      h('h3', { class: 'sect' }, t('cx.itemsTitle')), items,
      h('div', { class: 'card' }, field(t('cx.reason') + ' *', reason),
        h('div', { class: 'actions' }, back, h('span', { class: 'spacer' }), go)));
    reason.focus();
  }

  // ---------------------------------------------------------------- paging
  const PAGE_SIZES = [25, 50, 100, 200];
  const pageSize = () => { const n = Number(navStore.get('wms_page_size', '50')); return PAGE_SIZES.includes(n) ? n : 50; };
  /** Page bar: « ‹ 1 2 3 … › »  +  "Showing 51–100 of 191"  +  rows-per-page.
      total = a number when known; null when only "is there a next page" is known (server lists). */
  function makePager(onGo) {
    const pg = { page: 0, size: pageSize(), total: 0, more: false, shown: 0 };
    pg.el = h('nav', { class: 'pager', 'aria-label': t('pg.label') });
    pg.pages = () => (pg.total == null ? pg.page + (pg.more ? 2 : 1) : Math.max(1, Math.ceil(pg.total / pg.size)));
    pg.start = () => pg.page * pg.size;
    pg.slice = (arr) => arr.slice(pg.start(), pg.start() + pg.size);
    pg.showIndex = (i) => { pg.page = Math.max(0, Math.floor(i / pg.size)); };
    const go = (n) => { pg.page = n; onGo(); };
    pg.render = () => {
      const pages = pg.pages();
      if (pg.page > pages - 1) pg.page = pages - 1;
      const single = pg.total != null ? pg.total <= pg.size : pg.page === 0 && !pg.more;
      if (single) { pg.el.replaceChildren(); pg.el.hidden = true; return; }
      pg.el.hidden = false;
      const from = pg.start() + 1, to = pg.start() + (pg.total == null ? pg.shown : Math.min(pg.size, pg.total - pg.start()));
      const info = h('span', { class: 'pg-info' }, pg.total == null ? t('pg.showingOpen', { from: fmtNum(from), to: fmtNum(to) })
        : t('pg.showing', { from: fmtNum(from), to: fmtNum(to), total: fmtNum(pg.total) }));
      const btn = (label, n, aria, cur) => h('button', { type: 'button', class: 'pg-btn' + (cur ? ' cur' : ''), disabled: n < 0 || n >= pages || cur,
        'aria-label': aria, 'aria-current': cur ? 'page' : null, onclick: () => go(n) }, label);
      const nums = [];
      if (pg.total != null) {
        const want = new Set([0, pages - 1, pg.page - 1, pg.page, pg.page + 1].filter((n) => n >= 0 && n < pages));
        let last = -1;
        [...want].sort((a, b) => a - b).forEach((n) => {
          if (n - last > 1) nums.push(h('span', { class: 'pg-gap', 'aria-hidden': 'true' }, '…'));
          nums.push(btn(String(n + 1), n, t('pg.page', { n: n + 1 }), n === pg.page)); last = n;
        });
      } else nums.push(h('span', { class: 'pg-cur' }, t('pg.page', { n: pg.page + 1 })));
      const sizeSel = select(PAGE_SIZES.map((n) => [n, String(n)]), pg.size, { 'aria-label': t('pg.perPage'), onchange: (e) => {
        const first = pg.start(); pg.size = Number(e.target.value); navStore.set('wms_page_size', String(pg.size)); pg.showIndex(first); onGo();
      } });
      pg.el.replaceChildren(info, h('span', { class: 'pg-nav' },
        pg.total != null ? btn('«', 0, t('pg.first')) : null, btn('‹', pg.page - 1, t('pg.prev')), nums, btn('›', pg.page + 1, t('pg.next')),
        pg.total != null ? btn('»', pages - 1, t('pg.last')) : null),
        h('label', { class: 'pg-size' }, h('span', {}, t('pg.perPage')), sizeSel));
    };
    /** server lists: ask for one row more than a page to learn whether a next page exists */
    pg.q = () => ({ limit: pg.size + 1, offset: pg.start() });
    pg.take = (rows) => { pg.total = null; pg.more = rows.length > pg.size; const r = rows.slice(0, pg.size); pg.shown = r.length; pg.render(); return r; };
    return pg;
  }

  function table(headers, rows, opts = {}) {
    if (!rows.length) return h('div', { class: 'empty' }, icon('inbox'), h('span', {}, t('lbl.noData')));
    const body = h('tbody');
    // focusable so keyboard users can scroll a table that is wider than the screen
    const wrap = h('div', { class: 'tablewrap', tabindex: '0' },
      h('table', {},
        h('thead', {}, h('tr', {}, headers.map((x) => h('th', { class: x.num ? 'num' : '' }, x.label || x)))),
        body));
    if (opts.paged === false || rows.length <= PAGE_SIZES[0]) { body.append(...rows); return wrap; }
    // long lists are shown a page at a time (the row elements are kept, so typed values survive page changes)
    const pg = makePager(() => { draw(); wrap.scrollIntoView({ block: 'nearest' }); });
    pg.total = rows.length;
    const draw = () => { pg.render(); body.replaceChildren(...pg.slice(rows)); };
    draw();
    return h('div', { class: 'paged' }, wrap, pg.el);
  }
  const td = (v, num) => h('td', { class: num ? 'num' : '' }, v);

  const docTypeLabel = (x) => t('doc.' + x);
  const statusBadge = (s) => h('span', { class: 'badge ' + s.toLowerCase() }, t('status.' + s));

  function field(label, input, cls) { return h('label', { class: 'field ' + (cls || '') }, h('span', {}, label), input); }
  function select(options, value, props) {
    return h('select', props || {}, options.map(([v, l]) => h('option', { value: v, selected: String(v) === String(value) }, l)));
  }
  const whOptions = (list, withAll) => [...(withAll ? [['', t('lbl.all')]] : []), ...list.map((w) => [w.warehouse_id, w.warehouse_code + ' — ' + w.name])];

  async function loadRef() {
    // /me is refreshed too: the warehouses a user may view/transact in change when warehouses are created
    const [units, whs, cats, me] = await Promise.all([api('/units'), api('/warehouses'), api('/categories'), api('/me')]);
    S.units = units; S.whs = whs; S.categories = cats; S.me = me;
  }

  const isAdmin = () => S.me.role_code === 'ADMIN';
  const isManager = () => ['ADMIN', 'MANAGER'].includes(S.me.role_code);
  const canMaster = () => ['ADMIN', 'MANAGER', 'STOREKEEPER'].includes(S.me.role_code);
  const myDocTypes = () => {
    if (S.me.role_code === 'VIEWER' || !S.me.transact_warehouses.length) return [];
    if (S.me.role_code === 'SALES') return ['SALE', 'CUSTOMER_RETURN'];
    return DOC_TYPES;
  };
  const canImport = () => ['ADMIN', 'MANAGER', 'STOREKEEPER', 'SALES'].includes(S.me.role_code) && S.me.transact_warehouses.length > 0;
  const canTransfer = () => ['ADMIN', 'MANAGER', 'STOREKEEPER'].includes(S.me.role_code) && S.me.transact_warehouses.length > 0;
  const myTransactWhs = () => S.whs.filter((w) => S.me.transact_warehouses.includes(w.warehouse_id));

  // ---------------------------------------------------------------- shell
  function icon(name, cls = '') {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('class', 'i ' + cls);
    svg.setAttribute('aria-hidden', 'true');
    svg.innerHTML = (window.ICONS || {})[name] || '';
    return svg;
  }

  const navStore = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* ignore */ } },
  };

  function renderShell() {
    const app = document.getElementById('app');
    if (!S.token || !S.me) { app.replaceChildren(loginView()); return; }
    // groups: [id, label key, icon, items]; an item is [href, label key, icon]
    const groups = [
      ['daily', 'navg.daily', 'daily', [
        ['#/stock', 'nav.stock', 'stock'], ['#/docs', 'nav.docs', 'docs'],
        canScan() ? ['#/scan', 'nav.scan', 'scan'] : null,
        ['#/deposits', 'nav.deposits', 'deposits'], ['#/transfers', 'nav.transfers', 'transfers'], ['#/counts', 'nav.counts', 'counts'],
        myDocTypes().includes('ADJUSTMENT') ? ['#/new/ADJUSTMENT', 'nav.adjust', 'adjust'] : null,
        isManager() ? ['#/cancel', 'nav.cancelDoc', 'ban'] : null]],
      canImport() ? ['import', 'navg.import', 'importg', [['#/import', 'nav.import', 'import'], ['#/imports', 'nav.imports', 'imports']]] : null,
      ['reports', 'navg.reports', 'reportsg', [['#/movements', 'nav.movements', 'movements'], ['#/reports', 'nav.reports', 'reports']]],
      ['setup', 'navg.setup', 'setup', [
        ['#/products', 'nav.products', 'products'], ['#/masters', 'nav.masters', 'masters'],
        isAdmin() ? ['#/users', 'nav.users', 'users'] : null]],
    ].filter(Boolean).map(([id, label, ic, items]) => [id, label, ic, items.filter(Boolean)]);
    const cur = location.hash.split('/')[1] || 'dashboard';
    const isCur = (href) => (href.split('/').length > 2 ? location.hash === href : href.split('/')[1] === cur);
    // accordion: only one group is open — the one holding the current page, else the last one opened
    const activeGroup = groups.find(([, , , items]) => items.some(([href]) => isCur(href)));
    let openId = activeGroup ? activeGroup[0] : navStore.get('wms_nav_open1', 'daily');
    const layout = h('div', { class: 'layout' + (navStore.get('wms_nav_collapsed', '0') === '1' ? ' collapsed' : '') });
    const closeDrawer = () => layout.classList.remove('open');
    const link = ([href, key, ic], sub) => h('a', { href, title: t(key), class: (sub ? 'sub' : 'item') + (isCur(href) ? ' active' : ''), 'aria-current': isCur(href) ? 'page' : null, onclick: closeDrawer },
      icon(ic), h('span', { class: 'txt' }, t(key)));
    const groupEl = ([id, label, ic, items]) => {
      const open = openId === id;
      const box = h('div', { class: 'grp' + (open ? ' open' : '') + (items.some(([href]) => isCur(href)) ? ' has-active' : '') });
      box.append(
        h('button', { type: 'button', class: 'grp-head', title: t(label), 'aria-expanded': String(open), onclick: (e) => {
          const o = !box.classList.contains('open');
          box.parentNode.querySelectorAll('.grp.open').forEach((g) => {        // close the others
            g.classList.remove('open'); g.querySelector('.grp-head').setAttribute('aria-expanded', 'false');
          });
          box.classList.toggle('open', o);
          e.currentTarget.setAttribute('aria-expanded', String(o));
          openId = o ? id : '';
          navStore.set('wms_nav_open1', openId);
        } }, icon(ic), h('span', { class: 'txt' }, t(label)), icon('chevron', 'chev')),
        h('div', { class: 'grp-items' }, items.map((it) => link(it, true))));
      return box;
    };
    const side = h('aside', { class: 'side' },
      h('div', { class: 'side-brand' },
        h('a', { class: 'brand', href: '#/dashboard', title: t('app.title') }, h('img', { class: 'brand-logo', src: '/static/logo-hhx.png?v=1', alt: 'HHX', width: '55', height: '44' }),
          h('span', { class: 'txt' }, 'WMS-SVK'))),
      h('div', { class: 'side-user' },
        h('a', { class: 'avatar', href: '#/account', title: t('nav.account'), onclick: closeDrawer }, icon('user')),
        h('div', { class: 'txt' }, h('a', { href: '#/account', onclick: closeDrawer }, S.me.full_name), h('small', {}, '● ' + S.me.role_code))),
      h('div', { class: 'side-label' }, t('nav.mainMenu')),
      h('nav', { 'aria-label': t('nav.mainMenu') }, link(['#/dashboard', 'nav.dashboard', 'dashboard'], false), groups.map(groupEl)),
      h('div', { class: 'side-foot' },
        h('label', { class: 'langbox', title: 'Language' }, icon('lang'), langSelect()),
        h('button', { class: 'logout', type: 'button', onclick: logout, title: t('btn.logout') }, icon('logout'), h('span', { class: 'txt' }, t('btn.logout'))),
        h('button', { class: 'collapse', type: 'button', title: t('nav.collapse'), 'aria-label': t('nav.collapse'), 'aria-pressed': String(layout.classList.contains('collapsed')), onclick: (e) => {
          const c = layout.classList.toggle('collapsed');
          e.currentTarget.setAttribute('aria-pressed', String(c));
          navStore.set('wms_nav_collapsed', c ? '1' : '0');
        } }, icon('chevron'))));
    const topbar = h('header', { class: 'mtop' },
      h('button', { class: 'burger', type: 'button', 'aria-label': t('nav.menu'), 'aria-expanded': 'false', onclick: (e) => {
        e.currentTarget.setAttribute('aria-expanded', String(layout.classList.toggle('open')));
      } }, icon('menu')),
      h('span', { class: 'brand' }, h('img', { class: 'brand-logo', src: '/static/logo-hhx.png?v=1', alt: 'HHX', width: '38', height: '30' }), 'WMS-SVK'));
    const skip = h('button', { class: 'skip', type: 'button', onclick: () => { const m = main(); m.focus(); m.scrollIntoView(); } }, t('nav.skip'));
    layout.addEventListener('keydown', (e) => { if (e.key === 'Escape' && layout.classList.contains('open')) { closeDrawer(); topbar.querySelector('.burger').focus(); } });
    layout.append(skip, side, h('div', { class: 'backdrop', onclick: closeDrawer }),
      h('div', { class: 'content' }, topbar, h('main', { id: 'main', tabindex: '-1' })));
    app.replaceChildren(layout);
  }

  function langSelect() {
    return h('select', { class: 'lang', 'aria-label': 'Language', onchange: (e) => setLang(e.target.value) },
      [['th', 'ไทย'], ['en', 'English'], ['lo', 'ລາວ']].map(([v, l]) => h('option', { value: v, selected: v === S.lang }, l)));
  }
  function setLang(l) {
    S.lang = l; localStorage.setItem('wms_lang', l);
    document.documentElement.lang = l;
    document.title = t('app.title');
    if (S.me) { renderShell(); route(); } else renderShell();
  }

  function loginView() {
    const u = h('input', { type: 'text', autocomplete: 'username', required: true });
    const p = h('input', { type: 'password', autocomplete: 'current-password', required: true });
    return h('main', { class: 'login' },
      h('form', { class: 'login-card', 'aria-labelledby': 'login-title',
        onsubmit: async (e) => {
          e.preventDefault();
          try {
            const r = await api('/auth/login', { method: 'POST', body: { username: u.value.trim(), password: p.value } });
            S.token = r.token; localStorage.setItem('wms_token', r.token);
            await boot();
          } catch (err) { fail(err); }
        },
      },
      h('div', { class: 'lang-row' }, icon('lang'), langSelect()),
      h('img', { class: 'login-logo', src: '/static/logo-hhx.png?v=1', alt: 'HHX', width: '240', height: '180' }),
      h('h1', { id: 'login-title' }, t('app.title')),
      h('p', { class: 'login-sub' }, t('login.sub')),
      field(t('lbl.username'), u), field(t('lbl.password'), p),
      h('button', { class: 'primary big', type: 'submit' }, t('btn.login'))));
  }

  function logout() {
    S.token = null; S.me = null; localStorage.removeItem('wms_token');
    renderShell();
  }

  // ---------------------------------------------------------------- stock balances
  async function pageStock() {
    const f = { view: 'by_warehouse', warehouse_id: '', q: '', low_only: false };
    const box = h('div');
    const pg = makePager(() => load(true));
    async function load(keep) {
      if (!keep) pg.page = 0;
      try {
        const rows = pg.take(await api('/stock/balances' + qs({ ...f, ...pg.q() })));
        const total = f.view === 'total';
        box.replaceChildren(table(
          [t('lbl.code'), t('lbl.product'), ...(total ? [t('lbl.byWarehouse')] : [t('lbl.warehouse')]),
           { label: t('lbl.availPc'), num: true }, t('lbl.breakdown'), { label: t('dep.colHeld'), num: true }, { label: t('dep.colPhysical'), num: true },
           { label: t('lbl.reorder'), num: true }],
          rows.map((r) => h('tr', { class: Number(r.qty_on_hand) <= Number(r.reorder_level) && r.reorder_level > 0 ? 'low' : '' },
            td(r.product_code), td(r.name),
            total ? td(r.by_warehouse.map((x) => x.warehouse_code + ': ' + fmtNum(x.qty_on_hand)).join(' · ')) : td(r.warehouse_code + ' — ' + r.warehouse_name),
            td(fmtNum(r.qty_on_hand), true), td(breakdown(r.qty_on_hand, r.conversions)),
            td(Number(r.deposit_qty) ? h('a', { href: '#/deposits' }, fmtNum(r.deposit_qty)) : '', true),
            td(Number(r.deposit_qty) ? fmtNum(r.physical_qty) : '', true), td(fmtNum(r.reorder_level), true)))));
      } catch (e) { fail(e); }
    }
    const q = h('input', { type: 'search', placeholder: t('lbl.searchProduct'), oninput: debounce((e) => { f.q = e.target.value; load(); }) });
    view(h('h2', {}, t('nav.stock')), h('p', { class: 'hint' }, t('dep.stockHint')),
      h('div', { class: 'filters' },
        field(t('lbl.view'), select([['by_warehouse', t('lbl.viewByWh')], ['total', t('lbl.viewTotal')]], f.view, { onchange: (e) => { f.view = e.target.value; load(); } })),
        field(t('lbl.warehouse'), select(whOptions(S.whs, true), '', { onchange: (e) => { f.warehouse_id = e.target.value; load(); } })),
        field(t('btn.search'), q),
        h('label', { class: 'check' }, h('input', { type: 'checkbox', onchange: (e) => { f.low_only = e.target.checked; load(); } }), t('lbl.lowOnly'))),
      box, pg.el);
    load();
  }

  function debounce(fn, ms = 300) { let id; return (...a) => { clearTimeout(id); id = setTimeout(() => fn(...a), ms); }; }

  /** A button that opens a short list of links (keyboard: Enter/Space opens, arrows move, Escape closes). */
  function menuButton(label, items, cls = '') {
    const list = h('div', { class: 'menu-list', role: 'menu', hidden: true },
      items.map(([text, href]) => h('a', { role: 'menuitem', href, tabindex: '-1' }, text)));
    const btn = h('button', { type: 'button', class: 'menu-btn ' + cls, 'aria-haspopup': 'menu', 'aria-expanded': 'false' }, label, icon('chevron', 'chev'));
    const links = () => [...list.querySelectorAll('a')];
    const close = (refocus) => { list.hidden = true; btn.setAttribute('aria-expanded', 'false'); document.removeEventListener('click', outside, true); if (refocus) btn.focus(); };
    const outside = (e) => { if (!box.contains(e.target)) close(false); };
    const open = () => { list.hidden = false; btn.setAttribute('aria-expanded', 'true'); document.addEventListener('click', outside, true); links()[0].focus(); };
    btn.addEventListener('click', () => (list.hidden ? open() : close(true)));
    btn.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') { e.preventDefault(); open(); } });
    list.addEventListener('keydown', (e) => {
      const a = links(), i = a.indexOf(document.activeElement);
      if (e.key === 'Escape') { e.preventDefault(); close(true); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); a[(i + 1) % a.length].focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); a[(i - 1 + a.length) % a.length].focus(); }
      else if (e.key === 'Tab') close(false);
    });
    list.addEventListener('click', () => close(false));
    const box = h('div', { class: 'menu' }, btn, list);
    return box;
  }

  // ---------------------------------------------------------------- documents
  async function pageDocs() {
    const f = { doc_type: '', warehouse_id: '', status: '', q: '', date_from: '', date_to: '' };
    const box = h('div');
    const pg = makePager(() => load(true));
    async function load(keep) {
      if (!keep) pg.page = 0;
      try {
        const rows = pg.take(await api('/documents' + qs({ ...f, ...pg.q() })));
        box.replaceChildren(table(
          [t('lbl.date'), t('lbl.docType'), t('lbl.docNo'), t('lbl.warehouse'), t('lbl.party'), { label: t('lbl.lines'), num: true }, t('lbl.status'), t('lbl.createdBy')],
          rows.map((r) => h('tr', { class: 'click', onclick: () => { location.hash = '#/doc/' + r.document_id; } },
            td(fmtDate(r.doc_date)), td(docTypeLabel(r.doc_type)), td(r.doc_no), td(r.warehouse_name),
            td(r.customer_name || r.supplier_name || ''), td(r.line_count, true), td(statusBadge(r.status)), td(r.created_by_name)))));
      } catch (e) { fail(e); }
    }
    const newBtns = [];
    const createTypes = myDocTypes().filter((x) => x !== 'CUSTOMER_RETURN');   // returns go through "cancel document"
    if (createTypes.length) newBtns.push(menuButton(h('span', {}, icon('plus'), t('doc.newMenu')), createTypes.map((x) => [docTypeLabel(x), '#/new/' + x]), 'primary'));
    if (canScan()) newBtns.push(h('a', { class: 'btn', href: '#/scan' }, icon('scan'), t('nav.scan')));
    if (isManager()) newBtns.push(h('a', { class: 'btn danger-btn', href: '#/cancel' }, icon('ban'), t('nav.cancelDoc')));
    const docNoInput = h('input', { type: 'search', class: 'bigsearch', placeholder: t('doc.searchPh'), 'aria-label': t('doc.searchPh'),
      oninput: debounce((e) => { f.q = e.target.value.trim(); load(); }) });
    // one card: the filters in a row, the "new document" / "scan bill" buttons underneath
    view(h('h2', {}, t('nav.docs')),
      h('div', { class: 'filtercard' },
        h('div', { class: 'searchrow' }, h('div', { class: 'searchbox' }, icon('search'), docNoInput),
          barcodeCameraButton((code) => { docNoInput.value = code; f.q = code; load(); })),
        h('div', { class: 'filters' },
          field(t('lbl.docType'), select([['', t('lbl.all')], ...DOC_TYPES.map((x) => [x, docTypeLabel(x)])], '', { onchange: (e) => { f.doc_type = e.target.value; load(); } })),
          field(t('lbl.warehouse'), select(whOptions(S.whs, true), '', { onchange: (e) => { f.warehouse_id = e.target.value; load(); } })),
          field(t('lbl.status'), select([['', t('lbl.all')], ...['DRAFT', 'POSTED', 'REVERSED'].map((x) => [x, t('status.' + x)])], '', { onchange: (e) => { f.status = e.target.value; load(); } })),
          field(t('lbl.dateFrom'), h('input', { type: 'date', onchange: (e) => { f.date_from = e.target.value; load(); } })),
          field(t('lbl.dateTo'), h('input', { type: 'date', onchange: (e) => { f.date_to = e.target.value; load(); } }))),
        newBtns.length ? h('div', { class: 'df-actions' }, newBtns) : null),
      box, pg.el);
    load();
  }

  async function pageDoc(id) {
    const V = viewFor();
    let d;
    try { d = await api('/documents/' + id); } catch (e) { fail(e); if (main() === V.el) location.hash = '#/docs'; return; }
    const isRev = d.reverses_document_id != null;
    const act = [];
    if (myDocTypes().includes(d.doc_type) && S.me.transact_warehouses.includes(d.warehouse_id)) {
      if (d.status === 'DRAFT') {
        act.push(h('button', { class: 'primary', onclick: async () => {
          if (!confirm(t('msg.confirmPost'))) return;
          try { await api(`/documents/${id}/post`, { method: 'POST', headers: { 'Idempotency-Key': 'post-' + id } }); toast(t('msg.posted')); pageDoc(id); } catch (e) { fail(e); }
        } }, t('btn.post')));
        act.push(h('button', { class: 'danger', onclick: async () => {
          if (!confirm(t('msg.confirmDelete'))) return;
          try { await api('/documents/' + id, { method: 'DELETE' }); toast(t('msg.deleted')); location.hash = '#/docs'; } catch (e) { fail(e); }
        } }, t('btn.delete')));
      }
      if (d.status === 'POSTED' && !isRev && d.doc_type === 'SALE' && canDeposit() && d.items.some((i) => i.base_qty > (i.deposited_base || 0))) {
        act.push(h('a', { class: 'btn', href: '#/deposit-new/' + id }, icon('deposits'), t('dep.createFromSale')));
      }
      if (d.status === 'POSTED' && !isRev && isManager()) {
        act.push(h('a', { class: 'btn danger-btn', href: '#/cancel/' + id }, icon('ban'), t('btn.reverse')));
      }
    }
    const kv = (k, v) => h('div', { class: 'kv' }, h('span', {}, k), h('b', {}, v ?? ''));
    const hasDep = d.items.some((i) => i.deposited_base > 0);
    const isAdj = d.doc_type === 'ADJUSTMENT';
    const groups = groupItems(d.items);
    if (d.bill_scan_id) act.unshift(h('button', { onclick: () => showBillImage(d.bill_scan_id) }, icon('receipt'), t('scan.viewBill')));
    V(
      h('p', {}, h('a', { href: '#/docs' }, '← ' + t('nav.docs'))),
      h('h2', {}, docTypeLabel(d.doc_type) + ' ' + d.doc_no + ' ', statusBadge(d.status)),
      h('div', { class: 'card grid' },
        kv(t('lbl.date'), fmtDate(d.doc_date)), kv(t('lbl.warehouse'), d.warehouse_code + ' — ' + d.warehouse_name),
        kv(t('lbl.party'), d.customer_name || d.supplier_name || '—'), kv(t('lbl.createdBy'), d.created_by_name + ' ' + fmtDateTime(d.created_at)),
        d.posted_at ? kv(t('lbl.postedBy'), (d.posted_by_name || '') + ' ' + fmtDateTime(d.posted_at)) : null,
        d.reverses_document_id ? kv(t('lbl.reversesDoc'), h('a', { href: '#/doc/' + d.reverses_document_id }, '#' + d.reverses_document_id)) : null,
        d.reversed_by_document_id ? kv(t('cx.cancelledBy'), h('a', { href: '#/doc/' + d.reversed_by_document_id }, 'REV-' + d.doc_no)) : null,
        d.reversed_by_document_id && d.reversal_reason ? kv(t('cx.reason'), d.reversal_reason) : null,
        d.adjust_reason ? kv(t('adj.reason'), t('adj.r.' + d.adjust_reason)) : null,
        d.remark ? kv(t('lbl.remark'), d.remark) : null),
      h('div', { class: 'card lines-card' },
        h('div', { class: 'lines-head' }, h('b', {}, icon('list'), t('uq.items')), h('span', { class: 'hint' }, t('xl.count', { n: groups.length })),
          h('span', { class: 'hint' }, t('uq.totalPc', { n: fmtNum(groups.reduce((a, g) => a + g.base, 0)) }))),
        table(['#', t('lbl.code'), t('lbl.product'), ...(isAdj ? [t('adj.dir')] : []), ...[1, 2, 3].flatMap((n) => [{ label: t('uq.qtyN', { n }), num: true }, t('uq.unitN', { n })]),
          { label: t('uq.total'), num: true }, ...(hasDep ? [{ label: t('dep.colDepositPc'), num: true }] : [])],
        groups.map((g, n) => h('tr', {}, td(n + 1), td(g.product_code), td(g.product_name),
          isAdj ? td(h('span', { class: 'badge ' + (g.adj_sign > 0 ? 'adj-in' : 'adj-out') }, g.adj_sign > 0 ? t('adj.in') : t('adj.out'))) : null,
          unitTextCells(g),
          td(h('b', {}, fmtNum(g.base)), true), hasDep ? td(g.deposited ? fmtNum(g.deposited) : '', true) : null)))),
      d.deposits && d.deposits.length ? h('div', { class: 'card' }, h('h3', {}, t('nav.deposits')),
        table([t('dep.no'), t('lbl.date'), { label: t('dep.remainingPc'), num: true }, t('lbl.status')],
          d.deposits.map((x) => h('tr', { class: 'click', onclick: () => { location.hash = '#/deposit/' + x.deposit_id; } },
            td(x.deposit_no), td(fmtDate(x.deposit_date)), td(fmtNum(x.qty_remaining_base), true), td(depBadge(x.status)))))) : null,
      h('div', { class: 'actions' }, act));
  }

  const OUTFLOW = ['SALE', 'WITHDRAWAL', 'SUPPLIER_RETURN'];

  async function pageNewDoc(type) {
    if (!myDocTypes().includes(type)) { location.hash = '#/docs'; return; }
    const V = viewFor();
    const whs = myTransactWhs();
    const partyKind = PARTY[type];
    let parties = partyKind ? await api('/' + (partyKind === 'customer' ? 'customers' : 'suppliers')) : [];
    const draft = S.scanDraft && S.scanDraft.doc_type === type ? S.scanDraft : null;
    S.scanDraft = null;
    const isSale = type === 'SALE';
    const isOut = OUTFLOW.includes(type);
    const isAdj = type === 'ADJUSTMENT';
    const key = uuid();
    const state = { warehouse_id: whs[0] && whs[0].warehouse_id, doc_date: today(), doc_no: '', party: '', remark: '', reason: '' };
    const defDir = () => ADJ_DEFAULT_DIR[state.reason] || 'OUT';
    const blank = () => ({ product: null, q: emptyQ(), dep: '', dir: defDir(), dirSet: false });
    let lines = [blank()];
    if (draft) {
      if (draft.warehouse_id && whs.some((w) => w.warehouse_id === draft.warehouse_id)) state.warehouse_id = draft.warehouse_id;
      state.doc_date = draft.doc_date || today();
      state.doc_no = draft.doc_no || '';
      state.party = draft.customer_id || '';
      state.remark = t('scan.remark') + (draft.notes ? ': ' + draft.notes : '');
      const prods = {};
      await Promise.all([...new Set(draft.items.filter((i) => i.product_code).map((i) => i.product_code))].map(async (code) => {
        try { const r = await findByCode(code); if (r) prods[code] = r.product; } catch (_) { /* ignore */ }
      }));
      lines = draft.items.map((i) => {
        const p = i.product_code ? prods[i.product_code] || null : null;
        const ln = { product: p, q: emptyQ(), dep: '', warnings: i.warnings || [], source: i.source || {}, text: '' };
        if (p && i.qty) {
          addUnitQty(p, ln.q, i.unit_code || 'PC', i.qty);
          if (isSale && i.deposit_qty) {
            const cv = (p.conversions || []).find((c) => c.unit_code === i.unit_code);
            ln.dep = String(numOr0(i.deposit_qty) * (cv ? cv.factor_to_base : 1));
          }
        } else if (!p && i.qty) ln.q[2] = String(i.qty);
        return ln;
      });
      if (!lines.length) lines = [blank()];
    }
    const body = h('tbody');
    const lpg = makePager(() => { renderLines(); body.closest('.lines-card').scrollIntoView({ block: 'nearest' }); });
    const bad = (i, msg) => { lpg.showIndex(i); renderLines(); toast(msg, 'err'); };
    const lineCount = h('span', { class: 'hint' });
    const totalBox = h('span', { class: 'hint' });
    let avail = {};
    let busy = false;
    const refreshTotals = () => {
      const used = lines.filter((l) => l.product);
      lineCount.textContent = t('xl.count', { n: used.length });
      totalBox.textContent = t('uq.totalPc', { n: fmtNum(used.reduce((a, l) => a + qtyBase(l.product, l.q), 0)) });
    };

    function renderLines() {
      refreshTotals();
      lpg.total = lines.length; lpg.render();
      const off = lpg.start();
      body.replaceChildren(...lpg.slice(lines).map((ln, i) => {
        const idx = off + i;
        const dl = 'dl' + idx;
        const search = h('input', { type: 'text', list: dl, class: 'psearch', placeholder: t('lbl.searchProduct'), value: ln.product ? `${ln.product.product_code} — ${ln.product.name}` : ln.text || '',
          autocomplete: 'off',
          oninput: debounce(async (e) => {
            ln.text = e.target.value;
            try {
              const found = await api('/products' + qs({ q: e.target.value.split(' — ')[0], limit: 15 }));
              ln.options = found;
              document.getElementById(dl).replaceChildren(...found.map((p) => h('option', { value: `${p.product_code} — ${p.name}` })));
            } catch (_) { /* ignore while typing */ }
          }, 250),
          onchange: async (e) => {
            const text = e.target.value.trim();
            let p = (ln.options || []).find((x) => `${x.product_code} — ${x.name}` === text);
            let unit = null;
            if (!p && text) {
              try { const r = await findByCode(text.split(' — ')[0]); if (r) { p = r.product; unit = r.unit_code; } } catch (err) { fail(err); }
            }
            if (!p) { ln.product = null; toast(t('err.PRODUCT_NOT_FOUND'), 'err'); renderLines(); return; }
            if (ln.product && ln.product.product_id === p.product_id) return;   // same product picked again: keep quantities
            const keepPc = !ln.product && numOr0(ln.q[2]) > 0 ? ln.q[2] : '';
            ln.product = p; ln.q = emptyQ(); ln.warnings = [];
            if (keepPc) ln.q[2] = keepPc;
            renderLines();
            const slots = unitSlots(p);
            const want = unit ? slots.findIndex((s) => s && s.code === unit) : slots.findIndex(Boolean);
            const inp = body.querySelectorAll('tr')[idx - off].querySelector(`input.uq[data-slot="${Math.max(0, want)}"]`);
            if (inp) inp.focus();
          },
        });
        const src = ln.source && (ln.source.code || ln.source.description)
          ? h('div', { class: 'srcline' }, t('scan.onBill') + ': ', [ln.source.code, ln.source.description, ln.source.unit ? '(' + ln.source.unit + ')' : null].filter(Boolean).join(' ')) : null;
        const warn = (ln.warnings || []).length ? h('div', { class: 'linewarn' }, '⚠ ' + ln.warnings.map(warnText).join(' · ')) : null;
        const totalCell = h('td', { class: 'num tot' });
        const stateCell = h('td', {});
        const have = ln.product ? (avail[ln.product.product_id] || 0) : null;
        const update = () => {
          const b = ln.product ? qtyBase(ln.product, ln.q) : 0;
          totalCell.textContent = b ? (isAdj ? (ln.dir === 'IN' ? '+' : '−') : '') + fmtNum(b) : '';
          totalCell.className = 'num tot' + (isAdj && b ? (ln.dir === 'IN' ? ' plus' : ' minus') : '');
          if (isOut) stateCell.replaceChildren(stockState(b, have) || '');
          if (isAdj) {
            const after = have == null ? null : have + (ln.dir === 'IN' ? b : -b);
            stateCell.replaceChildren(after == null || !b ? '' : after < 0 ? h('span', { class: 'sb no' }, t('uq.short'))
              : h('b', { class: 'after' }, fmtNum(after)));
          }
          refreshTotals();
        };
        // + / − switch for adjustment lines
        const dirCell = isAdj ? h('td', {}, h('div', { class: 'seg', role: 'group', 'aria-label': t('adj.dir') },
          ['IN', 'OUT'].map((d) => h('button', { type: 'button', class: 'seg-btn ' + (d === 'IN' ? 'in' : 'out') + (ln.dir === d ? ' on' : ''),
            'aria-pressed': String(ln.dir === d), onclick: (e) => {
              ln.dir = d; ln.dirSet = true;
              e.currentTarget.parentNode.querySelectorAll('button').forEach((x) => {
                const on = x === e.currentTarget; x.classList.toggle('on', on); x.setAttribute('aria-pressed', String(on));
              });
              update();
            } }, d === 'IN' ? t('adj.in') : t('adj.out'))))) : null;
        const row = h('tr', { class: (ln.warnings || []).length ? 'warnrow' : '' },
          td(idx + 1),
          h('td', { class: 'pcell' }, search, h('datalist', { id: dl }), src, warn),
          dirCell,
          unitCells(ln.product, ln.q, update),
          totalCell,
          isSale ? h('td', { class: 'num' }, h('input', { class: 'qty dep', type: 'number', min: '0', step: '1', value: ln.dep, title: t('dep.lineHint'), disabled: !ln.product,
            oninput: (e) => { ln.dep = e.target.value; } })) : null,
          h('td', { class: 'avail' }, availBadges(ln.product, have)),
          isOut || isAdj ? stateCell : null,
          h('td', {}, lines.length > 1 ? h('button', { class: 'link', type: 'button', title: t('btn.delete'), onclick: () => { lines.splice(idx, 1); renderLines(); } }, '✕') : null));
        update();
        return row;
      }));
    }
    async function reloadAvail() { avail = await loadAvail(state.warehouse_id); renderLines(); }

    async function addByCode(code) {
      if (!code) return;
      let r;
      try { r = await findByCode(code); } catch (e) { fail(e); return; }
      if (!r) { toast(t('err.PRODUCT_NOT_FOUND') + ': ' + code, 'err'); return; }
      let ln = lines.find((l) => l.product && l.product.product_id === r.product.product_id);
      if (!ln) {
        ln = lines.find((l) => !l.product && !l.q.some((x) => numOr0(x)));
        if (ln) Object.assign(ln, blank(), { product: r.product }); else { ln = { ...blank(), product: r.product }; lines.push(ln); }
      }
      addUnitQty(r.product, ln.q, r.unit_code, 1);
      lpg.showIndex(lines.indexOf(ln));
      renderLines();
      toast(`+1 ${unitName(r.unit_code)} ${r.product.product_code} — ${r.product.name}`);
    }
    // ---- fill lines from an Excel / CSV file (e.g. 200-300 opening-balance items)
    const fileReport = h('div');
    async function importLines(e) {
      const f = e.target.files[0]; e.target.value = '';
      if (!f) return;
      const fd = new FormData();
      fd.append('file', f, f.name);
      toast(t('xl.reading'));
      let r;
      try { r = await api('/document-lines/parse', { method: 'POST', form: fd }); } catch (err) { fail(err); return; }
      const kept = lines.filter((l) => l.product || l.q.some((x) => numOr0(x)));
      const byId = new Map(kept.filter((l) => l.product).map((l) => [l.product.product_id, l]));
      for (const x of r.lines) {
        let ln = byId.get(x.product.product_id);
        if (!ln) { ln = { ...blank(), product: x.product }; kept.push(ln); byId.set(x.product.product_id, ln); }
        addUnitQty(x.product, ln.q, x.unit_code, x.qty);
      }
      lines = kept.length ? kept : [blank()];
      const nProducts = new Set(r.lines.map((x) => x.product.product_id)).size;
      lpg.page = 0;
      renderLines();
      const problems = [
        ...r.unmatched.map((u) => [u.row, t('xl.notFound', { code: [u.code, u.name].filter(Boolean).join(' '), qty: u.qty })]),
        ...r.errors.map((x) => [x.row, t('xl.e.' + x.code, x)]),
      ].sort((a, b) => a[0] - b[0]);
      fileReport.replaceChildren(h('div', { class: 'banner ' + (problems.length ? 'warn' : 'ok') + ' xlreport' },
        h('b', {}, '📥 ' + f.name + ': '),
        t('xl.summary', { n: nProducts, sheet: r.sheet }),
        r.merged ? ' · ' + t('xl.merged', { n: r.merged }) : '',
        r.skipped_empty ? ' · ' + t('xl.skipped', { n: r.skipped_empty }) : '',
        problems.length ? h('details', { open: problems.length <= 10 },
          h('summary', {}, t('xl.problems', { n: problems.length })),
          h('ul', {}, problems.map(([row, msg]) => h('li', {}, t('xl.row', { row }) + ': ' + msg)))) : null,
        h('button', { type: 'button', class: 'link', onclick: () => fileReport.replaceChildren() }, '✕')));
      toast(t('xl.done', { n: nProducts }), problems.length ? 'err' : undefined);
    }
    const xlBar = h('div', { class: 'xlbar' },
      h('label', { class: 'btn' }, icon('sheet'), t('xl.import'), h('input', { type: 'file', accept: '.xlsx,.xls,.csv,.htm,.html', class: 'vh', onchange: importLines })),
      h('button', { type: 'button', class: 'link', onclick: () => downloadFile('/document-lines/template', { with_products: true }, 'items-template.xlsx') }, t('xl.template')),
      h('span', { class: 'hint' }, t('xl.hint')));
    const scanInput = h('input', { type: 'text', class: 'scaninput', placeholder: t('scan.inputPh'), 'aria-label': t('scan.inputPh'), autocomplete: 'off',
      onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); const v = e.target.value.trim(); e.target.value = ''; addByCode(v); } } });

    async function submit(post) {
      if (busy) return;
      const items = [];
      for (const [i, ln] of lines.entries()) {
        const where = t('lbl.line') + ' ' + (i + 1) + ': ';
        if (!ln.product) {
          if (lines.length > 1 && !ln.q.some((x) => numOr0(x))) continue;      // ignore empty rows
          bad(i, where + t('err.PRODUCT_NOT_FOUND')); return;
        }
        if (ln.q.some((x) => numOr0(x) < 0)) { bad(i, where + t('err.BAD_QTY')); return; }
        const parts = qItems(ln.product, ln.q);
        const base = qtyBase(ln.product, ln.q);
        if (!parts.length || !(base > 0)) { bad(i, where + t('err.BAD_QTY')); return; }
        let dep = isSale ? Math.round(numOr0(ln.dep)) : 0;
        if (dep > base) { bad(i, where + t('err.DEPOSIT_EXCEEDS_SALE')); return; }
        for (const pt of parts) {
          const b = Math.round(numOr0(pt.qty) * pt.factor);
          const take = Math.min(dep, b);
          dep -= take;
          items.push({ product_id: ln.product.product_id, unit_code: pt.unit_code, qty: pt.qty, deposit_qty_base: take || null,
            direction: isAdj ? ln.dir : null });
        }
      }
      if (!items.length) { toast(t('err.NO_ITEMS'), 'err'); return; }
      if (isAdj && !state.reason) { toast(t('err.ADJUST_REASON_REQUIRED'), 'err'); reasonSel.focus(); return; }
      const payload = {
        adjust_reason: isAdj ? state.reason : null,
        doc_type: type, warehouse_id: Number(state.warehouse_id), doc_date: state.doc_date, doc_no: state.doc_no || null,
        remark: state.remark || null, items, post,
        customer_id: partyKind === 'customer' && state.party ? Number(state.party) : null,
        supplier_id: partyKind === 'supplier' && state.party ? Number(state.party) : null,
        bill_scan_id: draft ? draft.scan_id : null,
      };
      busy = true;
      try {
        const r = await api('/documents', { method: 'POST', body: payload, headers: { 'Idempotency-Key': key + (post ? '-p' : '-d') } });
        toast(post ? t('msg.posted') : t('msg.saved'));
        if (draft && draft.imageUrl) URL.revokeObjectURL(draft.imageUrl);
        location.hash = '#/doc/' + r.document_id;
      } catch (e) { fail(e); } finally { busy = false; }
    }

    const reasonSel = select([['', '—'], ...ADJ_REASONS.map((r) => [r, t('adj.r.' + r)])], state.reason, { required: true, onchange: (e) => {
      state.reason = e.target.value;
      lines.forEach((l) => { if (!l.dirSet) l.dir = defDir(); });   // lines not switched by hand follow the reason
      renderLines();
    } });
    const partyBox = h('div');
    function renderParty() {
      const sel = select([['', '—'], ...parties.filter((p) => p.is_active).map((p) => [p.id, p.code + ' — ' + p.name])], state.party, { onchange: (e) => { state.party = e.target.value; } });
      const sug = draft && !state.party && draft.customer_suggestion && partyKind === 'customer' ? draft.customer_suggestion : null;
      partyBox.replaceChildren(...present([sel, sug ? h('button', { type: 'button', class: 'small', onclick: async () => {
        try {
          const c = await api('/customers', { method: 'POST', body: { code: sug.code || ('C' + Date.now().toString().slice(-7)), name: sug.name || sug.code } });
          parties = await api('/customers'); state.party = c.id; renderParty(); toast(t('msg.saved'));
        } catch (e) { fail(e); }
      } }, icon('plus'), t('scan.addCustomer', { name: [sug.code, sug.name].filter(Boolean).join(' ') })) : null]));
    }
    renderParty();

    const warnings = draft && draft.warnings.length ? h('ul', { class: 'banner warn warnlist' }, draft.warnings.map((w) => h('li', {}, warnText(w),
      w.document_id ? [' ', h('a', { href: '#/doc/' + w.document_id }, t('scan.openDoc'))] : null))) : null;
    const form = [
      h('div', { class: 'card form' },
        field(t('lbl.warehouse'), select(draft && !state.warehouse_id ? [['', '—'], ...whOptions(whs)] : whOptions(whs), state.warehouse_id, { onchange: (e) => { state.warehouse_id = e.target.value; reloadAvail(); } })),
        field(t('lbl.date'), h('input', { type: 'date', value: state.doc_date, onchange: (e) => { state.doc_date = e.target.value; } })),
        field(t('lbl.docNoAuto'), h('input', { type: 'text', maxlength: 50, value: state.doc_no, oninput: (e) => { state.doc_no = e.target.value; } })),
        partyKind ? field(partyKind === 'customer' ? t('lbl.customer') : t('lbl.supplier'), partyBox) : null,
        isAdj ? field(t('adj.reason') + ' *', reasonSel) : null,
        field(t('lbl.remark'), h('input', { type: 'text', value: state.remark, oninput: (e) => { state.remark = e.target.value; } }), 'wide')),
      xlBar, fileReport,
      h('div', { class: 'scanbar' }, pickerButton(() => ({
        warehouseId: state.warehouse_id, inStock: isOut, avail,
        have: new Set(lines.filter((l) => l.product).map((l) => l.product.product_id)),
        onAdd: (picked) => {
          const first = addPickedLines(lines, picked, blank);
          if (first < 0) return;
          lpg.showIndex(first); renderLines();
          toast(t('pk.added', { n: picked.length }));
          const row = body.querySelectorAll('tr')[first - lpg.start()];
          const inp = row && row.querySelector('input.uq:not([disabled])');
          if (inp) inp.focus();
        },
      })), scanInput, barcodeCameraButton(addByCode)),
      h('div', { class: 'card lines-card' },
        h('div', { class: 'lines-head' }, h('b', {}, icon('list'), t('uq.items')), lineCount, totalBox),
        h('div', { class: 'tablewrap' },
          h('table', { class: 'lines' }, h('thead', {}, h('tr', {}, h('th', {}, '#'), h('th', {}, t('lbl.product')),
            isAdj ? h('th', {}, t('adj.dir')) : null, unitHeaders(),
            h('th', { class: 'num' }, t('uq.total')),
            isSale ? h('th', { class: 'num', title: t('dep.lineHint') }, t('dep.colDepositPc')) : null,
            h('th', {}, t('uq.available')), isOut ? h('th', {}, t('lbl.status')) : null, isAdj ? h('th', { class: 'num' }, t('adj.after')) : null,
            h('th', {}))), body), lpg.el)),
      isSale ? h('p', { class: 'hint' }, t('dep.lineHintPc')) : null,
      h('div', { class: 'actions' },
        h('button', { type: 'button', onclick: () => { lines.push(blank()); lpg.showIndex(lines.length - 1); renderLines(); const ins = body.querySelectorAll('input.psearch'); if (ins.length) ins[ins.length - 1].focus(); } }, icon('plus'), t('btn.addLine')),
        h('button', { type: 'button', class: 'link danger-link', onclick: () => {
          if (lines.length > 1 && !confirm(t('xl.confirmClear', { n: lines.length }))) return;
          lines = [blank()]; fileReport.replaceChildren(); renderLines();
        } }, t('xl.clear')),
        h('span', { class: 'spacer' }),
        h('button', { type: 'button', onclick: () => submit(false) }, t('btn.saveDraft')),
        h('button', { type: 'button', class: 'primary', onclick: () => submit(true) }, t('btn.savePost'))),
    ];
    V(h('p', {}, h('a', { href: '#/docs' }, '← ' + t('nav.docs'))),
      h('h2', {}, t('btn.new') + ' ' + docTypeLabel(type), draft ? h('span', { class: 'badge scan' }, icon('camera'), t('scan.fromBill')) : null),
      isAdj ? h('p', { class: 'hint' }, t('adj.hint')) : null,
      warnings,
      draft && draft.imageUrl
        ? h('div', { class: 'newdoc-split' }, h('div', {}, form),
          h('div', { class: 'card billside' }, h('h3', {}, t('scan.billImage')), h('img', { src: draft.imageUrl, class: 'billimg zoomable', onclick: (e) => e.target.classList.toggle('zoom') }),
            draft.total_amount != null ? h('p', { class: 'hint' }, t('scan.total') + ': ' + fmtNum(draft.total_amount)) : null,
            draft.is_handwritten ? h('p', { class: 'hint' }, icon('pen'), t('scan.handwritten')) : null,
            draft.ocr_text ? h('details', { class: 'ocrtext' }, h('summary', {}, t('scan.ocrText')), h('pre', {}, draft.ocr_text)) : null))
        : form);
    renderLines();
    reloadAvail();
    if (!draft) scanInput.focus();
  }

  // ---------------------------------------------------------------- movements
  async function pageMovements() {
    const f = { product_id: '', warehouse_id: '', doc_type: '', date_from: '', date_to: '' };
    const box = h('div');
    let pTimer;
    const pg = makePager(() => load(true));
    async function load(keep) {
      if (!keep) pg.page = 0;
      try {
        const rows = pg.take(await api('/stock/movements' + qs({ ...f, ...pg.q() })));
        box.replaceChildren(table(
          [t('lbl.date'), t('lbl.code'), t('lbl.product'), t('lbl.warehouse'), t('lbl.docType'), t('lbl.docNo'),
           { label: t('lbl.in'), num: true }, { label: t('lbl.out'), num: true }, { label: t('lbl.balance'), num: true }, t('lbl.user')],
          rows.map((r) => h('tr', { class: r.txn_type === 'REVERSAL' ? 'rev' : '' },
            td(fmtDate(r.txn_date)), td(r.product_code), td(r.product_name), td(r.warehouse_code),
            td(hasKey('doc.' + r.doc_type) ? docTypeLabel(r.doc_type) : r.doc_type + (r.txn_type === 'REVERSAL' ? ' (' + t('status.REVERSED') + ')' : '')),
            td(r.doc_no || ''), td(r.qty_in ? fmtNum(r.qty_in) : '', true), td(r.qty_out ? fmtNum(r.qty_out) : '', true),
            td(fmtNum(r.balance_after), true), td(r.user_name)))));
      } catch (e) { fail(e); }
    }
    const pInput = h('input', { type: 'search', placeholder: t('lbl.searchProduct'), list: 'mv-products',
      oninput: (e) => {
        clearTimeout(pTimer);
        pTimer = setTimeout(async () => {
          const text = e.target.value.trim();
          if (!text) { f.product_id = ''; load(); return; }
          try {
            const found = await api('/products' + qs({ q: text.split(' — ')[0], limit: 15, active_only: false }));
            document.getElementById('mv-products').replaceChildren(...found.map((p) => h('option', { value: `${p.product_code} — ${p.name}` })));
            const hit = found.find((p) => `${p.product_code} — ${p.name}` === text || p.product_code === text);
            if (hit) { f.product_id = hit.product_id; load(); }
          } catch (_) { /* ignore */ }
        }, 300);
      } });
    view(h('h2', {}, t('nav.movements')),
      h('div', { class: 'filters' },
        field(t('lbl.product'), h('div', {}, pInput, h('datalist', { id: 'mv-products' }))),
        field(t('lbl.warehouse'), select(whOptions(S.whs, true), '', { onchange: (e) => { f.warehouse_id = e.target.value; load(); } })),
        field(t('lbl.docType'), select([['', t('lbl.all')], ...DOC_TYPES.map((x) => [x, docTypeLabel(x)]), ['ADJUST_IN', t('doc.ADJUST_IN')], ['ADJUST_OUT', t('doc.ADJUST_OUT')], ['REVERSAL', t('doc.REVERSAL')]], '', { onchange: (e) => { f.doc_type = e.target.value; load(); } })),
        field(t('lbl.dateFrom'), h('input', { type: 'date', onchange: (e) => { f.date_from = e.target.value; load(); } })),
        field(t('lbl.dateTo'), h('input', { type: 'date', onchange: (e) => { f.date_to = e.target.value; load(); } }))),
      box, pg.el);
    load();
  }

  // ---------------------------------------------------------------- products
  async function pageProducts() {
    const f = { q: '', active_only: true };
    const box = h('div');
    const pg = makePager(() => load(true));
    async function load(keep) {
      if (!keep) pg.page = 0;
      try {
        const rows = pg.take(await api('/products' + qs({ ...f, ...pg.q(), active_only: f.active_only ? 'true' : 'false' })));
        box.replaceChildren(table(
          [t('lbl.code'), t('lbl.product'), t('lbl.barcode'), t('lbl.category'), t('lbl.conversions'), { label: t('lbl.reorder'), num: true }, t('lbl.status'), ''],
          rows.map((p) => h('tr', {},
            td(p.product_code), td(p.name), td(p.barcode || ''), td(p.category_name || ''),
            td(p.conversions.map((c) => `1 ${c.unit_code} = ${c.factor_to_base} PC`).join(', ')),
            td(fmtNum(p.reorder_level), true), td(p.is_active ? t('status.ACTIVE') : t('status.INACTIVE')),
            h('td', {}, canMaster() ? h('button', { class: 'link', onclick: () => productForm(p) }, t('btn.edit')) : null)))));
      } catch (e) { fail(e); }
    }
    view(h('h2', {}, t('nav.products')),
      h('div', { class: 'actions' }, canMaster() ? h('button', { class: 'primary', onclick: () => productForm(null) }, icon('plus'), t('btn.new')) : null),
      h('div', { class: 'filters' },
        field(t('btn.search'), h('input', { type: 'search', oninput: debounce((e) => { f.q = e.target.value; load(); }) })),
        h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: true, onchange: (e) => { f.active_only = e.target.checked; load(); } }), t('lbl.activeOnly'))),
      box, pg.el, h('div', { id: 'dialog-slot' }));
    load();
  }

  /** Category select with an inline "+ new category" box, so a category can be added without leaving the form. */
  function categoryPicker(v) {
    const box = h('div', { class: 'catpick' });
    const draw = (adding) => {
      const opts = S.categories.filter((c) => c.is_active || String(c.category_id) === String(v.category_id));
      const sel = select([['', '—'], ...opts.map((c) => [c.category_id, c.name])], v.category_id, { onchange: (e) => { v.category_id = e.target.value; } });
      if (!adding) {
        box.replaceChildren(sel, h('button', { type: 'button', class: 'small', onclick: () => draw(true) }, icon('plus'), t('cat.new')));
        return;
      }
      const inp = h('input', { type: 'text', maxlength: 150, placeholder: t('cat.namePh'), 'aria-label': t('cat.namePh'),
        onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); draw(false); } } });
      async function save() {
        const name = inp.value.trim();
        if (!name) { inp.focus(); return; }
        try {
          const c = await api('/categories', { method: 'POST', body: { name } });
          S.categories = await api('/categories');
          v.category_id = String(c.category_id);
          toast(t('cat.added', { name: c.name }));
          draw(false);
        } catch (e) { fail(e); }
      }
      box.replaceChildren(h('div', { class: 'catnew' }, inp,
        h('button', { type: 'button', class: 'primary small', onclick: save }, t('btn.save')),
        h('button', { type: 'button', class: 'small', onclick: () => draw(false) }, t('btn.cancel'))));
      inp.focus();
    };
    draw(false);
    return box;
  }

  function productForm(p) {
    const convs = ['CT', 'PK'].map((code) => {
      const ex = p && p.conversions.find((c) => c.unit_code === code);
      return { code, factor: ex ? ex.factor_to_base : '', barcode: ex && ex.barcode ? ex.barcode : '' };
    });
    const v = { product_code: p ? p.product_code : '', name: p ? p.name : '', barcode: p && p.barcode ? p.barcode : '',
      category_id: p && p.category_id ? p.category_id : '', reorder_level: p ? p.reorder_level : 0, is_active: p ? p.is_active : true };
    const dlg = h('dialog', {});
    const close = () => { dlg.close(); dlg.remove(); };
    dlg.append(h('form', { method: 'dialog', class: 'form', onsubmit: async (e) => {
      e.preventDefault();
      const body = { ...v, category_id: v.category_id ? Number(v.category_id) : null, reorder_level: Number(v.reorder_level) || 0,
        barcode: v.barcode || null,
        conversions: convs.filter((c) => Number(c.factor) > 0).map((c) => ({ unit_code: c.code, factor_to_base: Number(c.factor), barcode: c.barcode || null })) };
      try {
        await api(p ? '/products/' + p.product_id : '/products', { method: p ? 'PUT' : 'POST', body });
        toast(t('msg.saved')); close(); pageProducts();
      } catch (err) { fail(err); }
    } },
    h('h3', {}, p ? t('btn.edit') : t('btn.new')),
    field(t('lbl.code'), h('input', { required: true, maxlength: 50, value: v.product_code, oninput: (e) => { v.product_code = e.target.value; } })),
    field(t('lbl.product'), h('input', { required: true, maxlength: 255, value: v.name, oninput: (e) => { v.name = e.target.value; } })),
    field(t('lbl.barcode') + ' (PC)', h('input', { value: v.barcode, oninput: (e) => { v.barcode = e.target.value; } })),
    field(t('lbl.category'), categoryPicker(v)),
    field(t('lbl.reorder') + ' (PC)', h('input', { type: 'number', min: '0', value: v.reorder_level, oninput: (e) => { v.reorder_level = e.target.value; } })),
    h('fieldset', {}, h('legend', {}, t('lbl.conversions') + ' (1 unit = N PC)'),
      convs.map((c) => h('div', { class: 'conv-row' }, h('b', {}, c.code),
        h('input', { type: 'number', min: '1', step: '1', placeholder: 'N PC', value: c.factor, oninput: (e) => { c.factor = e.target.value; } }),
        h('input', { placeholder: t('lbl.barcode'), value: c.barcode, oninput: (e) => { c.barcode = e.target.value; } })))),
    p ? h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: v.is_active, onchange: (e) => { v.is_active = e.target.checked; } }), t('status.ACTIVE')) : null,
    h('p', { class: 'hint' }, t('msg.conversionHint')),
    h('div', { class: 'actions' }, h('button', { type: 'button', onclick: close }, t('btn.cancel')), h('button', { class: 'primary', type: 'submit' }, t('btn.save')))));
    document.body.append(dlg);
    dlg.showModal();
  }

  // ---------------------------------------------------------------- masters (warehouses / customers / suppliers)
  async function pageMasters(tab = 'warehouses') {
    const tabs = h('div', { class: 'tabs' }, ['warehouses', ...(canMaster() ? ['categories'] : []), 'customers', 'suppliers'].map((x) =>
      h('a', { href: '#/masters/' + x, class: x === tab ? 'active' : '' }, t('nav.' + x))));
    const box = h('div');
    view(h('h2', {}, t('nav.masters')), tabs, box);
    try {
      if (tab === 'warehouses') await mastersWarehouses(box);
      else if (tab === 'categories') await mastersCategories(box);
      else await mastersParties(box, tab);
    } catch (e) { fail(e); }
  }

  async function mastersWarehouses(box) {
    const rows = await api('/warehouses');
    const v = { warehouse_code: '', name: '' };
    box.append(
      table([t('lbl.code'), t('lbl.name'), t('lbl.status'), ''], rows.map((w) => h('tr', {}, td(w.warehouse_code), td(w.name),
        td(w.is_active ? t('status.ACTIVE') : t('status.INACTIVE')),
        h('td', {}, isAdmin() ? h('button', { class: 'link', onclick: async () => {
          const name = prompt(t('lbl.name'), w.name);
          if (name === null || !name.trim()) return;
          try { await api('/warehouses/' + w.warehouse_id, { method: 'PUT', body: { warehouse_code: w.warehouse_code, name: name.trim(), is_active: w.is_active } }); await loadRef(); pageMasters('warehouses'); } catch (e) { fail(e); }
        } }, t('btn.edit')) : null)))),
      isAdmin() ? h('form', { class: 'card form', onsubmit: async (e) => {
        e.preventDefault();
        try { await api('/warehouses', { method: 'POST', body: v }); toast(t('msg.saved')); await loadRef(); pageMasters('warehouses'); } catch (err) { fail(err); }
      } },
      h('h3', {}, t('btn.new') + ' ' + t('nav.warehouses')),
      field(t('lbl.code'), h('input', { required: true, maxlength: 20, oninput: (e) => { v.warehouse_code = e.target.value; } })),
      field(t('lbl.name'), h('input', { required: true, oninput: (e) => { v.name = e.target.value; } })),
      h('button', { class: 'primary', type: 'submit' }, t('btn.save'))) : null);
  }

  async function mastersCategories(box) {
    const rows = await api('/categories');
    S.categories = rows;
    const save = async (c, body) => {
      try { await api('/categories/' + c.category_id, { method: 'PUT', body }); toast(t('msg.saved')); pageMasters('categories'); } catch (e) { fail(e); }
    };
    let name = '';
    box.append(
      h('form', { class: 'card form', onsubmit: async (e) => {
        e.preventDefault();
        try { const c = await api('/categories', { method: 'POST', body: { name } }); toast(t('cat.added', { name: c.name })); pageMasters('categories'); } catch (err) { fail(err); }
      } },
      h('h3', {}, t('btn.new') + ' ' + t('nav.categories')),
      field(t('lbl.name'), h('input', { required: true, maxlength: 150, placeholder: t('cat.namePh'), oninput: (e) => { name = e.target.value; } })),
      h('button', { class: 'primary', type: 'submit' }, icon('plus'), t('btn.save'))),
      table([t('lbl.name'), { label: t('cat.products'), num: true }, t('lbl.status'), ''], rows.map((c) => h('tr', {},
        td(c.name), td(fmtNum(c.product_count), true), td(statusBadge(c.is_active ? 'ACTIVE' : 'INACTIVE')),
        h('td', { class: 'num' },
          h('button', { class: 'link', type: 'button', onclick: () => {
            const n = prompt(t('lbl.name'), c.name);
            if (n !== null && n.trim()) save(c, { name: n.trim(), is_active: c.is_active });
          } }, t('btn.edit')), ' · ',
          h('button', { class: 'link', type: 'button', onclick: () => save(c, { name: c.name, is_active: !c.is_active }) },
            c.is_active ? t('cat.disable') : t('cat.enable')))))));
  }

  async function mastersParties(box, kind) {
    const rows = await api('/' + kind);
    const v = { code: '', name: '', phone: '' };
    box.append(
      table([t('lbl.code'), t('lbl.name'), t('lbl.phone'), t('lbl.status')], rows.map((r) => h('tr', {}, td(r.code), td(r.name), td(r.phone || ''), td(r.is_active ? t('status.ACTIVE') : t('status.INACTIVE'))))),
      h('form', { class: 'card form', onsubmit: async (e) => {
        e.preventDefault();
        try { await api('/' + kind, { method: 'POST', body: { ...v, phone: v.phone || null } }); toast(t('msg.saved')); pageMasters(kind); } catch (err) { fail(err); }
      } },
      h('h3', {}, t('btn.new') + ' ' + t('nav.' + kind)),
      field(t('lbl.code'), h('input', { required: true, maxlength: 30, oninput: (e) => { v.code = e.target.value; } })),
      field(t('lbl.name'), h('input', { required: true, oninput: (e) => { v.name = e.target.value; } })),
      field(t('lbl.phone'), h('input', { oninput: (e) => { v.phone = e.target.value; } })),
      h('button', { class: 'primary', type: 'submit' }, t('btn.save'))));
  }

  // ---------------------------------------------------------------- stock counts
  async function pageCounts() {
    const box = h('div');
    const v = { warehouse_id: myTransactWhs()[0] ? myTransactWhs()[0].warehouse_id : '', product: null, q: emptyQ() };
    const cntRow = h('tr');
    const cntTotal = h('b');
    const drawCnt = () => {
      cntRow.replaceChildren(...unitCells(v.product, v.q, () => { cntTotal.textContent = v.product ? fmtNum(qtyBase(v.product, v.q)) + ' PC' : ''; }, { step: '1' }));
      cntTotal.textContent = v.product ? fmtNum(qtyBase(v.product, v.q)) + ' PC' : '';
    };
    const canCount = ['ADMIN', 'MANAGER', 'STOREKEEPER'].includes(S.me.role_code) && myTransactWhs().length > 0;
    async function load() {
      try {
        const rows = await api('/counts');
        box.replaceChildren(table(
          [t('lbl.date'), t('lbl.docNo'), t('lbl.warehouse'), t('lbl.product'), { label: t('lbl.systemQty'), num: true }, { label: t('dep.colHeld'), num: true }, { label: t('lbl.countedQty'), num: true }, { label: t('lbl.diff'), num: true }, t('lbl.status'), ''],
          rows.map((c) => h('tr', {}, td(fmtDate(c.count_date)), td(c.count_no), td(c.warehouse_name), td(c.product_code + ' — ' + c.product_name),
            td(fmtNum(c.system_qty), true), td(c.deposit_qty ? fmtNum(c.deposit_qty) : '', true), td(fmtNum(c.counted_qty), true), td(fmtNum(c.counted_qty - (c.deposit_qty || 0) - c.system_qty), true),
            td(t('status.' + c.status)),
            h('td', {}, c.status === 'OPEN' && canCount ? h('button', { class: 'primary small', onclick: async () => {
              if (!confirm(t('msg.confirmAdjust'))) return;
              try { const r = await api(`/counts/${c.count_id}/apply`, { method: 'POST', body: {} }); toast(r.no_difference ? t('msg.noDiff') : t('msg.adjusted')); load(); } catch (e) { fail(e); }
            } }, t('btn.applyCount')) : null)))));
      } catch (e) { fail(e); }
    }
    const search = h('input', { list: 'cnt-products', placeholder: t('lbl.searchProduct'), autocomplete: 'off',
      oninput: debounce(async (e) => {
        try {
          const found = await api('/products' + qs({ q: e.target.value.split(' — ')[0], limit: 15 }));
          document.getElementById('cnt-products').replaceChildren(...found.map((p) => h('option', { value: `${p.product_code} — ${p.name}` })));
          const np = found.find((p) => `${p.product_code} — ${p.name}` === e.target.value || p.product_code === e.target.value) || null;
          if ((np && np.product_id) !== (v.product && v.product.product_id)) { v.product = np; v.q = emptyQ(); drawCnt(); }
        } catch (_) { /* ignore */ }
      }, 250) });
    drawCnt();
    view(h('h2', {}, t('nav.counts')),
      canCount ? h('form', { class: 'card form', onsubmit: async (e) => {
        e.preventDefault();
        if (!v.product) { toast(t('err.PRODUCT_NOT_FOUND'), 'err'); return; }
        try {
          const counted = qtyBase(v.product, v.q);
          if (!Number.isInteger(counted) || v.q.some((x) => numOr0(x) < 0) || !v.q.some((x) => x !== '')) { toast(t('err.BAD_QTY'), 'err'); return; }
          await api('/counts', { method: 'POST', body: { warehouse_id: Number(v.warehouse_id), product_id: v.product.product_id, counted_qty: counted } });
          v.q = emptyQ(); drawCnt();
          toast(t('msg.saved')); load();
        } catch (err) { fail(err); }
      } },
      h('h3', {}, t('btn.new')),
      field(t('lbl.warehouse'), select(whOptions(myTransactWhs()), v.warehouse_id, { onchange: (e) => { v.warehouse_id = e.target.value; } })),
      field(t('lbl.product'), h('div', {}, search, h('datalist', { id: 'cnt-products' }))),
      h('div', { class: 'wide' }, h('label', {}, t('lbl.countedQty')),
        h('div', { class: 'tablewrap' }, h('table', { class: 'lines cnt' }, h('thead', {}, h('tr', {}, unitHeaders())), h('tbody', {}, cntRow))),
        h('p', { class: 'hint' }, t('uq.total') + ': ', cntTotal)),
      h('p', { class: 'hint wide' }, t('dep.countHint')),
      h('button', { class: 'primary', type: 'submit' }, t('btn.save'))) : null,
      box);
    load();
  }

  // ---------------------------------------------------------------- users (admin)
  async function pageUsers() {
    if (!isAdmin()) { location.hash = '#/stock'; return; }
    const box = h('div');
    const roles = ['ADMIN', 'MANAGER', 'STOREKEEPER', 'SALES', 'VIEWER'];
    async function load() {
      try {
        const rows = await api('/users');
        box.replaceChildren(table([t('lbl.username'), t('lbl.name'), t('lbl.role'), t('lbl.warehouse'), t('lbl.status'), ''],
          rows.map((u) => h('tr', {}, td(u.username), td(u.full_name), td(u.role_code),
            td(u.warehouses.map((a) => { const w = S.whs.find((x) => x.warehouse_id === a.warehouse_id); return (w ? w.warehouse_code : a.warehouse_id) + (a.can_transact ? '✎' : '👁'); }).join(' ')),
            td(u.is_active ? t('status.ACTIVE') : t('status.INACTIVE')),
            h('td', {}, h('button', { class: 'link', onclick: () => userForm(u) }, t('btn.edit')))))));
      } catch (e) { fail(e); }
    }
    function userForm(u) {
      const v = { username: u ? u.username : '', password: '', full_name: u ? u.full_name : '', role_code: u ? u.role_code : 'STOREKEEPER', is_active: u ? u.is_active : true };
      const access = new Map(S.whs.map((w) => {
        const a = u && u.warehouses.find((x) => x.warehouse_id === w.warehouse_id);
        return [w.warehouse_id, { can_view: !!a && a.can_view, can_transact: !!a && a.can_transact, can_approve: !!a && a.can_approve }];
      }));
      const dlg = h('dialog', {});
      const close = () => { dlg.close(); dlg.remove(); };
      dlg.append(h('form', { class: 'form', method: 'dialog', onsubmit: async (e) => {
        e.preventDefault();
        const warehouses = [...access.entries()].filter(([, a]) => a.can_view || a.can_transact).map(([id, a]) => ({ warehouse_id: id, can_view: a.can_view || a.can_transact, can_transact: a.can_transact, can_approve: a.can_approve }));
        try {
          if (u) await api('/users/' + u.user_id, { method: 'PUT', body: { full_name: v.full_name, role_code: v.role_code, is_active: v.is_active, password: v.password || null, warehouses } });
          else await api('/users', { method: 'POST', body: { username: v.username, password: v.password, full_name: v.full_name, role_code: v.role_code, warehouses } });
          toast(t('msg.saved')); close(); load();
        } catch (err) { fail(err); }
      } },
      h('h3', {}, u ? t('btn.edit') + ' ' + u.username : t('btn.new')),
      u ? null : field(t('lbl.username'), h('input', { required: true, minlength: 3, oninput: (e) => { v.username = e.target.value; } })),
      field(t('lbl.password') + (u ? ' (' + t('lbl.leaveBlank') + ')' : ''), h('input', { type: 'password', minlength: 8, required: !u, autocomplete: 'new-password', oninput: (e) => { v.password = e.target.value; } })),
      field(t('lbl.name'), h('input', { required: true, value: v.full_name, oninput: (e) => { v.full_name = e.target.value; } })),
      field(t('lbl.role'), select(roles.map((r) => [r, r]), v.role_code, { onchange: (e) => { v.role_code = e.target.value; } })),
      u ? h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: v.is_active, onchange: (e) => { v.is_active = e.target.checked; } }), t('status.ACTIVE')) : null,
      h('fieldset', {}, h('legend', {}, t('lbl.warehouseAccess')),
        S.whs.map((w) => { const a = access.get(w.warehouse_id); return h('div', { class: 'conv-row' }, h('b', {}, w.warehouse_code),
          h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: a.can_view, onchange: (e) => { a.can_view = e.target.checked; } }), t('lbl.canView')),
          h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: a.can_transact, onchange: (e) => { a.can_transact = e.target.checked; } }), t('lbl.canTransact'))); })),
      h('div', { class: 'actions' }, h('button', { type: 'button', onclick: close }, t('btn.cancel')), h('button', { class: 'primary', type: 'submit' }, t('btn.save')))));
      document.body.append(dlg); dlg.showModal();
    }
    view(h('h2', {}, t('nav.users')), h('div', { class: 'actions' }, h('button', { class: 'primary', onclick: () => userForm(null) }, icon('plus'), t('btn.new'))), box);
    load();
  }


  // ---------------------------------------------------------------- Excel import
  const IMPORT_FIELDS = ['doc_no', 'doc_date', 'product_code', 'barcode', 'qty', 'unit_code', 'customer_code', 'customer_name', 'salesman'];
  const importTypes = () => (S.me.role_code === 'SALES' ? ['SALE'] : ['SALE', 'WITHDRAWAL', 'RECEIVING']);
  const defaultImportOpt = (type) => ({
    match_by: 'PRODUCT_CODE', default_unit: 'PC', default_doc_no: '', default_date: '', doc_type_rules: [],
    negative_as_return: type === 'SALE', warehouse_by: null, warehouse_map: {}, create_missing_customers: true,
    duplicate_lines: 'ERROR', date_order: 'DMY',
  });

  async function downloadErrors(batchId, includeDup) {
    const res = await fetch(`/api/imports/${batchId}/errors.xlsx${includeDup ? '?include_duplicates=true' : ''}`,
      { headers: { Authorization: 'Bearer ' + S.token } });
    if (!res.ok) { toast(t('err.UNKNOWN'), 'err'); return; }
    const url = URL.createObjectURL(await res.blob());
    const a = h('a', { href: url, download: `import-${batchId}-errors.xlsx` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  async function pageImport() {
    if (!canImport()) { location.hash = '#/stock'; return; }
    const V = viewFor();
    const templates = await api('/import-templates');
    const whs = myTransactWhs();
    const st = {
      step: 1, wh: whs[0] && whs[0].warehouse_id, type: importTypes()[0], file: null,
      batch: null, fileName: '', already: null, sheets: [], sheet: null, headerRow: 1, preview: null,
      mapping: {}, opt: defaultImportOpt(importTypes()[0]), templateId: '', saveAs: '',
      summary: null, filter: '', rows: [], skipErrors: false, result: null, busy: false,
    };
    const root = h('div');
    V(h('h2', {}, t('nav.import')), root);

    const stepBar = () => h('div', { class: 'tabs' }, ['upload', 'setup', 'preview', 'done'].map((k, i) =>
      h('a', { class: st.step === i + 1 ? 'active' : '' }, `${i + 1}. ${t('imp.s.' + k)}`)));

    function applyTemplate(tpl) {
      st.templateId = tpl ? tpl.template_id : '';
      if (!tpl) return;
      st.mapping = { ...tpl.mapping };
      st.opt = { ...defaultImportOpt(st.type), ...(tpl.settings || {}) };
      st.headerRow = tpl.header_row || 1;
      if (tpl.sheet_name && st.sheets.includes(tpl.sheet_name)) st.sheet = tpl.sheet_name;
    }

    async function loadPreview(resetSuggest) {
      try {
        const q = qs({ sheet: st.sheet, header_row: st.headerRow });
        const r = await api(`/imports/${st.batch}/preview${q}`);
        st.sheet = r.sheet; st.preview = r.preview; st.sheets = r.sheets;
        if (resetSuggest) { st.headerRow = r.guessed_header_row; st.mapping = { ...r.suggested_mapping };
          if (r.guessed_header_row !== r.preview.header_row) { return loadPreview(false); } }
        render();
      } catch (e) { fail(e); }
    }

    // ---------------- step 1
    function step1() {
      const file = h('input', { type: 'file', accept: '.xlsx,.xls,.csv,.tsv,.txt', onchange: (e) => { st.file = e.target.files[0] || null; } });
      return h('div', { class: 'card form' },
        field(t('lbl.warehouse'), select(whOptions(whs), st.wh, { onchange: (e) => { st.wh = Number(e.target.value); } })),
        field(t('lbl.docType'), select(importTypes().map((x) => [x, docTypeLabel(x)]), st.type, { onchange: (e) => { st.type = e.target.value; st.opt = defaultImportOpt(st.type); } })),
        field(t('imp.file'), file, 'wide'),
        h('p', { class: 'hint wide' }, t('imp.fileHint')),
        h('div', { class: 'actions' }, h('button', { class: 'primary', disabled: st.busy, onclick: async () => {
          if (!st.file) { toast(t('imp.chooseFile'), 'err'); return; }
          const form = new FormData();
          form.append('file', st.file); form.append('warehouse_id', st.wh); form.append('doc_type', st.type);
          st.busy = true;
          try {
            const r = await api('/imports', { method: 'POST', form });
            st.batch = r.batch_id; st.fileName = r.file_name; st.already = r.already_imported_batch_id;
            st.sheets = r.sheets; st.sheet = r.sheet; st.preview = r.preview; st.headerRow = r.guessed_header_row;
            st.mapping = { ...r.suggested_mapping };
            const tpl = templates.find((x) => x.template_id === r.suggested_template_id);
            if (tpl) { applyTemplate(tpl); if (tpl.header_row !== r.preview.header_row || (tpl.sheet_name && tpl.sheet_name !== r.sheet)) await loadPreview(false); }
            st.busy = false; st.step = 2; render();
          } catch (e) { fail(e); } finally { st.busy = false; }
        } }, t('btn.upload'))));
    }

    // ---------------- step 2
    function previewTable() {
      const p = st.preview;
      if (!p) return null;
      const width = Math.min(Math.max(...p.rows.map((r) => r.length), 1), 12);
      return h('div', { class: 'tablewrap' }, h('table', { class: 'mini' }, h('tbody', {}, p.rows.map((r, i) =>
        h('tr', { class: i + 1 === st.headerRow ? 'hdr' : '', onclick: () => { st.headerRow = i + 1; loadPreview(false); } },
          h('td', { class: 'num rn' }, i + 1),
          Array.from({ length: width }, (_, c) => h('td', {}, r[c] == null ? '' : String(r[c]).slice(0, 40))))))));
    }

    function mappingGrid() {
      const headers = (st.preview && st.preview.headers) || [];
      const needsProduct = st.opt.match_by === 'BARCODE' ? ['barcode'] : st.opt.match_by === 'BOTH' ? ['product_code', 'barcode'] : ['product_code'];
      const required = new Set(['qty', needsProduct[0], ...(st.opt.default_doc_no ? [] : ['doc_no'])]);
      return h('div', { class: 'maprows' }, IMPORT_FIELDS.map((f) => h('label', { class: 'field' },
        h('span', {}, t('imp.f.' + f) + (required.has(f) ? ' *' : '')),
        select([['', t('imp.notMapped')], ...headers.map((x) => [x, x])], st.mapping[f] || '',
          { onchange: (e) => { if (e.target.value) st.mapping[f] = e.target.value; else delete st.mapping[f]; } }))));
    }

    function optionsPanel() {
      const o = st.opt;
      const rule = (r, i) => h('div', { class: 'conv-row' },
        h('input', { placeholder: t('imp.opt.prefix'), value: r.prefix, oninput: (e) => { r.prefix = e.target.value; } }),
        select(['CUSTOMER_RETURN', 'SALE', 'WITHDRAWAL', 'RECEIVING', 'SUPPLIER_RETURN'].map((x) => [x, docTypeLabel(x)]), r.doc_type, { onchange: (e) => { r.doc_type = e.target.value; } }),
        h('button', { class: 'link', type: 'button', onclick: () => { o.doc_type_rules.splice(i, 1); render(); } }, '✕'));
      return h('div', { class: 'card form' },
        field(t('imp.opt.matchBy'), select([['PRODUCT_CODE', t('imp.opt.matchCode')], ['BARCODE', t('imp.opt.matchBarcode')], ['BOTH', t('imp.opt.matchBoth')]], o.match_by, { onchange: (e) => { o.match_by = e.target.value; render(); } })),
        field(t('imp.opt.defaultUnit'), select(S.units.map((u) => [u.unit_code, u.unit_code]), o.default_unit, { onchange: (e) => { o.default_unit = e.target.value; } })),
        field(t('imp.opt.defaultDocNo'), h('input', { value: o.default_doc_no || '', maxlength: 50, oninput: (e) => { o.default_doc_no = e.target.value; }, onchange: () => render() })),
        field(t('imp.opt.defaultDate'), h('input', { type: 'date', value: o.default_date || '', onchange: (e) => { o.default_date = e.target.value; } })),
        field(t('imp.opt.dupLines'), select([['ERROR', t('imp.opt.dupError')], ['KEEP', t('imp.opt.dupKeep')]], o.duplicate_lines, { onchange: (e) => { o.duplicate_lines = e.target.value; } })),
        field(t('imp.opt.dateOrder'), select([['DMY', 'DD/MM/YYYY'], ['MDY', 'MM/DD/YYYY']], o.date_order, { onchange: (e) => { o.date_order = e.target.value; } })),
        st.type === 'SALE' ? h('label', { class: 'check wide' }, h('input', { type: 'checkbox', checked: o.negative_as_return, onchange: (e) => { o.negative_as_return = e.target.checked; } }), t('imp.opt.negReturn')) : null,
        st.type === 'SALE' ? h('label', { class: 'check wide' }, h('input', { type: 'checkbox', checked: o.create_missing_customers, onchange: (e) => { o.create_missing_customers = e.target.checked; } }), t('imp.opt.createCustomers')) : null,
        st.type === 'SALE' ? h('fieldset', { class: 'wide' }, h('legend', {}, t('imp.opt.rules')),
          o.doc_type_rules.map(rule),
          h('button', { type: 'button', onclick: () => { o.doc_type_rules.push({ prefix: '', doc_type: 'CUSTOMER_RETURN' }); render(); } }, icon('plus'), t('imp.opt.addRule')),
          h('p', { class: 'hint' }, t('imp.opt.rulesHint'))) : null);
    }

    function step2() {
      const tplSel = select([['', t('imp.templateNone')], ...templates.filter((x) => x.doc_type === st.type).map((x) => [x.template_id, x.name])], st.templateId,
        { onchange: async (e) => {
          const tpl = templates.find((x) => String(x.template_id) === e.target.value);
          if (tpl) { applyTemplate(tpl); await loadPreview(false); } else { st.templateId = ''; render(); }
        } });
      return h('div', {},
        h('p', {}, h('b', {}, st.fileName), ` · ${(whs.find((w) => w.warehouse_id === st.wh) || {}).warehouse_code || ''} · ${docTypeLabel(st.type)}`),
        st.already ? h('p', { class: 'banner warn' }, t('imp.alreadyImported', { id: st.already })) : null,
        h('div', { class: 'filters' },
          st.sheets.length > 1 ? field(t('imp.sheet'), select(st.sheets.map((x) => [x, x]), st.sheet, { onchange: async (e) => { st.sheet = e.target.value; await loadPreview(true); } })) : null,
          field(t('imp.headerRow'), h('input', { type: 'number', min: '1', value: st.headerRow, onchange: (e) => { st.headerRow = Math.max(1, Number(e.target.value) || 1); loadPreview(false); } })),
          field(t('imp.template'), tplSel)),
        h('p', { class: 'hint' }, t('imp.previewHint')), previewTable(),
        h('h3', {}, t('imp.mapping')), h('p', { class: 'hint' }, t('imp.mapHint')), mappingGrid(),
        h('h3', {}, t('imp.options')), optionsPanel(),
        h('div', { class: 'card form' }, field(t('imp.saveTemplate'), h('input', { value: st.saveAs, maxlength: 150, oninput: (e) => { st.saveAs = e.target.value; } }), 'wide')),
        h('div', { class: 'actions' },
          h('button', { onclick: async () => { try { await api(`/imports/${st.batch}/cancel`, { method: 'POST' }); } catch (_) { /* ignore */ } location.hash = '#/imports'; } }, t('btn.cancelImport')),
          h('span', { class: 'spacer' }),
          h('button', { class: 'primary', disabled: st.busy, onclick: () => validate() }, t('btn.validate'))));
    }

    async function validate() {
      const opt = { ...st.opt, doc_type_rules: st.opt.doc_type_rules.filter((r) => r.prefix.trim()) };
      st.busy = true;
      try {
        const r = await api(`/imports/${st.batch}/process`, { method: 'POST', body: {
          sheet: st.sheet, header_row: st.headerRow, mapping: st.mapping, options: opt,
          template_id: st.templateId ? Number(st.templateId) : null, save_template_as: st.saveAs.trim() || null } });
        st.summary = r.summary; st.saveAs = '';
        if (r.template_id) { const fresh = await api('/import-templates'); templates.splice(0, templates.length, ...fresh); st.templateId = String(r.template_id); }
        st.filter = r.summary.rows.error + r.summary.rows.blocked > 0 ? 'ERROR' : '';
        await loadRows();
        st.busy = false; st.step = 3; render();
      } catch (e) { fail(e); } finally { st.busy = false; }
    }

    async function loadRows() {
      st.rows = await api(`/imports/${st.batch}/rows${qs({ status: st.filter, limit: 200 })}`);
    }

    // ---------------- step 3
    const card = (label, value, cls) => h('div', { class: 'stat ' + (cls || '') }, h('b', {}, fmtNum(value)), h('span', {}, label));

    function step3() {
      const s = st.summary;
      const bad = s.rows.error + s.rows.blocked;
      const salesmen = Object.keys(s.salesmen || {});
      const mapPanel = st.mapping.salesman && salesmen.length > 1 ? h('div', { class: 'card' },
        h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: st.opt.warehouse_by === 'salesman', onchange: (e) => { st.opt.warehouse_by = e.target.checked ? 'salesman' : null; render(); } }), t('imp.opt.warehouseBySalesman')),
        st.opt.warehouse_by === 'salesman' ? h('div', {},
          h('p', { class: 'hint' }, t('imp.opt.salesmanMapHint')),
          h('div', { class: 'maprows' }, salesmen.map((sm) => h('label', { class: 'field' }, h('span', {}, `${sm} (${fmtNum(s.salesmen[sm])})`),
            select([['', t('imp.opt.defaultWh')], ...whOptions(whs)], st.opt.warehouse_map[sm] || '', { onchange: (e) => { if (e.target.value) st.opt.warehouse_map[sm] = Number(e.target.value); else delete st.opt.warehouse_map[sm]; } })))),
          h('div', { class: 'actions' }, h('button', { onclick: () => validate() }, t('btn.revalidate')))) : null) : null;
      const shortList = s.shortage_count ? h('p', { class: 'banner warn' }, `${t('imp.sum.shortages')}: ${fmtNum(s.shortage_count)}`) : null;
      return h('div', {},
        h('p', { class: 'banner ok' }, t('imp.sum.noStockMove')),
        st.already ? h('p', { class: 'banner warn' }, t('imp.alreadyImported', { id: st.already })) : null,
        h('div', { class: 'stats' },
          card(t('imp.sum.rows'), s.rows.total), card(t('imp.sum.valid'), s.rows.valid, 'good'),
          card(t('imp.sum.error'), s.rows.error, s.rows.error ? 'bad' : ''), card(t('imp.sum.blocked'), s.rows.blocked, s.rows.blocked ? 'bad' : ''),
          card(t('imp.sum.duplicate'), s.rows.duplicate, s.rows.duplicate ? 'warn' : '')),
        h('div', { class: 'stats' },
          card(t('imp.sum.docsReady'), s.docs.ready, 'good'), card(t('imp.sum.docsBlocked'), s.docs.blocked, s.docs.blocked ? 'bad' : ''),
          card(t('imp.sum.docsDup'), s.docs.duplicate, s.docs.duplicate ? 'warn' : ''),
          card(t('imp.sum.qtyOut'), s.qty_out), card(t('imp.sum.qtyIn'), s.qty_in), card(t('imp.sum.newCustomers'), s.new_customers)),
        Object.keys(s.docs.by_type || {}).length ? h('p', {}, Object.entries(s.docs.by_type).map(([k, v]) => `${docTypeLabel(k)}: ${fmtNum(v)}`).join(' · ')) : null,
        shortList, mapPanel,
        Object.keys(s.error_codes || {}).length ? h('p', {}, Object.entries(s.error_codes).map(([k, v]) => `${t('imp.err.' + k)}: ${fmtNum(v)}`).join(' · ')) : null,
        h('div', { class: 'filters' },
          field(t('lbl.status'), select([['', t('lbl.all')], ...['ERROR', 'BLOCKED', 'DUPLICATE', 'VALID'].map((x) => [x, t('imp.st.' + x)])], st.filter,
            { onchange: async (e) => { st.filter = e.target.value; await loadRows(); render(); } })),
          bad ? h('button', { onclick: () => downloadErrors(st.batch, false) }, t('btn.exportErrors')) : null),
        table([t('imp.col.row'), t('lbl.status'), t('imp.col.error'), t('lbl.docNo'), t('lbl.date'), t('lbl.docType'), t('lbl.warehouse'), t('lbl.product'), { label: t('lbl.qty'), num: true }, t('lbl.unit')],
          st.rows.map((r) => h('tr', { class: r.status === 'ERROR' ? 'low' : (r.status === 'BLOCKED' ? 'rev' : '') },
            td(r.row_no), td(t('imp.st.' + r.status)),
            td(r.error_code ? (t('imp.err.' + r.error_code) + (r.error_detail && r.error_code !== 'DOC_HAS_ERRORS' ? ' — ' + r.error_detail : '')) : ''),
            td(r.doc_no || ''), td(fmtDate(r.doc_date)), td(r.doc_type ? docTypeLabel(r.doc_type) : ''), td(r.warehouse_code || ''),
            td(r.product_ref || ''), td(r.qty == null ? '' : fmtNum(r.qty), true), td(r.unit_code || '')))),
        s.rows.total > 200 ? h('p', { class: 'hint' }, t('imp.rowsLimit')) : null,
        bad ? h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: st.skipErrors, onchange: (e) => { st.skipErrors = e.target.checked; render(); } }), t('imp.skipErrors')) : null,
        h('div', { class: 'actions' },
          h('button', { onclick: () => { st.step = 2; render(); } }, '← ' + t('btn.back')),
          h('span', { class: 'spacer' }),
          h('button', { class: 'primary', disabled: st.busy || s.docs.ready === 0 || (bad > 0 && !st.skipErrors), onclick: async () => {
            if (!confirm(t('imp.confirmQ', { n: s.docs.ready }))) return;
            st.busy = true;
            try {
              const r = await api(`/imports/${st.batch}/confirm`, { method: 'POST', body: { skip_errors: st.skipErrors }, headers: { 'Idempotency-Key': `imp-${st.batch}-${st.skipErrors ? 's' : 'a'}` } });
              st.result = r.result; st.busy = false; st.step = 4; render();
            } catch (e) { fail(e); } finally { st.busy = false; }
          } }, t('btn.confirmImport'))));
    }

    // ---------------- step 4
    function step4() {
      const r = st.result || {};
      return h('div', { class: 'card' },
        h('p', { class: 'banner ok' }, t('imp.done')),
        h('div', { class: 'stats' }, card(t('imp.postedDocs'), r.documents_posted || 0, 'good'), card(t('imp.sum.rows'), r.rows_posted || 0),
          card(t('imp.rowsSkipped'), r.rows_skipped || 0, r.rows_skipped ? 'warn' : ''), card(t('imp.customersCreated'), r.customers_created || 0)),
        h('div', { class: 'actions' },
          h('a', { class: 'btn', href: '#/docs' }, t('nav.docs')), h('a', { class: 'btn', href: '#/stock' }, t('nav.stock')),
          h('a', { class: 'btn', href: '#/imports' }, t('nav.imports')), h('button', { onclick: () => pageImport() }, t('imp.another'))));
    }

    function render() {
      if (main() !== V.el) return;
      root.replaceChildren(stepBar(), [step1, step2, step3, step4][st.step - 1]());
    }
    render();
  }

  async function pageImports() {
    const V = viewFor();
    const rows = await api('/imports?limit=100');
    V(h('h2', {}, t('nav.imports')),
      h('div', { class: 'actions' }, h('a', { class: 'btn primary', href: '#/import' }, icon('plus'), t('nav.import'))),
      table([t('imp.col.id'), t('lbl.date'), t('imp.file'), t('lbl.docType'), t('lbl.warehouse'), t('lbl.status'), { label: t('imp.sum.rows'), num: true },
        { label: t('imp.sum.error'), num: true }, { label: t('imp.sum.duplicate'), num: true }, { label: t('imp.postedDocs'), num: true }, t('lbl.createdBy'), ''],
      rows.map((b) => h('tr', {}, td('#' + b.batch_id), td(fmtDateTime(b.uploaded_at)), td(b.file_name), td(docTypeLabel(b.doc_type)), td(b.warehouse_code),
        td(t('imp.bst.' + b.status)), td(fmtNum(b.total_rows), true), td(fmtNum(b.error_rows), true), td(fmtNum(b.duplicate_rows), true),
        td(b.documents_posted == null ? '' : fmtNum(b.documents_posted), true), td(b.uploaded_by_name),
        h('td', {}, b.error_rows > 0 && b.status !== 'CANCELLED' ? h('button', { class: 'link', onclick: () => downloadErrors(b.batch_id, false) }, t('btn.exportErrors')) : null)))));
  }


  // ---------------------------------------------------------------- transfers
  const TST = ['DRAFT', 'IN_TRANSIT', 'PARTIAL', 'COMPLETED', 'CANCELLED'];

  async function pageTransfers() {
    const V = viewFor();
    const f = { status: '' };
    const box = h('div');
    const pg = makePager(() => load(true));
    async function load(keep) {
      if (!keep) pg.page = 0;
      const rows = pg.take(await api('/transfers' + qs({ status: f.status, ...pg.q() })));
      box.replaceChildren(table([t('tf.no'), t('lbl.date'), t('tf.mode'), t('tf.from'), t('tf.to'), t('lbl.status'), { label: t('tf.sent'), num: true }, { label: t('tf.received'), num: true }],
        rows.map((r) => h('tr', { class: 'click', onclick: () => { location.hash = '#/transfer/' + r.transfer_id; } },
          td(r.transfer_no), td(fmtDate(r.transfer_date)), td(t('tf.mode.' + r.transfer_mode)), td(r.from_code), td(r.to_code),
          h('td', {}, statusBadge(r.status)), td(fmtNum(r.qty_sent), true), td(fmtNum(r.qty_received), true)))));
    }
    V(h('h2', {}, t('nav.transfers')),
      h('div', { class: 'filters' },
        field(t('lbl.status'), select([['', t('lbl.all')], ...TST.map((x) => [x, t('status.' + x)])], '', { onchange: (e) => { f.status = e.target.value; load().catch(fail); } })),
        canTransfer() ? h('a', { class: 'btn primary', href: '#/transfer-new' }, icon('plus'), t('tf.new')) : null),
      box, pg.el);
    await load();
  }

  async function pageNewTransfer() {
    if (!canTransfer()) { location.hash = '#/transfers'; return; }
    const V = viewFor();
    const from = myTransactWhs();
    const key = uuid();
    const blank = () => ({ product: null, q: emptyQ() });
    let lines = [blank()];
    const st = { mode: 'APPROVAL', from: from[0] && from[0].warehouse_id, to: '', date: today(), remark: '' };
    const body = h('tbody');
    const lpg = makePager(() => { renderLines(); body.closest('.lines-card').scrollIntoView({ block: 'nearest' }); });
    const bad = (i, msg) => { lpg.showIndex(i); renderLines(); toast(msg, 'err'); };
    const lineCount = h('span', { class: 'hint' });
    let avail = {};
    let busy = false;
    const refreshTotals = () => { lineCount.textContent = t('xl.count', { n: lines.filter((l) => l.product).length }); };
    function renderLines() {
      refreshTotals();
      lpg.total = lines.length; lpg.render();
      const off = lpg.start();
      body.replaceChildren(...lpg.slice(lines).map((ln, i) => {
        const idx = off + i;
        const dl = 'tdl' + idx;
        const search = h('input', { type: 'text', list: dl, class: 'psearch', placeholder: t('lbl.searchProduct'), autocomplete: 'off',
          value: ln.product ? `${ln.product.product_code} — ${ln.product.name}` : ln.text || '',
          oninput: debounce(async (e) => {
            ln.text = e.target.value;
            try {
              const found = await api('/products' + qs({ q: e.target.value.split(' — ')[0], limit: 15 }));
              ln.options = found;
              document.getElementById(dl).replaceChildren(...found.map((p) => h('option', { value: `${p.product_code} — ${p.name}` })));
            } catch (_) { /* ignore while typing */ }
          }, 250),
          onchange: async (e) => {
            const text = e.target.value.trim();
            let p = (ln.options || []).find((x) => `${x.product_code} — ${x.name}` === text);
            if (!p && text) { try { const r = await findByCode(text.split(' — ')[0]); if (r) p = r.product; } catch (err) { fail(err); } }
            if (!p) { ln.product = null; toast(t('err.PRODUCT_NOT_FOUND'), 'err'); renderLines(); return; }
            if (ln.product && ln.product.product_id === p.product_id) return;
            ln.product = p; ln.q = emptyQ(); renderLines();
            const inp = body.querySelectorAll('tr')[idx - off].querySelector('input.uq:not([disabled])');
            if (inp) inp.focus();
          } });
        const totalCell = h('td', { class: 'num tot' });
        const stateCell = h('td', {});
        const have = ln.product ? (avail[ln.product.product_id] || 0) : null;
        const update = () => {
          const b = ln.product ? qtyBase(ln.product, ln.q) : 0;
          totalCell.textContent = b ? fmtNum(b) : '';
          stateCell.replaceChildren(stockState(b, have) || '');
        };
        const row = h('tr', {}, td(idx + 1), h('td', { class: 'pcell' }, search, h('datalist', { id: dl })),
          unitCells(ln.product, ln.q, update, { step: '1' }), totalCell,
          h('td', { class: 'avail' }, availBadges(ln.product, have)), stateCell,
          h('td', {}, lines.length > 1 ? h('button', { class: 'link', type: 'button', onclick: () => { lines.splice(idx, 1); renderLines(); } }, '✕') : null));
        update();
        return row;
      }));
    }
    async function reloadAvail() { avail = await loadAvail(st.from); renderLines(); }
    async function submit() {
      if (busy) return;
      if (!st.to) { toast(t('tf.chooseTo'), 'err'); return; }
      const items = [];
      for (const [i, ln] of lines.entries()) {
        const where = t('lbl.line') + ' ' + (i + 1) + ': ';
        if (!ln.product) {
          if (lines.length > 1 && !ln.q.some((x) => numOr0(x))) continue;
          bad(i, where + t('err.PRODUCT_NOT_FOUND')); return;
        }
        const parts = qItems(ln.product, ln.q);
        const base = qtyBase(ln.product, ln.q);
        if (!parts.length || !(base > 0) || !Number.isInteger(base) || ln.q.some((x) => numOr0(x) < 0)) { bad(i, where + t('err.BAD_QTY')); return; }
        if (items.some((x) => x.product_id === ln.product.product_id)) { bad(i, where + t('err.DUP_PRODUCT')); return; }
        const one = parts.length === 1 && Number.isInteger(numOr0(parts[0].qty));
        items.push({ product_id: ln.product.product_id, unit_code: one ? parts[0].unit_code : 'PC', qty: one ? Number(parts[0].qty) : base });
      }
      if (!items.length) { toast(t('err.NO_ITEMS'), 'err'); return; }
      busy = true;
      try {
        const r = await api('/transfers', { method: 'POST', headers: { 'Idempotency-Key': key }, body: {
          mode: st.mode, from_warehouse_id: Number(st.from), to_warehouse_id: Number(st.to), transfer_date: st.date, remark: st.remark || null, items } });
        toast(t('tf.created')); location.hash = '#/transfer/' + r.transfer_id;
      } catch (e) { fail(e); } finally { busy = false; }
    }
    renderLines();
    reloadAvail();
    const toOpts = () => S.whs.filter((w) => w.warehouse_type === 'PHYSICAL' && w.is_active && String(w.warehouse_id) !== String(st.from));
    const toSel = select([['', '—'], ...whOptions(toOpts())], '', { onchange: (e) => { st.to = e.target.value; } });
    V(h('p', {}, h('a', { href: '#/transfers' }, '← ' + t('nav.transfers'))), h('h2', {}, t('tf.new')),
      h('div', { class: 'card form' },
        field(t('tf.mode'), select([['APPROVAL', t('tf.mode.APPROVAL')], ['INSTANT', t('tf.mode.INSTANT')]], st.mode, { onchange: (e) => { st.mode = e.target.value; } })),
        field(t('tf.from'), select(whOptions(from), st.from, { onchange: (e) => {
          st.from = e.target.value; toSel.replaceChildren(...[['', '—'], ...whOptions(toOpts())].map(([v, l]) => h('option', { value: v }, l))); st.to = ''; reloadAvail();
        } })),
        field(t('tf.to'), toSel),
        field(t('lbl.date'), h('input', { type: 'date', value: st.date, onchange: (e) => { st.date = e.target.value; } })),
        field(t('lbl.remark'), h('input', { value: '', oninput: (e) => { st.remark = e.target.value; } }), 'wide'),
        h('p', { class: 'hint wide' }, t('tf.modeHint'))),
      h('div', { class: 'card lines-card' },
        h('div', { class: 'lines-head' }, h('b', {}, icon('list'), t('uq.items')), lineCount),
        h('div', { class: 'tablewrap' }, h('table', { class: 'lines' }, h('thead', {}, h('tr', {}, h('th', {}, '#'), h('th', {}, t('lbl.product')), unitHeaders(),
          h('th', { class: 'num' }, t('uq.total')), h('th', {}, t('uq.availableFrom')), h('th', {}, t('lbl.status')), h('th', {}))), body), lpg.el)),
      h('div', { class: 'actions' }, h('button', { type: 'button', onclick: () => { lines.push(blank()); lpg.showIndex(lines.length - 1); renderLines(); const ins = body.querySelectorAll('input.psearch'); if (ins.length) ins[ins.length - 1].focus(); } }, icon('plus'), t('btn.addLine')),
        pickerButton(() => ({
          warehouseId: st.from, inStock: true, avail,
          have: new Set(lines.filter((l) => l.product).map((l) => l.product.product_id)),
          onAdd: (picked) => {
            const first = addPickedLines(lines, picked, blank);
            if (first < 0) return;
            lpg.showIndex(first); renderLines();
            toast(t('pk.added', { n: picked.length }));
            const row = body.querySelectorAll('tr')[first - lpg.start()];
            const inp = row && row.querySelector('input.uq:not([disabled])');
            if (inp) inp.focus();
          },
        })),
        h('span', { class: 'spacer' }), h('button', { class: 'primary', onclick: submit }, t('btn.save'))));
  }

  async function pageTransfer(id) {
    const V = viewFor();
    let d;
    try { d = await api('/transfers/' + id); } catch (e) { fail(e); if (main() === V.el) location.hash = '#/transfers'; return; }
    const canSrc = S.me.transact_warehouses.includes(d.from_warehouse_id);
    const canDst = S.me.transact_warehouses.includes(d.to_warehouse_id);
    const mgr = isManager();
    const reload = () => pageTransfer(id);
    const call = async (path, body, okMsg) => {
      try { await api(`/transfers/${id}/${path}`, { method: 'POST', body: body || {}, headers: { 'Idempotency-Key': `tf-${id}-${path}-${uuid()}` } }); toast(okMsg); reload(); } catch (e) { fail(e); }
    };
    const act = [];
    if (d.status === 'DRAFT' && mgr && canSrc) act.push(h('button', { class: 'primary', onclick: () => { if (confirm(t('tf.confirmShip'))) call('ship', null, t('tf.shipped')); } }, t('tf.ship')));
    const recv = {};   // item_id -> { p, q } (quantities per unit)
    const open = d.status === 'IN_TRANSIT' || d.status === 'PARTIAL';
    const prods = {};
    if (open) {
      await Promise.all([...new Set(d.items.map((i) => i.product_code))].map(async (code) => {
        try { const r = await findByCode(code); if (r) prods[code] = r.product; } catch (_) { /* ignore */ }
      }));
      for (const i of d.items) {
        const p = prods[i.product_code] || { conversions: [] };
        if (i.qty_outstanding_base > 0) recv[i.item_id] = { p, q: baseToQ(p, i.qty_outstanding_base) };
      }
    }
    if (open && canDst && ['ADMIN', 'MANAGER', 'STOREKEEPER'].includes(S.me.role_code)) {
      act.push(h('button', { class: 'primary', onclick: () => {
        const lines = d.items.filter((i) => recv[i.item_id] && qtyBase(recv[i.item_id].p, recv[i.item_id].q) > 0)
          .map((i) => ({ product_id: i.product_id, unit_code: 'PC', qty: qtyBase(recv[i.item_id].p, recv[i.item_id].q) }));
        if (!lines.length) { toast(t('dep.enterQty'), 'err'); return; }
        call('receive', { lines }, t('tf.receivedOk'));
      } }, t('tf.receiveEntered')));
      act.push(h('button', { onclick: () => call('receive', {}, t('tf.receivedOk')) }, t('tf.receiveAll')));
    }
    const cancelOk = (d.status === 'DRAFT' && (mgr || d.created_by === S.me.user_id) && canSrc) || (open && mgr && canSrc);
    if (cancelOk) act.push(h('button', { class: 'danger', onclick: () => {
      const reason = prompt(open ? t('tf.cancelReasonOpen') : t('tf.cancelReason'));
      if (reason !== null) call('cancel', { reason: reason || null }, t('tf.cancelled'));
    } }, t('btn.cancel')));
    const kv = (k, v) => h('div', { class: 'kv' }, h('span', {}, k), h('b', {}, v ?? ''));
    V(h('p', {}, h('a', { href: '#/transfers' }, '← ' + t('nav.transfers'))),
      h('h2', {}, `${t('tf.transfer')} ${d.transfer_no} `, statusBadge(d.status)),
      h('div', { class: 'card grid' }, kv(t('tf.mode'), t('tf.mode.' + d.transfer_mode)), kv(t('tf.from'), `${d.from_code} — ${d.from_name}`),
        kv(t('tf.to'), `${d.to_code} — ${d.to_name}`), kv(t('lbl.date'), fmtDate(d.transfer_date)), kv(t('lbl.createdBy'), d.created_by_name), kv(t('lbl.remark'), d.remark)),
      open ? h('p', { class: 'banner warn' }, t('tf.inTransitHint')) : null,
      table([t('lbl.product'), t('lbl.unit'), { label: t('tf.sent') + ' (PC)', num: true }, { label: t('tf.received') + ' (PC)', num: true },
        { label: t('tf.returned') + ' (PC)', num: true }, { label: t('tf.outstanding') + ' (PC)', num: true },
        ...(open ? [1, 2, 3].flatMap((n) => [{ label: t('tf.receiveNow') + ' ' + t('uq.qtyN', { n }), num: true }, t('uq.unitN', { n })]) : [])],
      d.items.map((i) => h('tr', {}, td(`${i.product_code} — ${i.product_name}`), td(`${unitName(i.unit_code)}/${i.conversion_factor}`), td(fmtNum(i.qty_sent_base), true),
        td(fmtNum(i.qty_received_base), true), td(fmtNum(i.qty_cancelled_base), true), td(fmtNum(i.qty_outstanding_base), true),
        open ? unitCells((recv[i.item_id] || { p: prods[i.product_code] || { conversions: [] } }).p, (recv[i.item_id] || { q: emptyQ() }).q, null,
          { step: '1', disabled: !(i.qty_outstanding_base > 0) }) : null))),
      h('div', { class: 'actions' }, act));
  }


  // ---------------------------------------------------------------- dashboard
  const SVGNS = 'http://www.w3.org/2000/svg';
  function svgEl(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }

  /** Grouped bar chart. series = [{name, cls, values[]}], labels = x labels. Negative values are drawn below the axis. */
  function barChart(labels, series, height = 190, title = '') {
    const W = 720, padL = 46, padR = 8, padT = 10, padB = 24, H = height;
    const all = series.flatMap((s) => s.values);
    const max = Math.max(0, ...all), min = Math.min(0, ...all);
    const span = (max - min) || 1;
    const y = (v) => padT + (H - padT - padB) * (1 - (v - min) / span);
    const n = labels.length, gw = (W - padL - padR) / n, bw = Math.max(1, Math.min(18, (gw - 2) / series.length));
    const nice = (v) => (Math.abs(v) >= 1000 ? Math.round(v / 100) / 10 + 'k' : String(Math.round(v)));
    const name = title || series.map((x) => x.name).join(', ');
    const g = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', preserveAspectRatio: 'none', 'aria-label': name });
    g.append(svgEl('title', {}, name));
    [min, (max + min) / 2, max].forEach((v) => g.append(svgEl('line', { x1: padL, x2: W - padR, y1: y(v), y2: y(v), class: 'grid' }), svgEl('text', { x: padL - 4, y: y(v) + 3, class: 'tick', 'text-anchor': 'end' }, nice(v))));
    g.append(svgEl('line', { x1: padL, x2: W - padR, y1: y(0), y2: y(0), class: 'axis' }));
    const step = Math.max(1, Math.ceil(n / 8));
    labels.forEach((lb, i) => {
      const x0 = padL + i * gw + (gw - bw * series.length) / 2;
      series.forEach((s, j) => {
        const v = s.values[i], top = Math.min(y(v), y(0)), hgt = Math.max(Math.abs(y(v) - y(0)), v ? 1 : 0);
        g.append(svgEl('rect', { x: x0 + j * bw, y: top, width: Math.max(bw - 1, 1), height: hgt, class: 'bar ' + s.cls }, svgEl('title', {}, `${lb} — ${s.name}: ${fmtNum(v)}`)));
      });
      if (i % step === 0) g.append(svgEl('text', { x: padL + i * gw + gw / 2, y: H - 8, class: 'tick', 'text-anchor': 'middle' }, lb.slice(5)));
    });
    return g;
  }

  async function pageDashboard() {
    const V = viewFor();
    const f = { warehouse_id: '', days: 30 };
    const box = h('div');
    async function load() {
      const d = await api('/dashboard' + qs(f));
      if (main() !== V.el) return;
      const k = d.kpi;
      const kpi = (label, value, ic, tone, href) => h(href ? 'a' : 'div', { class: 'kpi ' + tone, href },
        h('span', { class: 'kpi-ic' }, icon(ic)), h('span', { class: 'kpi-txt' }, h('span', { class: 'kpi-lbl' }, label), h('b', { class: 'kpi-val' }, fmtNum(value))));
      const chip = (label, value, ic, hot, href) => h('a', { class: 'chip' + (Number(value) ? ' ' + hot : ''), href, role: 'listitem' },
        icon(ic), h('span', {}, label), h('b', {}, fmtNum(value)));
      const dates = d.series.map((x) => x.date);
      const topMax = Math.max(1, ...d.top_products.map((p) => Number(p.sold)));
      box.replaceChildren(
        h('div', { class: 'kpis' },
          kpi(t('dash.totalQty'), k.total_qty, 'stock', 'blue', '#/stock'), kpi(t('dash.skuInStock'), k.sku_in_stock, 'products', 'violet', '#/stock'),
          kpi(t('dash.soldToday'), k.sold_today, 'cart', 'green'), kpi(t('dash.soldPeriod', { n: d.days }), k.sold_period, 'trend', 'amber', '#/reports')),
        h('div', { class: 'chips', role: 'list', 'aria-label': t('dash.attention') },
          chip(t('dash.lowStock'), k.low_stock, 'alert', 'warn', '#/reports'), chip(t('dash.outOfStock'), k.out_of_stock, 'ban', 'bad', '#/stock'),
          chip(t('dash.inTransit'), k.transfers_in_transit, 'transfers', 'info', '#/transfers'),
          chip(t('dash.deposits'), k.deposits_open, 'deposits', k.deposits_overdue ? 'warn' : 'info', '#/deposits'),
          chip(t('dash.draftDocs'), k.draft_documents, 'draft', 'warn', '#/docs')),
        h('div', { class: 'grid2' },
          h('div', { class: 'card' }, h('h3', {}, t('dash.salesChart', { n: d.days })), barChart(dates, [{ name: t('rep.c.sold'), cls: 'a', values: d.series.map((x) => x.sold) }], 190, t('dash.salesChart', { n: d.days }))),
          h('div', { class: 'card' }, h('h3', {}, t('dash.flowChart', { n: d.days })),
            barChart(dates, [{ name: t('lbl.in'), cls: 'a', values: d.series.map((x) => x.in) }, { name: t('lbl.out'), cls: 'b', values: d.series.map((x) => x.out) }], 190, t('dash.flowChart', { n: d.days })),
            h('p', { class: 'legend' }, h('i', { class: 'sw a' }), t('lbl.in'), ' ', h('i', { class: 'sw b' }), t('lbl.out')))),
        h('div', { class: 'grid2' },
          h('div', { class: 'card' }, h('h3', {}, t('dash.topProducts')),
            d.top_products.length ? d.top_products.map((p) => h('div', { class: 'hbar' },
              h('span', { class: 'nm' }, `${p.product_code} — ${p.product_name}`),
              h('span', { class: 'bg' }, h('i', { style: `width:${Math.round((Number(p.sold) / topMax) * 100)}%` })), h('b', {}, fmtNum(p.sold)))) : h('div', { class: 'empty' }, icon('inbox'), h('span', {}, t('lbl.noData')))),
          h('div', { class: 'card' }, h('h3', {}, t('dash.byWarehouse')),
            table([t('lbl.warehouse'), { label: t('dash.skuInStock'), num: true }, { label: t('lbl.qtyPc'), num: true }],
              d.by_warehouse.map((w) => h('tr', {}, td(`${w.warehouse_code} — ${w.name}`), td(fmtNum(w.skus), true), td(fmtNum(w.qty), true)))))),
        h('div', { class: 'card' }, h('h3', {}, t('dash.lowItems')),
          table([t('lbl.warehouse'), t('lbl.product'), { label: t('lbl.qtyPc'), num: true }, { label: t('lbl.reorder'), num: true }],
            d.low_stock_items.map((r) => h('tr', { class: 'low' }, td(r.warehouse_code), td(`${r.product_code} — ${r.product_name}`), td(fmtNum(r.qty), true), td(fmtNum(r.reorder_level), true))))));
    }
    const whSel = select([['', t('lbl.all')], ...S.whs.filter((w) => S.me.warehouses.includes(w.warehouse_id) && w.warehouse_type === 'PHYSICAL').map((w) => [w.warehouse_id, w.warehouse_code + ' — ' + w.name])], '',
      { onchange: (e) => { f.warehouse_id = e.target.value; load().catch(fail); } });
    V(h('h2', {}, t('nav.dashboard')),
      h('div', { class: 'actions' }, field(t('lbl.warehouse'), whSel),
        field(t('dash.period'), select([[7, t('dash.lastDays', { n: 7 })], [30, t('dash.lastDays', { n: 30 })], [90, t('dash.lastDays', { n: 90 })]], 30, { onchange: (e) => { f.days = Number(e.target.value); load().catch(fail); } }))),
      box);
    await load();
  }

  // ---------------------------------------------------------------- reports
  async function downloadFile(path, params, filename) {
    const res = await fetch('/api' + path + qs(params), { headers: { Authorization: 'Bearer ' + S.token } });
    if (!res.ok) { let e; try { e = (await res.json()).error; } catch (_) { e = null; } fail(Object.assign(new Error((e && e.message) || t('err.UNKNOWN')), e || {})); return; }
    const url = URL.createObjectURL(await res.blob());
    const a = h('a', { href: url, download: filename });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }

  async function pageReports() {
    const V = viewFor();
    const defs = await api('/reports' + qs({ lang: S.lang }));
    const f = { name: defs[0].name, warehouse_id: '', date_from: '', date_to: '', days: 30, q: '', low_only: false, status: '', class: '', action: '' };
    const filters = h('div', { class: 'filters' }), out = h('div'), info = h('p', { class: 'hint' });
    const usesDates = () => ['movements', 'sales_summary', 'transfers', 'audit', 'deposits'].includes(f.name);
    const params = () => {
      const p = { lang: S.lang, warehouse_id: f.warehouse_id };
      if (usesDates()) { p.date_from = f.date_from; p.date_to = f.date_to; }
      if (f.name === 'movers') { p.days = f.days; p.class = f.class; }
      if (f.name === 'stock_balance') { p.q = f.q; p.low_only = f.low_only; }
      if (f.name === 'transfers' || f.name === 'deposits') p.status = f.status;
      if (f.name === 'deposits') p.q = f.q;
      if (f.name === 'audit') p.action = f.action;
      return p;
    };
    const cell = (kind, v, labels) => {
      if (v == null || v === '') return '';
      if (kind === 's') return labels[v] || v;
      if (kind === 'n') return fmtNum(v);
      if (kind === 'f') return Number(v).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 2 });
      if (kind === 'd') return fmtDate(v);
      if (kind === 'dt') return fmtDateTime(v);
      const sv = String(v);
      return hasKey('doc.' + sv) ? t('doc.' + sv) : hasKey('status.' + sv) ? t('status.' + sv) : hasKey('tf.mode.' + sv) ? t('tf.mode.' + sv) : sv;
    };
    const rpg = makePager(() => { run(true); out.scrollIntoView({ block: 'nearest' }); });
    async function run(keep) {
      if (!keep) rpg.page = 0;
      try {
        const r = await api('/reports/' + f.name + qs({ ...params(), limit: rpg.size, offset: rpg.start() }));
        if (main() !== V.el) return;
        info.textContent = t('rep.total', { n: fmtNum(r.total) }) + (r.truncated ? ' ' + t('rep.truncated') : '');
        rpg.total = Math.min(r.total, 50000); rpg.render();
        out.replaceChildren(table(r.columns.map((c) => ({ label: c.text, num: ['n', 'f'].includes(c.kind) })),
          r.rows.map((row) => h('tr', { class: ['LOW', 'OUT'].includes(row.status) || (f.name === 'deposits' && row.due && row.due < today() && ['OPEN', 'PARTIAL'].includes(row.status)) ? 'low' : '' }, r.columns.map((c) => td(cell(c.kind, row[c.key], r.labels), ['n', 'f'].includes(c.kind))))), { paged: false }), rpg.el);
      } catch (e) { fail(e); }
    }
    function drawFilters() {
      const def = defs.find((d) => d.name === f.name);
      const whs = S.whs.filter((w) => S.me.warehouses.includes(w.warehouse_id) && w.warehouse_type === 'PHYSICAL');
      const input = (type, key, extra) => h('input', { type, value: f[key], ...extra, onchange: (e) => { f[key] = type === 'checkbox' ? e.target.checked : e.target.value; run(); } });
      filters.replaceChildren(...present([
        field(t('rep.choose'), select(defs.map((d) => [d.name, d.title]), f.name, { onchange: (e) => { f.name = e.target.value; f.status = ''; drawFilters(); run(); } })),
        f.name !== 'audit' ? field(t('lbl.warehouse'), select([['', t('lbl.all')], ...whOptions(whs)], f.warehouse_id, { onchange: (e) => { f.warehouse_id = e.target.value; run(); } })) : null,
        usesDates() ? field(t('lbl.dateFrom'), input('date', 'date_from')) : null, usesDates() ? field(t('lbl.dateTo'), input('date', 'date_to')) : null,
        f.name === 'movers' ? field(t('rep.days'), input('number', 'days', { min: 1, max: 365 })) : null,
        f.name === 'movers' ? field(t('rep.class'), select([['', t('lbl.all')], ...['FAST', 'NORMAL', 'SLOW', 'DEAD'].map((c) => [c, t('rep.cls.' + c)])], f.class, { onchange: (e) => { f.class = e.target.value; run(); } })) : null,
        f.name === 'stock_balance' ? field(t('lbl.searchProduct'), h('input', { type: 'search', value: f.q, oninput: debounce((e) => { f.q = e.target.value; run(); }) })) : null,
        f.name === 'stock_balance' ? h('label', { class: 'check' }, input('checkbox', 'low_only', { checked: f.low_only }), t('lbl.lowOnly')) : null,
        f.name === 'transfers' ? field(t('lbl.status'), select([['', t('lbl.all')], ...['DRAFT', 'IN_TRANSIT', 'PARTIAL', 'COMPLETED', 'CANCELLED'].map((x) => [x, t('status.' + x)])], f.status, { onchange: (e) => { f.status = e.target.value; run(); } })) : null,
        f.name === 'audit' ? field(t('rep.action'), input('text', 'action', { placeholder: 'POST_DOC' })) : null,
        f.name === 'deposits' ? field(t('lbl.status'), select([['', t('dep.openOnly')], ...['OPEN', 'PARTIAL', 'COMPLETED', 'CANCELLED'].map((x) => [x, t('dep.st.' + x)])], f.status, { onchange: (e) => { f.status = e.target.value; run(); } })) : null,
        f.name === 'deposits' ? field(t('dep.search'), h('input', { type: 'search', value: f.q, oninput: debounce((e) => { f.q = e.target.value; run(); }) })) : null,
        h('button', { class: 'primary', onclick: () => downloadFile('/reports/' + f.name, { ...params(), format: 'xlsx' }, `${f.name}.xlsx`) }, t('btn.exportExcel')),
        h('button', { onclick: () => downloadFile('/reports/' + f.name, { ...params(), format: 'pdf' }, `${f.name}.pdf`) }, t('btn.exportPdf'))]));
      void def;
    }
    drawFilters();
    const integ = h('div');
    const adminBox = isAdmin() ? h('div', { class: 'card' }, h('h3', {}, t('rep.integrity')), h('p', { class: 'hint' }, t('rep.integrityHint')),
      h('button', { onclick: async () => {
        try {
          const r = await api('/admin/integrity');
          integ.replaceChildren(h('p', { class: 'banner ' + (r.ok ? 'ok' : 'warn') }, r.ok ? t('rep.integrityOk') : t('rep.integrityBad')),
            table([t('rep.check'), t('lbl.status'), { label: t('rep.issues'), num: true }],
              r.checks.map((c) => h('tr', { class: c.ok ? '' : 'low' }, td(hasKey('chk.' + c.name) ? t('chk.' + c.name) : c.name), td(c.ok ? 'OK' : '✗'), td(fmtNum(c.count), true)))));
        } catch (e) { fail(e); }
      } }, t('rep.runIntegrity')), integ) : null;
    V(h('h2', {}, t('nav.reports')), filters, info, out, adminBox);
    await run();
  }

  // ---------------------------------------------------------------- account
  async function pageAccount() {
    const V = viewFor();
    const v = { current_password: '', new_password: '', confirm: '' };
    const inp = (k) => h('input', { type: 'password', autocomplete: k === 'current_password' ? 'current-password' : 'new-password', oninput: (e) => { v[k] = e.target.value; } });
    V(h('h2', {}, t('nav.account')),
      h('p', {}, `${S.me.full_name} (${S.me.username}) · ${S.me.role_code}`),
      h('form', { class: 'card form', onsubmit: async (e) => {
        e.preventDefault();
        if (v.new_password !== v.confirm) { toast(t('acct.mismatch'), 'err'); return; }
        try {
          const r = await api('/me/password', { method: 'POST', body: { current_password: v.current_password, new_password: v.new_password } });
          S.token = r.token; localStorage.setItem('wms_token', r.token);
          toast(t('acct.changed')); e.target.reset(); v.current_password = v.new_password = v.confirm = '';
        } catch (err) { fail(err); }
      } },
      h('h3', { class: 'wide' }, t('acct.changePassword')),
      field(t('acct.current'), inp('current_password')), field(t('acct.new'), inp('new_password')), field(t('acct.confirm'), inp('confirm')),
      h('p', { class: 'hint wide' }, t('acct.policy')),
      h('div', { class: 'actions wide' }, h('button', { class: 'primary', type: 'submit' }, t('btn.save')))));
  }

  // ---------------------------------------------------------------- barcode / photo helpers
  const canScan = () => S.me.role_code !== 'VIEWER' && S.me.transact_warehouses.length > 0;
  const canDeposit = () => S.me.role_code !== 'VIEWER' && S.me.transact_warehouses.length > 0;

  let zxingLoading = null;
  function loadZxing() {
    if (window.ZXing) return Promise.resolve();
    if (!zxingLoading) {
      zxingLoading = new Promise((ok, bad) => {
        const s = h('script', { src: '/static/vendor/zxing.min.js' });
        s.onload = ok; s.onerror = () => { zxingLoading = null; bad(new Error('zxing')); };
        document.head.append(s);
      });
    }
    return zxingLoading;
  }

  /** Shrink a photo before upload (phones produce 4000px+ images). Falls back to the original file. */
  async function downscale(file, max = 2000) {
    try {
      const bmp = await createImageBitmap(file);
      const k = Math.min(1, max / Math.max(bmp.width, bmp.height));
      const c = document.createElement('canvas');
      c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
      c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
      const blob = await new Promise((r) => c.toBlob(r, 'image/jpeg', 0.85));
      return blob || file;
    } catch (_) { return file; }
  }

  /** Read a barcode / QR code from a photo: native BarcodeDetector when the browser has it, else ZXing. */
  async function decodeBarcode(file) {
    const bmp = await createImageBitmap(file);
    if ('BarcodeDetector' in window) {
      try {
        const found = await new window.BarcodeDetector().detect(bmp);
        if (found.length) return found[0].rawValue;
      } catch (_) { /* fall back to ZXing */ }
    }
    await loadZxing();
    const Z = window.ZXing;
    const hints = new Map([[Z.DecodeHintType.TRY_HARDER, true]]);
    for (const size of [1600, 1000, 2400]) {
      const k = Math.min(1, size / Math.max(bmp.width, bmp.height));
      const c = document.createElement('canvas');
      c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
      c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
      try {
        const reader = new Z.MultiFormatReader();
        reader.setHints(hints);
        const res = reader.decode(new Z.BinaryBitmap(new Z.HybridBinarizer(new Z.HTMLCanvasElementLuminanceSource(c))));
        if (res && res.getText()) return res.getText();
      } catch (_) { /* not found at this size */ }
    }
    return null;
  }

  /** A "📷" button that takes/chooses a photo, reads the barcode and calls onCode(text). */
  function barcodeCameraButton(onCode) {
    const inp = h('input', { type: 'file', accept: 'image/*', capture: 'environment', class: 'vh', onchange: async (e) => {
      const f = e.target.files[0]; e.target.value = '';
      if (!f) return;
      toast(t('scan.decoding'));
      try {
        const code = await decodeBarcode(f);
        if (code) onCode(code.trim()); else toast(t('scan.noBarcode'), 'err');
      } catch (_) { toast(t('scan.noBarcode'), 'err'); }
    } });
    return h('label', { class: 'btn', title: t('scan.cameraHint') }, icon('camera'), t('scan.camera'), inp);
  }

  /** Find a product by product code, product barcode or unit barcode. Returns { product, unit_code } or null. */
  async function findByCode(code) {
    const found = await api('/products' + qs({ q: code, limit: 10 }));
    const up = code.toUpperCase();
    let p = found.find((x) => x.product_code.toUpperCase() === up || x.barcode === code);
    if (p) return { product: p, unit_code: 'PC' };
    for (const x of found) {
      const c = (x.conversions || []).find((cv) => cv.barcode === code);
      if (c) return { product: x, unit_code: c.unit_code };
    }
    return found.length === 1 ? { product: found[0], unit_code: 'PC' } : null;
  }

  async function showBillImage(scanId) {
    try {
      const res = await fetch(`/api/bill-scans/${scanId}/image`, { headers: { Authorization: 'Bearer ' + S.token } });
      if (!res.ok) throw Object.assign(new Error('HTTP ' + res.status), { code: 'NOT_FOUND' });
      const url = URL.createObjectURL(await res.blob());
      const dlg = h('dialog', { class: 'imgdlg', onclose: () => { URL.revokeObjectURL(url); dlg.remove(); } },
        h('img', { src: url, class: 'billimg zoomable', onclick: (e) => e.target.classList.toggle('zoom') }),
        h('div', { class: 'actions' }, h('button', { onclick: () => dlg.close() }, t('btn.close'))));
      document.body.append(dlg); dlg.showModal();
    } catch (e) { fail(e); }
  }

  const warnText = (w) => t('scan.w.' + w.code, w);

  // ---------------------------------------------------------------- bill scanning
  async function pageScan() {
    if (!canScan()) { location.hash = '#/docs'; return; }
    const V = viewFor();
    const st = await api('/bill-scan/status');
    let file = null, url = null;
    const reading = () => (st.free ? t('scan.readingFree') : t('scan.reading'));
    const preview = h('div', { class: 'scanprev' });
    const status = h('p', { class: 'hint' });
    const readBtn = h('button', { class: 'primary', disabled: true, onclick: read }, '🔍 ' + (st.free ? t('scan.readFree') : t('scan.read')));
    async function onFile(e) {
      const f = e.target.files[0]; e.target.value = '';
      if (!f) return;
      file = await downscale(f);
      if (url) URL.revokeObjectURL(url);
      url = URL.createObjectURL(file);
      preview.replaceChildren(h('img', { src: url, class: 'billimg zoomable', onclick: (ev) => ev.target.classList.toggle('zoom') }));
      readBtn.disabled = !st.configured;
      status.textContent = '';
    }
    async function read() {
      if (!file) return;
      readBtn.disabled = true;
      status.textContent = reading();
      const fd = new FormData();
      fd.append('file', file, 'bill.jpg');
      try {
        const r = await api('/bill-scan', { method: 'POST', form: fd });
        const allowed = myDocTypes();
        const dt = allowed.includes(r.draft.doc_type) ? r.draft.doc_type : (allowed.includes('SALE') ? 'SALE' : allowed[0]);
        S.scanDraft = { ...r.draft, doc_type: dt, scan_id: r.scan_id, imageUrl: url };
        url = null;  // now owned by the draft page
        location.hash = '#/new/' + dt;
      } catch (e) { fail(e); readBtn.disabled = false; status.textContent = ''; }
    }
    V(h('h2', {}, t('nav.scan')),
      st.configured ? h('p', { class: 'banner ' + (st.free ? 'ok' : 'ok') }, st.free ? t('scan.engineFree') : t('scan.engineAi'))
        : h('p', { class: 'banner warn' }, st.engine === 'claude' ? t('scan.notConfigured') : t('scan.ocrMissing')),
      h('p', { class: 'hint' }, t('scan.hint')),
      h('div', { class: 'actions' },
        h('label', { class: 'btn primary' }, icon('camera'), t('scan.takePhoto'), h('input', { type: 'file', accept: 'image/*', capture: 'environment', class: 'vh', onchange: onFile })),
        h('label', { class: 'btn' }, icon('image'), t('scan.choose'), h('input', { type: 'file', accept: 'image/*', class: 'vh', onchange: onFile }))),
      preview, h('div', { class: 'actions' }, readBtn), status,
      h('div', { class: 'card' }, h('h3', {}, t('scan.tipsTitle')), h('ul', { class: 'tips' }, ['scan.tip1', 'scan.tip2', 'scan.tip3', 'scan.tip4'].map((k) => h('li', {}, t(k))))));
  }

  // ---------------------------------------------------------------- customer deposits (ฝากคลัง)
  const depBadge = (s) => h('span', { class: 'badge ' + s.toLowerCase() }, t('dep.st.' + s));
  const factorOf = (convs, unit) => (unit === 'PC' ? 1 : ((convs || []).find((c) => c.unit_code === unit) || { factor_to_base: 1 }).factor_to_base);
  const qtyWithBreakdown = (base, convs) => { const b = breakdown(base, convs); return fmtNum(base) + (b && b !== fmtNum(base) + ' PC' ? ' (' + b + ')' : ''); };

  async function pageDeposits() {
    const f = { open_only: true, status: '', warehouse_id: '', q: '' };
    const box = h('div');
    const pg = makePager(() => load(true));
    async function load(keep) {
      if (!keep) pg.page = 0;
      try {
        const rows = pg.take(await api('/deposits' + qs({ ...f, ...pg.q() })));
        box.replaceChildren(table(
          [t('dep.no'), t('lbl.date'), t('dep.due'), t('lbl.customer'), t('lbl.warehouse'), t('dep.saleBill'), { label: t('lbl.lines'), num: true }, { label: t('dep.remainingPc'), num: true }, t('lbl.status')],
          rows.map((r) => h('tr', { class: 'click' + (r.due_date && r.due_date < today() && ['OPEN', 'PARTIAL'].includes(r.status) ? ' low' : ''), onclick: () => { location.hash = '#/deposit/' + r.deposit_id; } },
            td(r.deposit_no), td(fmtDate(r.deposit_date)), td(fmtDate(r.due_date)), td((r.customer_code ? r.customer_code + ' — ' : '') + (r.customer_display || '')),
            td(r.warehouse_code), td(r.sale_doc_no), td(r.line_count, true), td(fmtNum(r.qty_remaining_base || 0), true), td(depBadge(r.status))))));
      } catch (e) { fail(e); }
    }
    view(h('h2', {}, t('nav.deposits')),
      h('p', { class: 'hint' }, t('dep.hint')),
      h('div', { class: 'filters' },
        field(t('lbl.status'), select([['open', t('dep.openOnly')], ['', t('lbl.all')], ...['OPEN', 'PARTIAL', 'COMPLETED', 'CANCELLED'].map((x) => [x, t('dep.st.' + x)])], 'open',
          { onchange: (e) => { f.open_only = e.target.value === 'open'; f.status = e.target.value === 'open' ? '' : e.target.value; load(); } })),
        field(t('lbl.warehouse'), select(whOptions(S.whs.filter((w) => w.warehouse_type === 'PHYSICAL'), true), '', { onchange: (e) => { f.warehouse_id = e.target.value; load(); } })),
        field(t('dep.search'), h('input', { type: 'search', oninput: debounce((e) => { f.q = e.target.value; load(); }) }))),
      box, pg.el);
    load();
  }

  async function pageDeposit(id) {
    const V = viewFor();
    let d;
    try { d = await api('/deposits/' + id); } catch (e) { fail(e); if (main() === V.el) location.hash = '#/deposits'; return; }
    const active = ['OPEN', 'PARTIAL'].includes(d.status);
    const canAct = active && canDeposit() && S.me.transact_warehouses.includes(d.warehouse_id);
    const key = uuid();
    const plan = {};   // deposit_item_id -> { qty, unit_code }
    const kv = (k, v) => h('div', { class: 'kv' }, h('span', {}, k), h('b', {}, v ?? ''));
    async function deliver(all) {
      let body = {};
      if (!all) {
        const lines = Object.entries(plan).map(([iid, x]) => ({ deposit_item_id: Number(iid), qty: qtyBase(x.p, x.q), unit_code: 'PC' }))
          .filter((x) => x.qty > 0);
        if (!lines.length) { toast(t('dep.enterQty'), 'err'); return; }
        if (lines.some((x) => !Number.isInteger(x.qty))) { toast(t('err.NOT_WHOLE_PC'), 'err'); return; }
        body = { lines };
      }
      body.delivery_date = dlvDate.value || null;
      body.remark = dlvRemark.value || null;
      if (!confirm(all ? t('dep.confirmAll') : t('dep.confirmDeliver'))) return;
      try {
        await api(`/deposits/${id}/deliver`, { method: 'POST', body, headers: { 'Idempotency-Key': key + (all ? '-all' : '-' + JSON.stringify(body.lines)) } });
        toast(t('dep.delivered')); pageDeposit(id);
      } catch (e) { fail(e); }
    }
    const dlvDate = h('input', { type: 'date', value: today() });
    const dlvRemark = h('input', { type: 'text', maxlength: 500 });
    const cancelReason = h('input', { type: 'text', maxlength: 500 });
    const cancelReturn = h('input', { type: 'checkbox', checked: true });
    const itemsTable = table(
      [t('lbl.code'), t('lbl.product'), { label: t('dep.deposited'), num: true }, { label: t('dep.deliveredQ'), num: true }, { label: t('dep.cancelledQ'), num: true }, { label: t('dep.remaining'), num: true },
       ...(canAct ? [...[1, 2, 3].flatMap((n) => [{ label: t('uq.qtyN', { n }), num: true }, t('uq.unitN', { n })])] : [])],
      d.items.map((i) => {
        const x = { p: { conversions: i.conversions || [] }, q: emptyQ() };
        if (i.qty_remaining_base > 0) plan[i.deposit_item_id] = x;
        return h('tr', {}, td(i.product_code), td(i.product_name), td(fmtNum(i.qty_base), true), td(fmtNum(i.qty_delivered_base), true),
          td(i.qty_cancelled_base ? fmtNum(i.qty_cancelled_base) : '', true), td(h('b', {}, qtyWithBreakdown(i.qty_remaining_base, i.conversions)), true),
          ...(canAct ? unitCells(x.p, x.q, null, { step: '1', disabled: !(i.qty_remaining_base > 0) }) : []));
      }));
    const byItem = Object.fromEntries(d.items.map((i) => [i.deposit_item_id, i]));
    V(h('p', {}, h('a', { href: '#/deposits' }, '← ' + t('nav.deposits'))),
      h('h2', {}, t('dep.title') + ' ' + d.deposit_no + ' ', depBadge(d.status)),
      h('div', { class: 'card grid' },
        kv(t('lbl.customer'), (d.customer_code ? d.customer_code + ' — ' : '') + (d.customer_display || '—')),
        kv(t('lbl.warehouse'), d.warehouse_code + ' — ' + d.warehouse_name),
        kv(t('dep.saleBill'), h('a', { href: '#/doc/' + d.sale_document_id }, d.sale_doc_no + ' (' + fmtDate(d.sale_doc_date) + ')')),
        kv(t('lbl.date'), fmtDate(d.deposit_date)), kv(t('dep.due'), fmtDate(d.due_date) || '—'),
        kv(t('lbl.createdBy'), d.created_by_name + ' ' + fmtDateTime(d.created_at)),
        d.remark ? kv(t('lbl.remark'), d.remark) : null,
        d.cancel_reason ? kv(t('dep.cancelReason'), d.cancel_reason) : null,
        d.return_document_id ? kv(t('dep.returnDoc'), h('a', { href: '#/doc/' + d.return_document_id }, d.return_doc_no)) : null),
      itemsTable,
      canAct ? h('div', { class: 'card form' },
        h('h3', { class: 'wide' }, t('dep.deliverTitle')),
        field(t('dep.deliveryDate'), dlvDate), field(t('lbl.remark'), dlvRemark, 'wide'),
        h('div', { class: 'actions wide' },
          h('button', { class: 'primary', onclick: () => deliver(false) }, icon('truck'), t('dep.deliverSelected')),
          h('button', { onclick: () => deliver(true) }, t('dep.deliverAll')))) : null,
      d.deliveries.length ? h('div', { class: 'card' }, h('h3', {}, t('dep.history')),
        table([t('dep.deliveryNo'), t('lbl.date'), t('lbl.product'), { label: t('lbl.qtyPc'), num: true }, t('lbl.createdBy'), t('lbl.remark')],
          d.deliveries.flatMap((dl) => (dl.lines || []).map((ln, n) => h('tr', {},
            td(n === 0 ? dl.delivery_no : ''), td(n === 0 ? fmtDate(dl.delivery_date) : ''),
            td(byItem[ln.deposit_item_id] ? byItem[ln.deposit_item_id].product_code + ' — ' + byItem[ln.deposit_item_id].product_name : ''),
            td(fmtNum(ln.qty_base), true), td(n === 0 ? dl.created_by_name : ''), td(n === 0 ? (dl.remark || '') : '')))))) : null,
      canAct && isManager() ? h('div', { class: 'card form' },
        h('h3', { class: 'wide' }, t('dep.cancelTitle')),
        field(t('dep.cancelReason'), cancelReason, 'wide'),
        h('label', { class: 'check wide' }, cancelReturn, t('dep.returnToStock')),
        h('p', { class: 'hint wide' }, t('dep.cancelHint')),
        h('div', { class: 'actions wide' }, h('button', { class: 'danger', onclick: async () => {
          if (!cancelReason.value.trim()) { toast(t('dep.reasonRequired'), 'err'); return; }
          if (!confirm(t('dep.confirmCancel'))) return;
          try {
            await api(`/deposits/${id}/cancel`, { method: 'POST', body: { reason: cancelReason.value.trim(), return_to_stock: cancelReturn.checked } });
            toast(t('dep.cancelled')); pageDeposit(id);
          } catch (e) { fail(e); }
        } }, t('dep.cancelBtn')))) : null);
  }

  async function pageNewDeposit(docId) {
    const V = viewFor();
    let d;
    try { d = await api('/documents/' + docId); } catch (e) { fail(e); return; }
    const products = {};
    await Promise.all([...new Set(d.items.map((i) => i.product_code))].map(async (code) => {
      try { const r = await findByCode(code); if (r) products[code] = r.product; } catch (_) { /* ignore */ }
    }));
    const plan = {};
    const due = h('input', { type: 'date' });
    const remark = h('input', { type: 'text', maxlength: 500 });
    const custName = h('input', { type: 'text', maxlength: 200 });
    const key = uuid();
    V(h('p', {}, h('a', { href: '#/doc/' + docId }, '← ' + d.doc_no)),
      h('h2', {}, t('dep.newTitle') + ' — ' + d.doc_no),
      h('p', { class: 'hint' }, t('dep.newHint')),
      table([t('lbl.code'), t('lbl.product'), { label: t('dep.sold'), num: true }, { label: t('dep.alreadyDeposited'), num: true }, { label: t('dep.canDeposit'), num: true },
        ...[1, 2, 3].flatMap((n) => [{ label: t('uq.qtyN', { n }), num: true }, t('uq.unitN', { n })])],
        d.items.map((i) => {
          const free = i.base_qty - (i.deposited_base || 0);
          const x = { p: { conversions: products[i.product_code] ? products[i.product_code].conversions : [] }, q: emptyQ() };
          if (free > 0) plan[i.item_id] = x;
          return h('tr', {}, td(i.product_code), td(i.product_name), td(fmtNum(i.base_qty), true), td(fmtNum(i.deposited_base || 0), true), td(fmtNum(free), true),
            unitCells(x.p, x.q, null, { step: '1', disabled: !(free > 0) }));
        })),
      h('div', { class: 'card form' },
        field(t('dep.due'), due),
        d.customer_id ? null : field(t('dep.customerName'), custName),
        field(t('lbl.remark'), remark, 'wide'),
        h('div', { class: 'actions wide' }, h('button', { class: 'primary', onclick: async () => {
          const items = Object.entries(plan).map(([sid, x]) => ({ sale_item_id: Number(sid), qty: qtyBase(x.p, x.q), unit_code: 'PC' }))
            .filter((x) => x.qty > 0);
          if (!items.length) { toast(t('dep.enterQty'), 'err'); return; }
          try {
            const r = await api('/deposits', { method: 'POST', headers: { 'Idempotency-Key': key },
              body: { sale_document_id: Number(docId), items, due_date: due.value || null, remark: remark.value || null, customer_name: custName.value || null } });
            toast(t('dep.created')); location.hash = '#/deposit/' + r.deposit_id;
          } catch (e) { fail(e); }
        } }, t('dep.createBtn')))));
  }

  // ---------------------------------------------------------------- multi-unit quantities (unit 1 / unit 2 / unit 3)
  // Each product has up to three units: the two largest packing units (e.g. CT ×24, PK ×6) and the base unit PC.
  // Lines keep one quantity per unit, like a paper order form; totals are always computed in PC.
  function unitName(code) { const k = 'unitname.' + code; const v = t(k); return v === k ? code : v; }
  function unitSlots(p) {
    const convs = ((p && p.conversions) || []).filter((c) => c.unit_code !== 'PC' && c.factor_to_base > 1)
      .sort((a, b) => b.factor_to_base - a.factor_to_base);
    return [convs[0] ? { code: convs[0].unit_code, factor: convs[0].factor_to_base } : null,
      convs[1] ? { code: convs[1].unit_code, factor: convs[1].factor_to_base } : null,
      { code: 'PC', factor: 1 }];
  }
  const emptyQ = () => ['', '', ''];
  const numOr0 = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);
  function qtyBase(p, q) { return unitSlots(p).reduce((a, s, i) => a + (s ? numOr0(q[i]) * s.factor : 0), 0); }
  /** add `qty` of `unit` to the right unit slot (units beyond the first two are converted to PC) */
  function addUnitQty(p, q, unit, qty) {
    const slots = unitSlots(p);
    let i = slots.findIndex((s) => s && s.code === unit);
    let n = numOr0(qty);
    if (i < 0) {
      const cv = ((p && p.conversions) || []).find((c) => c.unit_code === unit);
      n *= cv ? cv.factor_to_base : 1;
      i = 2;
    }
    const sum = numOr0(q[i]) + n;
    q[i] = sum ? String(+sum.toFixed(4)) : '';
  }
  /** split a PC amount into the unit slots (largest first) */
  function baseToQ(p, base) {
    const q = emptyQ();
    let rest = Math.max(0, Math.round(numOr0(base)));
    unitSlots(p).forEach((s, i) => { if (s && rest > 0) { const n = Math.floor(rest / s.factor); if (n) { q[i] = String(n); rest -= n * s.factor; } } });
    return q;
  }
  function qItems(p, q) {
    return unitSlots(p).map((s, i) => (s && numOr0(q[i]) > 0 ? { unit_code: s.code, qty: String(q[i]), factor: s.factor } : null)).filter(Boolean);
  }
  /** six cells: [qty input, "unit/rate"] × 3. onChange() runs after every keystroke. */
  function unitCells(p, q, onChange, opts = {}) {
    const slots = unitSlots(p);
    return slots.flatMap((s, i) => {
      const inp = h('input', { class: 'qty uq', type: 'number', min: '0', step: opts.step || 'any', value: s ? q[i] : '',
        disabled: !s || !p || opts.disabled, 'data-slot': i,
        oninput: (e) => { q[i] = e.target.value; onChange && onChange(); },
        onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); focusNextQty(e.target); } } });
      inp.setAttribute('aria-label', t('uq.qtyN', { n: i + 1 }) + (s && p ? ' (' + unitName(s.code) + ')' : ''));
      if (!s || !p) inp.value = '';
      return [h('td', { class: 'num uqcell' }, inp), h('td', { class: 'ulabel' }, s && p ? `${unitName(s.code)}/${s.factor}` : '')];
    });
  }
  function focusNextQty(el) {
    const all = [...document.querySelectorAll('#main input.uq:not([disabled])')];
    const n = all[all.indexOf(el) + 1];
    if (n) { n.focus(); n.select(); }
  }
  const unitHeaders = () => [1, 2, 3].flatMap((n) => [h('th', { class: 'num' }, t('uq.qtyN', { n })), h('th', {}, t('uq.unitN', { n }))]);
  /** stock on hand as unit badges (like 66 | 1 | 2) */
  function availBadges(p, base) {
    if (base == null || !p) return '';
    if (base <= 0) return h('span', { class: 'qb zero', title: t('uq.none') }, '0');
    const q = baseToQ(p, base);
    const slots = unitSlots(p);
    return h('span', { class: 'qbs', title: fmtNum(base) + ' PC' }, slots.map((s, i) => (s ? h('span', { class: 'qb', title: unitName(s.code) }, fmtNum(numOr0(q[i]))) : null)));
  }
  const stockState = (need, have) => (need <= 0 || have == null ? '' : need <= have
    ? h('span', { class: 'sb ok' }, t('uq.enough')) : h('span', { class: 'sb no' }, t('uq.short')));
  async function loadAvail(whId) {
    if (!whId) return {};
    try { return await api('/stock/available' + qs({ warehouse_id: whId })); } catch (_) { return {}; }
  }
  /** document items grouped per product (one row per product, quantity per unit) */
  function groupItems(items) {
    const out = [];
    for (const it of items) {
      const last = out[out.length - 1];
      if (last && last.product_id === it.product_id && (last.adj_sign || null) === (it.adj_sign || null)) last.items.push(it);
      else out.push({ product_id: it.product_id, product_code: it.product_code, product_name: it.product_name, adj_sign: it.adj_sign || null, items: [it] });
    }
    for (const g of out) {
      g.items.sort((a, b) => b.conversion_factor - a.conversion_factor);
      g.base = g.items.reduce((a, i) => a + Number(i.base_qty), 0);
      g.deposited = g.items.reduce((a, i) => a + Number(i.deposited_base || 0), 0);
    }
    return out;
  }
  /** 6 read-only cells for up to 3 units of a document group */
  function unitTextCells(group) {
    const cells = [];
    for (let i = 0; i < 3; i++) {
      const it = group.items.length <= 3 ? group.items[i] : (i < 2 ? group.items[i] : null);
      if (i === 2 && group.items.length > 3) {
        const rest = group.items.slice(2).reduce((a, x) => a + Number(x.base_qty), 0);
        cells.push(td(fmtNum(rest), true), td(unitName('PC') + '/1'));
      } else cells.push(td(it ? fmtNum(it.qty) : '', true), td(it ? `${unitName(it.unit_code)}/${it.conversion_factor}` : ''));
    }
    return cells;
  }

  // ---------------------------------------------------------------- router
  function route() {
    if (!S.me) return;
    const parts = location.hash.replace(/^#\/?/, '').split('/');
    const [page, arg] = [parts[0] || 'dashboard', parts[1]];
    renderShell();
    const run = {
      stock: pageStock, docs: pageDocs, doc: () => pageDoc(Number(arg)),
      new: () => (arg === 'CUSTOMER_RETURN' ? pageCancel() : pageNewDoc(arg)), cancel: () => pageCancel(Number(arg) || null),
      dashboard: pageDashboard, reports: pageReports, account: pageAccount,
      import: pageImport, imports: pageImports,
      transfers: pageTransfers, 'transfer-new': pageNewTransfer, transfer: () => pageTransfer(Number(arg)),
      scan: pageScan, deposits: pageDeposits, deposit: () => pageDeposit(Number(arg)), 'deposit-new': () => pageNewDeposit(Number(arg)),
      movements: pageMovements, products: pageProducts, counts: pageCounts, masters: () => pageMasters(arg || 'warehouses'), users: pageUsers,
    }[page] || pageDashboard;
    Promise.resolve(run()).catch(fail);
  }

  async function boot() {
    document.documentElement.lang = S.lang;
    document.title = t('app.title');
    if (!S.token) { renderShell(); return; }
    try {
      S.me = await api('/me');
      await loadRef();
      if (!location.hash) location.hash = '#/dashboard';
      route();
    } catch (e) {
      if (e.status !== 401) fail(e);
      S.me = null; renderShell();
    }
  }

  window.addEventListener('hashchange', route);
  boot();
})();
