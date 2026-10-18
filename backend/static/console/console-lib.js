/* ember 记忆库网页 · 工具层：配色、拼 DOM、日期、调接口、提示条、确认弹窗。
   这个文件在 <head> 里同步加载：一加载就把配色挂到 <html> 上，免得闪一下默认色。
   规矩：服务器给的东西一律 textContent / createElement，绝不 innerHTML。 */
(function () {
  'use strict';

  // ---------- 配色（每台设备自己记） ----------
  var THEMES = ['matcha', 'pink', 'blue', 'night'];
  var THEME_LABELS = { matcha: '抹茶', pink: '粉粉', blue: '淡蓝', night: '夜间' };
  // 状态栏颜色（theme-color），跟 console.css 各主题的 --top 一致
  var THEME_BAR = { matcha: '#E5ECD5', pink: '#FBDDE5', blue: '#DDEBF9', night: '#14111B' };
  var THEME_KEY = 'ember-console-theme';

  function storedTheme() {
    try {
      var t = window.localStorage.getItem(THEME_KEY);
      if (t === 'warm') {
        t = 'matcha'; // 暖白橙已换成抹茶，之前选了它的设备接着用抹茶
      }
      return THEMES.indexOf(t) !== -1 ? t : null;
    } catch (e) {
      return null; // 无痕模式 / 禁了存储：用默认
    }
  }

  function defaultTheme() {
    try {
      if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        return 'night';
      }
    } catch (e) { /* 老浏览器 */ }
    return 'pink';
  }

  function applyTheme(t, persist) {
    if (THEMES.indexOf(t) === -1) {
      t = 'pink';
    }
    var root = document.documentElement;
    THEMES.forEach(function (x) { root.classList.remove('theme-' + x); });
    root.classList.add('theme-' + t);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      meta.setAttribute('content', THEME_BAR[t]);
    }
    if (persist) {
      try {
        window.localStorage.setItem(THEME_KEY, t);
      } catch (e) { /* 存不了就只在这一次生效 */ }
    }
    return t;
  }

  var currentTheme = applyTheme(storedTheme() || defaultTheme(), false);

  // ---------- 拼 DOM ----------
  function append(el, kids) {
    for (var i = 0; i < kids.length; i++) {
      var c = kids[i];
      if (c == null || c === false) {
        continue;
      }
      if (Array.isArray(c)) {
        append(el, c);
      } else if (typeof c === 'string' || typeof c === 'number') {
        el.appendChild(document.createTextNode(String(c)));
      } else {
        el.appendChild(c);
      }
    }
    return el;
  }

  /** h('button', {class: 'x', type: 'button', onclick: fn, text: '...'}, ...children) */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v == null || v === false) {
          return;
        }
        if (k === 'class') {
          el.className = v;
        } else if (k === 'text') {
          el.textContent = String(v);
        } else if (k.indexOf('on') === 0 && typeof v === 'function') {
          el.addEventListener(k.slice(2), v);
        } else {
          el.setAttribute(k, v === true ? '' : String(v));
        }
      });
    }
    return append(el, Array.prototype.slice.call(arguments, 2));
  }

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var ICONS = {
    x: ['M6 6l12 12M18 6L6 18'],
    plus: ['M12 5v14M5 12h14'],
    revert: ['M4 5v5h5M4.6 10A8 8 0 1 1 6 16.6'],
    chev: ['M6 9l6 6 6-6'],
    edit: ['M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4'],
    trash: ['M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10 11v5.5M14 11v5.5']
  };

  function icon(name, size, extraClass) {
    var s = document.createElementNS(SVG_NS, 'svg');
    s.setAttribute('class', 'ico' + (extraClass ? ' ' + extraClass : ''));
    s.setAttribute('width', String(size || 18));
    s.setAttribute('height', String(size || 18));
    s.setAttribute('viewBox', '0 0 24 24');
    s.setAttribute('aria-hidden', 'true');
    (ICONS[name] || []).forEach(function (d) {
      var p = document.createElementNS(SVG_NS, 'path');
      p.setAttribute('d', d);
      s.appendChild(p);
    });
    return s;
  }

  function clear(el) {
    while (el.firstChild) {
      el.removeChild(el.firstChild);
    }
    return el;
  }

  // ---------- 焦点：列表重画 / 面板关掉以后，焦点放回「同一个东西」上 ----------
  // 卡片、动静行带 data-key（只由编号拼成，如 m24 / a57）；行里的按钮另带 data-row = 这一行的 key。
  /** 记下 el 的 key，再记它所在行、前后各一个：重画后先找自己，没了找这一行，再找后一个 / 前一个 */
  function focusKeys(el) {
    var key = el && el.getAttribute ? el.getAttribute('data-key') : null;
    if (!key) {
      return [];
    }
    var keys = [key];
    if (el.getAttribute('data-row')) {
      keys.push(el.getAttribute('data-row'));
    }
    var view = el.closest('.view');
    var all = view ? Array.prototype.slice.call(view.querySelectorAll('[data-key]')) : [];
    var i = all.indexOf(el);
    if (i !== -1 && all[i + 1]) {
      keys.push(all[i + 1].getAttribute('data-key'));
    }
    if (i > 0) {
      keys.push(all[i - 1].getAttribute('data-key'));
    }
    return keys;
  }

  function focusByKeys(keys, scope) {
    for (var i = 0; i < keys.length; i++) {
      if (!/^[a-z0-9-]+$/.test(keys[i])) {
        continue;
      }
      var el = (scope || document).querySelector('[data-key="' + keys[i] + '"]');
      if (el && !el.disabled && el.getClientRects().length) {
        el.focus({ preventScroll: true });
        return true;
      }
    }
    return false;
  }

  /** 焦点是不是丢了（被删掉 / 变灰的元素上的焦点会掉回 body） */
  function focusLost() {
    var a = document.activeElement;
    return !a || a === document.body || a === document.documentElement;
  }

  // ---------- 日期（库里的时间都是北京时间） ----------
  var WK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  function bjDay(offsetDays) {
    var d = new Date(Date.now() + 8 * 3600e3 + (offsetDays || 0) * 86400e3);
    return d.toISOString().slice(0, 10);
  }
  function curYear() {
    return Number(bjDay(0).slice(0, 4));
  }
  function pad2(n) {
    n = Number(n);
    return (n < 10 ? '0' : '') + n;
  }

  /** '2026-10-17' / '2026-07' → {y, m, d(0 = 只记到月)} */
  function parseMemDate(s) {
    var m = /^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$/.exec(String(s || ''));
    if (!m) {
      return null;
    }
    return { y: Number(m[1]), m: Number(m[2]), d: m[3] ? Number(m[3]) : 0 };
  }

  /** 卡片上的大日期：10.17 / 7 月；不是今年的加上年份 */
  function dateBig(s) {
    var p = parseMemDate(s);
    if (!p) {
      return String(s || '');
    }
    var cur = p.y === curYear();
    if (p.d) {
      return cur ? (p.m + '.' + p.d) : (p.y + '.' + p.m + '.' + p.d);
    }
    return cur ? (p.m + ' 月') : (p.y + ' 年 ' + p.m + ' 月');
  }

  function weekday(s) {
    var p = parseMemDate(s);
    if (!p || !p.d) {
      return '只记到月';
    }
    return WK[new Date(Date.UTC(p.y, p.m - 1, p.d)).getUTCDay()];
  }

  /** 'YYYY-MM' → 10 月 / 2025 年 12 月 */
  function monthLabel(ym) {
    var p = parseMemDate(ym);
    if (!p) {
      return ym;
    }
    return (p.y === curYear() ? '' : p.y + ' 年 ') + p.m + ' 月';
  }

  function prevMonth(ym) {
    var p = parseMemDate(ym);
    var y = p.y;
    var m = p.m - 1;
    if (m < 1) {
      m = 12;
      y -= 1;
    }
    return y + '-' + pad2(m);
  }

  /** '2026-10-18 15:40:12' → '10.18 15:40'；relative 时今天 / 昨天写成字 */
  function stampLabel(at, relative) {
    var m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(at || ''));
    if (!m) {
      return String(at || '');
    }
    var hm = m[4] + ':' + m[5];
    var day = m[1] + '-' + m[2] + '-' + m[3];
    if (relative) {
      if (day === bjDay(0)) {
        return '今天 ' + hm;
      }
      if (day === bjDay(-1)) {
        return '昨天 ' + hm;
      }
    }
    var md = Number(m[2]) + '.' + Number(m[3]);
    return (Number(m[1]) === curYear() ? md : m[1] + '.' + md) + ' ' + hm;
  }

  /** 表单里填的日期 → 'YYYY-MM-DD' / 'YYYY-MM'，不合格返回 null */
  function normalizeInputDate(prec, raw) {
    var v = String(raw || '').trim().replace(/[./年月]/g, '-').replace(/日$/, '').replace(/-+$/, '');
    if (prec === 'month') {
      var mm = /^(\d{4})-(\d{1,2})$/.exec(v);
      if (!mm || Number(mm[2]) < 1 || Number(mm[2]) > 12) {
        return null;
      }
      return mm[1] + '-' + pad2(mm[2]);
    }
    var md = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(v);
    if (!md) {
      return null;
    }
    var y = Number(md[1]);
    var mo = Number(md[2]);
    var d = Number(md[3]);
    var dt = new Date(Date.UTC(y, mo - 1, d));
    if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) {
      return null;
    }
    return y + '-' + pad2(mo) + '-' + pad2(d);
  }

  // ---------- 标签 ----------
  function splitTags(s) {
    var out = [];
    String(s || '').split(/[,，、]/).forEach(function (t) {
      t = t.trim();
      if (t && out.indexOf(t) === -1) {
        out.push(t);
      }
    });
    return out;
  }
  function tagClass(t) {
    return t === '旧档' ? 'tag tag-old' : 'tag tag-diary';
  }

  // ---------- 调接口 ----------
  function ApiError(status, message, data) {
    this.status = status;
    this.message = message;
    this.data = data;
  }

  var DEFAULT_ERR = {
    0: '网络不太好，没连上',
    400: '填的东西不太对',
    404: '这条不在了，已刷新',
    409: '这条刚被改过，已刷新'
  };

  function api(method, path, body) {
    var opt = { method: method, credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    return fetch(path, opt).then(function (res) {
      if (res.status === 401) {
        // 登录过期：刷新整页，服务器会给登录页。这里不再往下走。
        window.location.reload();
        return new Promise(function () {});
      }
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok) {
          var msg = (data && data.error_description) || DEFAULT_ERR[res.status] || ('出错了（' + res.status + '）');
          throw new ApiError(res.status, msg, data);
        }
        return data;
      });
    }, function () {
      throw new ApiError(0, DEFAULT_ERR[0], null);
    });
  }

  function qs(params) {
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v !== undefined && v !== null && v !== '') {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
      }
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  // ---------- 提示条 ----------
  var toastTimer = null;
  function toast(msg) {
    var wrap = document.getElementById('toastwrap');
    if (!wrap || !msg) {
      return;
    }
    if (toastTimer) {
      clearTimeout(toastTimer);
    }
    clear(wrap);
    wrap.appendChild(h('span', { class: 'glass toast', text: msg }));
    toastTimer = setTimeout(function () {
      clear(wrap);
      toastTimer = null;
    }, Math.min(4000, 1900 + String(msg).length * 40));
  }

  // ---------- 确认弹窗 ----------
  var alertState = null;

  /** confirmDialog({title, msg, ok}) → Promise<boolean> */
  function confirmDialog(opt) {
    var layer = document.getElementById('alert-layer');
    if (alertState) {
      finishAlert(false);
    }
    document.getElementById('alert-title').textContent = opt.title || '';
    document.getElementById('alert-msg').textContent = opt.msg || '';
    document.getElementById('alert-ok').textContent = opt.ok || '确定';
    return new Promise(function (resolve) {
      alertState = { resolve: resolve, opener: document.activeElement };
      layer.hidden = false;
      setInertForModal();
      document.getElementById('alert-cancel').focus({ preventScroll: true });
    });
  }

  function finishAlert(ok) {
    if (!alertState) {
      return;
    }
    var st = alertState;
    alertState = null;
    document.getElementById('alert-layer').hidden = true;
    setInertForModal();
    if (st.opener && document.contains(st.opener) && typeof st.opener.focus === 'function') {
      st.opener.focus({ preventScroll: true });
    }
    st.resolve(ok);
  }

  function alertOpen() {
    return !!alertState;
  }

  /** 弹窗 / 面板开着时，后面的东西不让 Tab 和读屏摸到（inert） */
  function setInertForModal() {
    var alertOn = !!alertState;
    var sheetLayer = document.getElementById('sheet-layer');
    var sheetOn = sheetLayer && !sheetLayer.hidden;
    var bg = [document.getElementById('content'), document.getElementById('tabbar'), document.getElementById('fab')];
    bg.forEach(function (el) {
      if (el) {
        el.inert = alertOn || sheetOn;
      }
    });
    if (sheetLayer) {
      sheetLayer.inert = alertOn;
    }
    document.documentElement.classList.toggle('locked', alertOn || sheetOn);
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('alert-cancel').addEventListener('click', function () { finishAlert(false); });
    document.getElementById('alert-ok').addEventListener('click', function () { finishAlert(true); });
    document.getElementById('alert-layer').addEventListener('click', function (e) {
      if (e.target === e.currentTarget) {
        finishAlert(false);
      }
    });
    document.getElementById('alert-layer').addEventListener('keydown', function (e) {
      if (e.key !== 'Tab') {
        return;
      }
      // 两个按钮之间来回
      var a = document.getElementById('alert-cancel');
      var b = document.getElementById('alert-ok');
      e.preventDefault();
      (document.activeElement === a ? b : a).focus();
    });
  });

  window.EmberLib = {
    THEMES: THEMES,
    THEME_LABELS: THEME_LABELS,
    getTheme: function () { return currentTheme; },
    setTheme: function (t) { currentTheme = applyTheme(t, true); return currentTheme; },
    h: h,
    icon: icon,
    clear: clear,
    focusKeys: focusKeys,
    focusByKeys: focusByKeys,
    focusLost: focusLost,
    bjDay: bjDay,
    curYear: curYear,
    pad2: pad2,
    parseMemDate: parseMemDate,
    dateBig: dateBig,
    weekday: weekday,
    monthLabel: monthLabel,
    prevMonth: prevMonth,
    stampLabel: stampLabel,
    normalizeInputDate: normalizeInputDate,
    splitTags: splitTags,
    tagClass: tagClass,
    api: api,
    qs: qs,
    toast: toast,
    confirmDialog: confirmDialog,
    finishAlert: finishAlert,
    alertOpen: alertOpen,
    setInertForModal: setInertForModal
  };
})();
