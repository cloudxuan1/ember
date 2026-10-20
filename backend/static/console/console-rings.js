/* ember 记忆库网页 · 面板里的年轮：细线时间线（老的在上，最新的点是实心），每圈右边一支笔改 / 删，
   线的尽头补一圈。补 / 改都在面板里就地写，不另开一层。
   状态在 S.sheet.ring（同一时间只开一个：{id（null = 补一圈）, prec, date, text, orig, busy, err}）。
   服务器给的字一律 textContent，不用 innerHTML。 */
(function () {
  'use strict';

  var L = window.EmberLib;
  var App = window.EmberApp;
  var S = App.S;
  var h = L.h;
  var icon = L.icon;
  var api = L.api;
  var toast = L.toast;

  var FLASH_MS = 2200;
  var SPOT_MS = 3400;

  /** 底部面板在 console-sheet.js（用到时再取） */
  function Sheet() {
    return window.EmberSheet;
  }

  /** 这张卡的圈（从早到晚）。没有圈的老数据当成只有一圈，不能改圈 */
  function ringsOf(sh) {
    var m = sh.mem;
    return m.rings && m.rings.length ? m.rings : [{ id: null, date: m.date, content: m.content }];
  }

  /** 头上亮哪个点：从某个月点进来就是那一圈，不然最新一圈 */
  function litIndex(sh, rs) {
    for (var i = 0; i < rs.length; i++) {
      if (sh.spot != null && rs[i].id === sh.spot) {
        return i;
      }
    }
    return rs.length - 1;
  }

  function timeline(sh) {
    var rs = ringsOf(sh);
    var ed = sh.ring;
    var ol = h('ol', { class: 'rail', 'aria-label': '年轮，从早到晚，共 ' + rs.length + ' 圈' });
    rs.forEach(function (r, i) {
      var editing = !!ed && ed.id != null && ed.id === r.id;
      var cls = 'ring' + (i === rs.length - 1 ? ' ring-new' : '') +
        (sh.spotOn && sh.spot === r.id ? ' ring-spot' : '') + (sh.flash === r.id ? ' ring-flash' : '');
      ol.appendChild(h('li', { class: cls, 'data-ring': String(r.id) },
        h('span', { class: 'rdotb', 'aria-hidden': 'true' }),
        editing ? editor(sh) : h('div', { class: 'rbody' },
          h('div', { class: 'rtop' },
            h('span', { class: 'rdate', text: L.dateLong(r.date) }),
            r.id != null ? h('button', {
              class: 'press rmore', type: 'button', disabled: !sh.full, 'data-key': 'r' + Number(r.id),
              'aria-label': '改 ' + L.dateLong(r.date) + ' 这一圈',
              onclick: function () { openEditor(sh, newEd(r)); }
            }, icon('edit', 16)) : null),
          h('p', { class: 'rtext', text: r.content }))));
    });
    var adding = !!ed && ed.id == null;
    var add = h('div', { class: 'addrow' }, adding
      ? [h('span', { class: 'adddot adddot-live', 'aria-hidden': 'true' }), editor(sh)]
      : h('button', {
        class: 'press addbtn', type: 'button', disabled: !sh.full || rs[0].id == null, 'data-key': 'r-add',
        onclick: function () { openEditor(sh, newEd(null)); }
      }, h('span', { class: 'adddot', 'aria-hidden': 'true' }), '补一圈'));
    return h('div', { class: 'tl' },
      h('div', { class: 'tlh' }, h('span', { text: '年轮' }), L.ringDots(rs.length, litIndex(sh, rs), 'rdots tlh-dots')),
      ol, add);
  }

  // =====================================================================
  // 补一圈 / 改这一圈
  // =====================================================================
  function newEd(r) {
    var date = r ? r.date : L.bjDay(0);
    var p = L.parseMemDate(date);
    var text = r ? r.content : '';
    return { id: r ? r.id : null, prec: p && !p.d ? 'month' : 'day', date: date, text: text, orig: { date: date, text: text }, busy: false, err: '', refs: {} };
  }

  function dirty(sh) {
    var e = sh && sh.ring;
    return !!e && (e.text !== e.orig.text || e.date !== e.orig.date);
  }

  /** 开一个编辑框；另一个正写着、有改动，先问一句 */
  function openEditor(sh, ed) {
    var go = function () {
      if (S.sheet !== sh) {
        return;
      }
      sh.ring = ed;
      sh.spotOn = false;
      Sheet().renderSheet();
      var t = ed.refs.text;
      if (t) {
        t.focus({ preventScroll: true });
        t.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    };
    if (!dirty(sh)) {
      go();
      return;
    }
    L.confirmDialog({ title: '刚写的不要了？', msg: '那一圈还没存。', ok: '不要了' }).then(function (ok) {
      if (ok) {
        go();
      }
    });
  }

  function closeEditor(sh, focusKey) {
    sh.ring = null;
    Sheet().renderSheet();
    var el = focusKey && document.querySelector('#sheet [data-key="' + focusKey + '"]');
    if (el && !el.disabled) {
      el.focus({ preventScroll: true });
    }
  }

  function todayFor(prec) {
    var d = L.bjDay(0);
    return prec === 'month' ? d.slice(0, 7) : d;
  }

  function editor(sh) {
    var e = sh.ring;
    var r = e.refs = {};
    var isNew = e.id == null;
    var label = isNew ? '补一圈' : '改这一圈';
    r.date = h('input', { class: 'field', type: e.prec === 'month' ? 'month' : 'date', max: todayFor(e.prec), 'aria-label': '这一圈的日子' });
    r.date.value = e.date;
    var onDate = function () {
      e.date = r.date.value;
      setErr(e, '');
    };
    r.date.addEventListener('input', onDate);
    r.date.addEventListener('change', onDate);
    r.prec = h('button', {
      class: 'press toggle' + (e.prec === 'month' ? ' toggle-on' : ''), type: 'button', 'aria-pressed': e.prec === 'month' ? 'true' : 'false',
      onclick: function () { togglePrec(e); }
    }, h('span', { class: 'tick', 'aria-hidden': 'true' }, icon('tick', 12)), '只记到月');
    r.text = h('textarea', { class: 'field ta', rows: '3', placeholder: '这一圈发生了什么？', 'aria-label': '这一圈的内容' });
    r.text.value = e.text;
    r.text.addEventListener('input', function () {
      e.text = r.text.value;
      grow(r.text);
      setErr(e, '');
    });
    requestAnimationFrame(function () { grow(r.text); });
    r.err = h('span', { class: 'err', role: 'alert' });
    r.save = h('button', { class: 'press btn-primary btn-sm', type: 'button', onclick: function () { save(sh); } }, '存');
    var kids = [
      h('span', { class: 'edlabel', text: label }),
      h('div', { class: 'edrow' }, r.date, r.prec),
      r.text,
      isNew ? h('span', { class: 'fhint', text: '日子写事情发生的那天，不能写以后；打算哪天做，写进内容里。' }) : null,
      r.err,
      h('div', { class: 'edbtns' },
        isNew ? null : h('button', { class: 'press btn-danger btn-sm', type: 'button', onclick: function () { del(sh); } }, '删这一圈'),
        h('span', { class: 'edsp' }),
        h('button', {
          class: 'press btn-ghost btn-sm', type: 'button',
          onclick: function () { closeEditor(sh, isNew ? 'r-add' : 'r' + Number(e.id)); }
        }, '取消'),
        r.save)
    ];
    setErr(e, e.err);
    setBusy(e, e.busy);
    return h('div', { class: 'ed', role: 'group', 'aria-label': label }, kids);
  }

  /** 写多长框就多高（最高 60% 屏幕，再长就在框里滚） */
  function grow(ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight + 2, Math.round(window.innerHeight * 0.6)) + 'px';
  }

  function setErr(e, msg) {
    e.err = msg;
    if (e.refs.err) {
      e.refs.err.textContent = msg;
      e.refs.err.hidden = !msg;
    }
  }

  // 正在存：aria-disabled 变灰（save 自己挡重复点）；用 disabled 的话存失败后键盘焦点就丢了
  function setBusy(e, busy) {
    e.busy = busy;
    if (e.refs.save) {
      e.refs.save.setAttribute('aria-disabled', busy ? 'true' : 'false');
      e.refs.save.textContent = busy ? '正在存……' : '存';
    }
  }

  function togglePrec(e) {
    var toMonth = e.prec !== 'month';
    var d = String(e.date || '');
    e.prec = toMonth ? 'month' : 'day';
    e.date = toMonth ? d.slice(0, 7) : (d.length === 7 ? d + '-01' : d);
    var r = e.refs;
    r.date.type = toMonth ? 'month' : 'date';
    r.date.max = todayFor(e.prec);
    r.date.value = e.date;
    r.prec.classList.toggle('toggle-on', toMonth);
    r.prec.setAttribute('aria-pressed', toMonth ? 'true' : 'false');
    setErr(e, '');
  }

  /** 存之前先在页面上挑一遍：没写字、没选日子、写了以后的日子 */
  function check(e) {
    var date = L.normalizeInputDate(e.prec, e.date);
    if (!e.text.trim()) {
      return { err: e.id == null ? '这一圈写点什么再存' : '这一圈不能改成空的，不要了就删掉它' };
    }
    if (!date) {
      return { err: e.prec === 'month' ? '还没选是哪个月' : '还没选是哪天' };
    }
    if (date > todayFor(e.prec)) {
      return { err: '不能写以后的日子，打算哪天做写进内容里' };
    }
    return { date: date };
  }

  /** 关面板（点叉、点背景、Esc）时这一圈还开着、有改动：写好了就先存再关；没写好（空的、没选日子、以后的日子）问一句 */
  function saveThenClose(sh, close) {
    var e = sh.ring;
    if (e.busy) {
      return;
    }
    var c = check(e);
    if (c.err) {
      L.confirmDialog({ title: '这一圈还没写好', msg: c.err + '。现在关掉，刚写的就不要了。', ok: '不要了' }).then(function (ok) {
        if (ok && S.sheet === sh) {
          close();
        }
      });
      return;
    }
    save(sh, close);
  }

  /** after：存好以后再做的事（关面板时用） */
  function save(sh, after) {
    var e = sh.ring;
    if (!e || e.busy) {
      return;
    }
    var c = check(e);
    if (c.err) {
      setErr(e, c.err);
      return;
    }
    setBusy(e, true);
    var body = { date: c.date, content: e.text.trim(), expect_updated_at: sh.mem.updated_at || null };
    var req = e.id == null
      ? api('POST', '/api/memories/' + sh.id + '/rings', body)
      : api('PATCH', '/api/memories/' + sh.id + '/rings/' + e.id, body);
    req.then(function (data) {
      var msg = e.id == null ? '补好了一圈' : (data.changed === false ? '没有改动' : '这一圈改好了');
      done(sh, e, data, data.ring_id, msg);
      if (after && S.sheet === sh) {
        after();
      }
    }, function (err) {
      failed(sh, e, err);
    });
  }

  function del(sh) {
    var e = sh.ring;
    if (!e || e.id == null || e.busy) {
      return;
    }
    if (ringsOf(sh).length <= 1) {
      setErr(e, '这是最后一圈了，要删就删整张卡');
      return;
    }
    setBusy(e, true);
    var ringId = e.id;
    api('DELETE', '/api/memories/' + sh.id + '/rings/' + ringId, { expect_updated_at: sh.mem.updated_at || null }).then(function (data) {
      // 防手滑：提示条上带「撤回」，点了就退回删之前那一版（这一圈连同日子、内容原样回来）
      done(sh, e, data, null, '删掉了这一圈', { label: '撤回', run: function () { undoDelete(sh.id, ringId, data); } });
    }, function (err) {
      failed(sh, e, err);
    });
  }

  function undoDelete(id, ringId, data) {
    api('POST', '/api/memories/' + id + '/revert', {
      history_id: data.history_id, expect_updated_at: data.memory.updated_at
    }).then(function (back) {
      toast('撤回了，这一圈回来了');
      var sh = S.sheet;
      if (sh && sh.id === id && sh.mode === 'view') {
        sh.mem = back.memory;
        sh.full = true;
        sh.req++;
        sh.flash = ringId;
        Sheet().renderSheet();
        setTimeout(function () {
          if (sh.flash === ringId) {
            sh.flash = null;
          }
        }, FLASH_MS);
      }
      App.afterChange(false);
    }, function (err) {
      toast(err.status === 409 ? '这张卡后来又改过，撤回不了；最近动静里还能回退' : err.message);
      App.afterChange(null);
    });
  }

  /** 存好 / 删好：面板换成新的，那一圈闪一下，后面的列表跟着变（卡的日子 = 最新一圈，混着来的位置也重排） */
  function done(sh, e, data, flashId, msg, action) {
    toast(msg, action);
    if (S.sheet === sh && sh.ring === e) {
      sh.mem = data.memory;
      sh.full = true;
      sh.req++;
      sh.ring = null;
      sh.flash = flashId;
      Sheet().renderSheet();
      var el = document.querySelector('#sheet [data-key="' + (flashId != null ? 'r' + Number(flashId) : 'r-add') + '"]');
      if (el && !el.disabled) {
        el.focus({ preventScroll: true });
      }
      setTimeout(function () {
        if (sh.flash === flashId) {
          sh.flash = null; // 不重画：动画自己播完，之后重画时不再闪
        }
      }, FLASH_MS);
    }
    App.afterChange(false);
  }

  function failed(sh, e, err) {
    setBusy(e, false);
    if (S.sheet !== sh || sh.ring !== e) {
      toast(err.message);
      return;
    }
    if (err.status === 409) {
      // 等的时候有人改了（多半是小克）：不覆盖。写的字留着，面板拿最新的，再点一次存就好
      setErr(e, '这张卡刚被改过，已刷新；你写的还在，再存一次就好');
      Sheet().fetchDetail();
      App.afterChange(false);
      return;
    }
    if (err.status === 404) {
      toast(err.message);
      sh.ring = null;
      Sheet().fetchDetail();
      App.afterChange(false);
      return;
    }
    setErr(e, err.message);
  }

  /** 面板第一次画好以后：从某个月点进来，滚到那一圈、闪一下；过一会儿拿掉，之后重画不再闪 */
  function scrollToSpot(sh, scroller) {
    if (!sh.needScroll) {
      return;
    }
    var li = sh.spot != null && scroller.querySelector('[data-ring="' + Number(sh.spot) + '"]');
    if (!li) {
      return;
    }
    sh.needScroll = false;
    scroller.scrollTop = Math.max(0, li.offsetTop - 120);
    setTimeout(function () {
      sh.spotOn = false;
    }, SPOT_MS);
  }

  window.EmberRings = {
    timeline: timeline,
    dirty: dirty,
    saveThenClose: saveThenClose,
    scrollToSpot: scrollToSpot
  };
})();
