/* 图片页（console-images.js）的回归检查：Node 自带的 node:test + 一个很小的假 DOM，不装任何依赖。
   由 tests/test_console_images_js.py 在跑后端测试时顺带跑（机器上有 node 才跑）。
   请求的返回顺序由这里一个个放行，专门复现「先发出去的读请求比⭐保存晚回来」这种交错（#41 Codex 审核 P2）。 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'console', 'console-images.js'), 'utf8');

// ---------- 假 DOM：只做 console-images.js 用得到的那些 ----------
class Node {
  constructor(tag) {
    this.tagName = tag;
    this.kids = [];
    this.parentNode = null;
    this.attrs = {};
    this.on = {};
    this.cls = new Set();
    this.text = '';
    this.style = {};
    this.hidden = false;
    this.scrollTop = 0;
    this.scrollLeft = 0;
  }
  get className() { return [...this.cls].join(' '); }
  set className(v) { this.cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get classList() {
    const c = this.cls;
    return {
      add: (x) => c.add(x), remove: (x) => c.delete(x), contains: (x) => c.has(x),
      toggle: (x, force) => { const on = force === undefined ? !c.has(x) : !!force; if (on) { c.add(x); } else { c.delete(x); } return on; }
    };
  }
  get firstChild() { return this.kids[0] || null; }
  get textContent() { return this.text + this.kids.map((k) => k.textContent).join(''); }
  set textContent(v) { this.kids = []; this.text = String(v); }
  appendChild(k) { if (k.parentNode) { k.parentNode.removeChild(k); } k.parentNode = this; this.kids.push(k); return k; }
  removeChild(k) { this.kids.splice(this.kids.indexOf(k), 1); k.parentNode = null; return k; }
  replaceChild(n, o) { this.kids[this.kids.indexOf(o)] = n; n.parentNode = this; o.parentNode = null; return o; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  addEventListener(type, fn) { (this.on[type] = this.on[type] || []).push(fn); }
  contains(x) { for (let n = x; n; n = n.parentNode) { if (n === this) { return true; } } return false; }
  all() { return [this].concat(...this.kids.map((k) => k.all())); }
  querySelector(sel) { return this.all().slice(1).find((n) => n.tagName === sel) || null; }
  focus() {}
  click() { (this.on.click || []).forEach((fn) => fn({ currentTarget: this, preventDefault() {} })); }
}

function load() {
  const ids = ['img-chips', 'img-sum', 'img-list', 'img-sticky', 'img-stick-sentinel', 'img-seltrash', 'img-selcount',
    'img-selbar', 'tabbar', 'sheet', 'sheet-head', 'sheet-dialog'];
  const byId = {};
  ids.forEach((id) => { byId[id] = new Node('div'); });
  const roots = () => Object.values(byId);
  const calls = [];   // 发出去、还没放行的请求
  const sent = [];    // 发过的全部请求
  const toasts = [];
  const S = { sheet: null };

  function h(tag, attrs, ...kids) {
    const el = new Node(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v == null || v === false) { return; }
      if (k === 'class') { el.className = v; } else if (k === 'text') { el.text = String(v); } else if (k.startsWith('on') && typeof v === 'function') { el.addEventListener(k.slice(2), v); } else { el.setAttribute(k, v === true ? '' : v); }
    });
    const add = (list) => list.forEach((c) => {
      if (c == null || c === false) { return; }
      if (Array.isArray(c)) { add(c); } else if (typeof c === 'string' || typeof c === 'number') { const t = new Node('#text'); t.text = String(c); el.appendChild(t); } else { el.appendChild(c); }
    });
    add(kids);
    return el;
  }

  const lib = {
    h,
    icon: (name, size, cls, filled) => { const s = new Node('svg'); s.setAttribute('data-icon', name); if (filled) { s.setAttribute('data-filled', ''); } return s; },
    clear: (el) => { el.kids.forEach((k) => { k.parentNode = null; }); el.kids = []; el.text = ''; return el; },
    api: (method, url, body) => new Promise((resolve, reject) => {
      const call = { key: method + ' ' + url, method, url, body, resolve, reject };
      calls.push(call);
      sent.push(call);
    }),
    qs: (p) => '?' + Object.entries(p).filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&'),
    toast: (msg) => { toasts.push(msg); },
    focusKeys: () => [], focusByKeys: () => false, focusLost: () => false,
    stampLabel: (s) => s,
    parseMemDate: (s) => { const m = /^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?/.exec(String(s || '')); return m ? { y: +m[1], m: +m[2], d: m[3] ? +m[3] : 0 } : null; },
    curYear: () => 2026,
    confirmDialog: () => Promise.resolve(true),
    alertOpen: () => false
  };
  const sandbox = {
    Promise, Date, Object, Array, JSON, Math, String, Number, RegExp, isNaN, encodeURIComponent, setTimeout,
    document: {
      readyState: 'complete',
      activeElement: null,
      addEventListener() {},
      contains: (el) => roots().some((r) => r.contains(el)),
      querySelectorAll: (sel) => {
        const attr = /^\[([\w-]+)\]$/.exec(sel)[1];
        return [].concat(...roots().map((r) => r.all())).filter((n) => n.getAttribute(attr) !== null);
      }
    },
    scrollTo() {}
  };
  sandbox.window = sandbox;
  sandbox.EmberLib = lib;
  sandbox.EmberApp = { S, $: (id) => byId[id] };
  sandbox.EmberSheet = { show() {}, close() { S.sheet = null; }, requestClose() { S.sheet = null; } };
  vm.runInNewContext(SOURCE, sandbox);

  const env = {
    S, byId, sent, toasts,
    images: sandbox.EmberImages,
    /** 放行第一个对得上的请求（key 以 prefix 开头） */
    answer(prefix, data) { const call = env.take(prefix); call.resolve(data); return call; },
    take(prefix) {
      const i = calls.findIndex((c) => c.key.startsWith(prefix));
      assert.ok(i !== -1, 'no pending request ' + prefix + ' (pending: ' + calls.map((c) => c.key).join(', ') + ')');
      return calls.splice(i, 1)[0];
    },
    find(rootId, pick) { return byId[rootId].all().find(pick) || null; },
    findAll(rootId, pick) { return byId[rootId].all().filter(pick); },
    starIn(rootId, id) { return env.find(rootId, (n) => n.getAttribute('data-imgstar') === id); }
  };
  return env;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const plain = (x) => JSON.parse(JSON.stringify(x)); // 沙箱里造的对象原型不同，比内容就行
const STATS = { kept: { count: 1, bytes: 1000, starred: 0 }, trash: { count: 0, bytes: 0, latest: null }, trash_days: 30 };

function item(id, starred, extra) {
  return Object.assign({
    id, status: 'succeeded', created_at: '2026-11-07T06:00:00.000+00:00', model: null, model_label: 'Codex 订阅（gpt-image-2）',
    prompt_preview: '编的：' + id, starred, width: 64, height: 48, image_bytes: 1000
  }, extra || {});
}

function page(items) {
  return { items, total: items.length, page: 1, page_size: 60, has_more: false };
}

async function open(env, items) {
  env.images.show();
  env.answer('GET /api/images/stats', STATS);
  env.answer('GET /api/images?', page(items));
  await flush();
}

const A = '11111111-1111-4111-8111-111111111111';

test('a detail read sent before a ⭐ is saved does not undo the ⭐ when it lands after', async () => {
  const env = load();
  await open(env, [item(A, false)]);
  env.find('img-list', (n) => n.cls.has('imgopen')).click();     // 点开：大图面板发出详情 GET
  const detail = env.take('GET /api/images/' + A);                 // ……它先读到 starred=false，晚点再回
  env.starIn('sheet-head', A).click();                              // 在面板里点⭐
  const save = env.take('POST /api/images/' + A + '/star');
  assert.deepStrictEqual(plain(save.body), { starred: true });
  save.resolve({ job: item(A, true) });
  await flush();
  env.answer('GET /api/images/stats', STATS);
  await flush();
  detail.resolve({ job: item(A, false, { prompt: '编的：完整提示词' }) });  // 旧的读请求这时才回来
  await flush();
  const star = env.starIn('sheet-head', A);
  assert.strictEqual(star.getAttribute('aria-pressed'), 'true', 'the sheet must still show the ⭐ just saved');
  assert.strictEqual(env.starIn('img-list', A).getAttribute('aria-pressed'), 'true');
  star.click();                                                     // 再点一下 = 取消看得见的那颗⭐
  assert.deepStrictEqual(plain(env.take('POST /api/images/' + A + '/star').body), { starred: false });
});

test('a grid read sent before a ⭐ is saved does not undo it; a read sent after it is believed', async () => {
  const env = load();
  await open(env, [item(A, false)]);
  env.images.show();                                                // 再拉一遍：这次网格 GET 晚点回
  const grid = env.take('GET /api/images?');
  env.answer('GET /api/images/stats', STATS);
  env.starIn('img-list', A).click();
  env.take('POST /api/images/' + A + '/star').resolve({ job: item(A, true) });
  await flush();
  env.answer('GET /api/images/stats', STATS);
  grid.resolve(page([item(A, false)]));                             // 读在点⭐之前发出：不能把⭐盖掉
  await flush();
  assert.strictEqual(env.starIn('img-list', A).getAttribute('aria-pressed'), 'true');
  env.images.show();                                                // 点完以后才发出的读：小克刚好又取消了⭐，以它为准
  env.answer('GET /api/images/stats', STATS);
  env.answer('GET /api/images?', page([item(A, false)]));
  await flush();
  assert.strictEqual(env.starIn('img-list', A).getAttribute('aria-pressed'), 'false');
});

test('selection stops at 100, the batch the server accepts', async () => {
  const env = load();
  const items = Array.from({ length: 101 }, (_, i) => item('00000000-0000-4000-8000-' + String(i).padStart(12, '0'), false));
  await open(env, items);
  env.find('img-chips', (n) => n.cls.has('chip-act')).click();     // 选择
  env.findAll('img-list', (n) => n.cls.has('imgopen')).forEach((b) => b.click());
  assert.strictEqual(env.byId['img-selcount'].textContent, '已选 100 张');
  assert.ok(env.toasts.some((t) => /最多.*100/.test(t)), env.toasts.join(' | '));
  env.byId['img-seltrash'].click();
  assert.strictEqual(env.take('POST /api/images/trash').body.ids.length, 100);
});
