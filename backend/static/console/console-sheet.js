/* ember 记忆库网页 · 底部面板：点开一张卡看年轮 / 上一版 / 回退 / 清掉 / 删除，改标题和便利贴，右下角 + 新写一张。
   年轮那一段（补圈 / 改圈 / 删圈）在 console-rings.js；往上推展开到顶在 console-sheet-drag.js。
   状态都在 console.js 的 S 里（S.sheet）。服务器给的字一律 textContent，不用 innerHTML。 */
(function () {
  'use strict';

  var L = window.EmberLib;
  var App = window.EmberApp;
  var S = App.S;
  var $ = App.$;
  var h = L.h;
  var icon = L.icon;
  var api = L.api;
  var toast = L.toast;
  var whoName = App.whoName;
  var afterChange = App.afterChange;

  var TOP_TAGS = 12;

  function Rings() {
    return window.EmberRings;
  }
  function Drag() {
    return window.EmberSheetDrag;
  }

  // =====================================================================
  // 底部面板：开 / 关
  // =====================================================================
  function showSheet() {
    var layer = $('sheet-layer');
    if (S.closeTimer) {
      clearTimeout(S.closeTimer);
      S.closeTimer = null;
    }
    layer.classList.remove('closing');
    layer.hidden = false;
    L.setInertForModal();
    if (Drag()) {
      Drag().reset(); // 每次打开都从半高开始
    }
    $('sheet').scrollTop = 0;
    $('sheet-dialog').focus({ preventScroll: true });
  }

  function closeSheet() {
    var sh = S.sheet;
    if (!sh) {
      return;
    }
    S.sheet = null;
    var layer = $('sheet-layer');
    layer.classList.add('closing');
    S.closeTimer = setTimeout(function () {
      S.closeTimer = null;
      layer.hidden = true;
      layer.classList.remove('closing');
      L.clear($('sheet'));
      L.setInertForModal();
      returnFocus(sh);
    }, 230);
  }

  /** 关掉后焦点回到点开它的那张卡片 / 那一行；列表重画过就找同一条的新卡片，删掉了就找旁边那张 */
  function returnFocus(sh) {
    if (L.alertOpen()) {
      return;
    }
    var op = sh.opener;
    if (op && document.contains(op) && !op.disabled && typeof op.focus === 'function') {
      op.focus({ preventScroll: true });
      return;
    }
    if (L.focusByKeys(sh.focusKeys || [])) {
      return;
    }
    var title = document.querySelector('.view:not([hidden]) .pagetitle');
    if (title) {
      title.focus({ preventScroll: true });
    }
  }

  function editDirty(sh) {
    var ed = sh && sh.ed;
    if (sh && sh.mode === 'view') {
      return Rings().dirty(sh);
    }
    if (!ed || (sh.mode !== 'edit' && sh.mode !== 'new')) {
      return false;
    }
    return ed.title !== ed.orig.title || ed.text !== ed.orig.text || ed.date !== ed.orig.date || ed.sel.join(',') !== ed.orig.tags;
  }

  /** 点遮罩 / 按 Esc：正在写的有改动就先问一句 */
  function requestClose() {
    var sh = S.sheet;
    if (!sh) {
      return;
    }
    if (sh.mode === 'view' && Rings().dirty(sh)) {
      Rings().saveThenClose(sh, closeSheet); // 关面板 = 存：写了一半的那一圈先存上再关（design-rules 踩坑 4）
      return;
    }
    if (!editDirty(sh)) {
      closeSheet();
      return;
    }
    L.confirmDialog({ title: '不保存了？', msg: '刚写的内容会丢掉。', ok: '不保存' }).then(function (ok) {
      if (ok && S.sheet === sh) {
        closeSheet();
      }
    });
  }

  /** spot = 从某个月 / 搜索点进来时要滚到、闪一下的那一圈（编号），没有就 null */
  function openDetail(id, listItem, opener, spot) {
    S.sheet = {
      mode: 'view', id: id, mem: listItem || null, full: false, prevOpen: false, opener: opener || null, focusKeys: L.focusKeys(opener),
      req: 0, busy: false, ring: null, spot: spot == null ? null : spot, spotOn: spot != null, needScroll: spot != null, flash: null
    };
    showSheet();
    renderSheet();
    fetchDetail();
  }

  function fetchDetail() {
    var sh = S.sheet;
    if (!sh || sh.id == null) {
      return;
    }
    var req = ++sh.req;
    api('GET', '/api/memories/' + sh.id).then(function (mem) {
      if (S.sheet !== sh || req !== sh.req) {
        return;
      }
      sh.mem = mem;
      sh.full = true;
      sh.failed = false;
      if (sh.mode === 'view') {
        renderSheet();
      }
    }, function (err) {
      if (S.sheet !== sh || req !== sh.req) {
        return;
      }
      toast(err.message);
      if (err.status === 404 || !sh.mem) {
        closeSheet();
        if (err.status === 404) {
          afterChange(null);
        }
      } else if (sh.mode === 'view') {
        sh.failed = true; // 列表里的内容还能看，只是上一版没拉到
        renderSheet();
      }
    });
  }

  function closeBtn(label, fn) {
    return h('button', { class: 'press closebtn', type: 'button', 'aria-label': label, onclick: fn }, icon('x', 16));
  }

  function renderSheet() {
    var sh = S.sheet;
    var el = $('sheet');
    var top = el.scrollTop; // 重画（拉完全文、存好一圈…）不让面板跳回顶上
    var a = document.activeElement;
    var inEd = sh && sh.ring && a && a.closest && a.closest('.ed') ? ({ TEXTAREA: 'text', INPUT: 'date' }[a.tagName] || 'save') : null;
    L.clear(el);
    renderHead(sh);
    if (!sh) {
      return;
    }
    if (sh.mode === 'view') {
      $('sheet-dialog').setAttribute('aria-label', (sh.mem && sh.mem.title ? '「' + sh.mem.title + '」' : '卡 #' + sh.id) + ' · 年轮');
      renderView(el, sh);
      el.scrollTop = top;
      if (sh.mem) {
        Rings().scrollToSpot(sh, el);
      }
      if (inEd && sh.ring && sh.ring.refs[inEd]) {
        sh.ring.refs[inEd].focus({ preventScroll: true }); // 正写着一圈，面板在底下刷新了：焦点留在原处
      }
    } else {
      $('sheet-dialog').setAttribute('aria-label', sh.mode === 'new' ? '写一张新卡' : '改 #' + sh.id + ' 的标题和便利贴');
      renderEdit(el, sh);
    }
    // 点的按钮被重画掉了（编辑 / 保存 / 回退 / 清掉…）：焦点留在面板上，别掉回页面开头
    if (L.focusLost()) {
      $('sheet-dialog').focus({ preventScroll: true });
    }
  }

  // =====================================================================
  // 底部面板：看一条
  // =====================================================================
  function verMeta(v) {
    return L.stampLabel(v.replaced_at, false) + ' 被' + whoName(v.replaced_by) + (v.action === 'revert' ? '换下来的' : '改掉的');
  }

  function verBlock(sh, v, i, n) {
    var m = sh.mem;
    var kids = [];
    var which = i === 0 ? '较新的一版' : '更早的一版';
    if (n > 1) {
      kids.push(h('span', { class: 'prevmeta', text: which + ' · ' + verMeta(v) }));
    }
    if ('title' in v && (v.title || '') !== (m.title || '')) {
      kids.push(h('span', { class: 'prevline', text: '那一版的标题：' + (v.title || '没写标题') }));
    }
    if (v.date !== m.date) {
      kids.push(h('span', { class: 'prevline', text: '那一版的日子：' + L.dateBig(v.date) }));
    }
    var vt = L.splitTags(v.tags);
    if (vt.join(',') !== L.splitTags(m.tags).join(',')) {
      kids.push(h('span', { class: 'prevline', text: '那一版的标签：' + (vt.length ? vt.join('、') : '没有标签') }));
    }
    var same = v.content === m.content;
    kids.push(h('span', { class: 'prevtext' + (same ? ' prevtext-same' : ''), text: same ? '内容和现在一样' : v.content }));
    kids.push(h('div', { class: 'prevbtns' },
      h('button', {
        class: 'press btn-soft', type: 'button', 'aria-label': n > 1 ? '回退到' + which : '回退到上一版',
        onclick: function () { revertTo(sh, v); }
      }, icon('revert', 16), '回退'),
      h('button', {
        class: 'press btn-ghost', type: 'button', 'aria-label': n > 1 ? '清掉' + which : '清掉上一版',
        onclick: function () { clearVersion(sh, v); }
      }, '清掉')));
    return h('div', { class: 'prevver' }, kids);
  }

  function prevBox(sh) {
    var box = h('div', { class: 'prevbox' });
    if (!sh.full) {
      box.appendChild(h('div', { class: 'prevnone' }, icon('revert', 18),
        h('span', { text: sh.failed ? '上一版没拉到，关掉再点开试试' : '正在看有没有上一版……' })));
      return box;
    }
    var hist = sh.mem.history || [];
    if (!hist.length) {
      box.appendChild(h('div', { class: 'prevnone' }, icon('revert', 18), h('span', { text: '没有上一版' })));
      return box;
    }
    var open = sh.prevOpen;
    var meta = hist.length === 1 ? verMeta(hist[0]) : ('留着 ' + hist.length + ' 版，点开挑退到哪一版');
    box.appendChild(h('button', {
      class: 'press prevhead', type: 'button', 'aria-expanded': open ? 'true' : 'false', 'aria-controls': 'prevbody',
      onclick: function () {
        sh.prevOpen = !sh.prevOpen;
        renderSheet();
        var head = $('sheet').querySelector('.prevhead');
        if (head) {
          head.focus({ preventScroll: true });
        }
      }
    },
    icon('revert', 18),
    h('span', { class: 'prevhead-t' },
      h('span', { class: 'prevhead-title', text: '上一版' }),
      h('span', { class: 'prevhead-meta', text: meta })),
    icon('chev', 18, open ? 'chev chev-open' : 'chev')));
    if (open) {
      box.appendChild(h('div', { class: 'prevbody', id: 'prevbody' }, hist.map(function (v, i) {
        return verBlock(sh, v, i, hist.length);
      })));
    }
    return box;
  }

  /** 面板顶上不跟着滚的那一条：标题、⭐、关闭（只在看卡时有；编辑 / 新写自己带标题行） */
  function renderHead(sh) {
    var head = L.clear($('sheet-head'));
    head.hidden = !sh || sh.mode !== 'view';
    if (head.hidden) {
      return;
    }
    var m = sh.mem;
    var titled = !!(m && m.title);
    head.appendChild(h('h2', { class: titled ? 'stitle' : 'stitle stitle-none', text: titled ? m.title : (m ? '没写标题' : '#' + sh.id) }));
    if (m) {
      head.appendChild(App.starBtn(m, 'shead-star'));
    }
    head.appendChild(closeBtn('关闭', requestClose));
  }

  /** '2026-10-18 15:40:12' → 10 月 18 日（不是今年的加上年份） */
  function dayLabel(at) {
    var p = L.parseMemDate(String(at || '').slice(0, 10));
    if (!p || !p.d) {
      return '';
    }
    return (p.y === L.curYear() ? '' : p.y + ' 年 ') + p.m + ' 月 ' + p.d + ' 日';
  }

  /** 「来自」那一行：老记忆写来源文件 / 小节；没有来源写哪天存进来的 */
  function fromLine(m) {
    var refs = (m.sources || []).map(function (x) { return String(x.source_ref || '').trim(); }).filter(Boolean);
    if (refs.length) {
      return h('span', { class: 'from' }, icon('doc', 16), h('span', { text: '来自：' + refs.join('、') }));
    }
    var day = dayLabel(m.created_at);
    return day ? h('span', { class: 'from' }, icon('chat', 16), h('span', { text: '来自：' + day + '存进来的' })) : null;
  }

  function renderView(el, sh) {
    var m = sh.mem;
    if (!m) {
      el.appendChild(h('span', { class: 'loading', text: '正在打开……' }));
      return;
    }
    var tags = L.splitTags(m.tags);
    el.appendChild(h('div', { class: 'smeta' },
      h('span', { class: 'tag tag-drawer', text: m.drawer || '日常' }),
      h('span', { class: 'tag tag-no', text: '#' + m.id }),
      sh.full ? fromLine(m) : null));
    if (tags.length) {
      el.appendChild(h('div', { class: 'tags stags' }, tags.map(function (t) { return h('span', { class: L.tagClass(t), text: t }); })));
    }
    el.appendChild(Rings().timeline(sh));
    el.appendChild(prevBox(sh));
    el.appendChild(h('div', { class: 'grid2' },
      h('button', { class: 'press btn-soft bigbtn', type: 'button', disabled: !sh.full, onclick: function () { startEdit(sh); } }, icon('edit', 18), '标题和便利贴'),
      h('button', { class: 'press btn-danger bigbtn', type: 'button', onclick: function () { askDelete(sh); } }, icon('trash', 18), '删整张卡')));
  }

  function revertTo(sh, v) {
    if (sh.busy) {
      return;
    }
    sh.busy = true;
    api('POST', '/api/memories/' + sh.id + '/revert', {
      history_id: v.id, expect_updated_at: sh.mem.updated_at
    }).then(function (data) {
      sh.busy = false;
      toast('已回退到这一版');
      if (S.sheet === sh) {
        sh.mem = data.memory;
        sh.full = true;
        sh.prevOpen = false;
        sh.req++;
        renderSheet();
      }
      afterChange(false);
    }, function (err) {
      sh.busy = false;
      toast(err.message);
      if (S.sheet === sh) {
        fetchDetail();
      }
    });
  }

  function clearVersion(sh, v) {
    if (sh.busy) {
      return;
    }
    sh.busy = true;
    api('DELETE', '/api/memories/' + sh.id + '/history' + L.qs({ history_id: v.id })).then(function () {
      sh.busy = false;
      toast('那一版清掉了');
      afterChange(sh.id);
    }, function (err) {
      sh.busy = false;
      toast(err.message);
      if (S.sheet === sh) {
        fetchDetail();
      }
    });
  }

  function askDelete(sh) {
    var days = S.settings ? S.settings.activity_days : 30;
    L.confirmDialog({
      title: '删掉整张卡 #' + sh.id + '？',
      msg: '连同所有的圈一起删。删掉以后，' + days + ' 天内还能在最近动静里恢复。',
      ok: '删除'
    }).then(function (ok) {
      if (!ok || S.sheet !== sh) {
        return;
      }
      var id = sh.id;
      api('DELETE', '/api/memories/' + id).then(function () {
        toast('删掉了整张卡', { label: '撤回', run: function () { undoDeleteCard(id); } });
        if (S.sheet === sh) {
          closeSheet();
        }
        afterChange(null);
      }, function (err) {
        toast(err.message);
        if (err.status === 404 && S.sheet === sh) {
          closeSheet();
          afterChange(null);
        }
      });
    });
  }

  /** 防手滑：删整张卡后提示条上的「撤回」，跟最近动静里的「恢复」是同一件事 */
  function undoDeleteCard(id) {
    api('POST', '/api/memories/' + id + '/restore').then(function () {
      toast('撤回了，#' + id + ' 回来了');
      afterChange(null);
    }, function (err) {
      toast(err.message);
      afterChange(null);
    });
  }

  // =====================================================================
  // 底部面板：编辑 / 新写
  // =====================================================================
  function newEd(title, date, text, tags, expect) {
    var p = L.parseMemDate(date);
    var sel = L.splitTags(tags);
    return {
      title: title || '',
      prec: p && !p.d ? 'month' : 'day',
      date: date || '',
      text: text || '',
      sel: sel.slice(),
      cands: sel.slice(),
      expect: expect,
      busy: false,
      orig: { title: title || '', date: date || '', text: text || '', tags: sel.join(',') },
      refs: {}
    };
  }

  /** 编辑整张卡只改标题和便利贴：日子和内容在年轮里一圈一圈改 */
  function startEdit(sh) {
    if (!sh.mem || !sh.full) {
      return;
    }
    if (Rings().dirty(sh)) {
      L.confirmDialog({ title: '刚写的不要了？', msg: '那一圈还没存。', ok: '不要了' }).then(function (ok) {
        if (ok && S.sheet === sh) {
          sh.ring = null;
          startEdit(sh);
        }
      });
      return;
    }
    sh.ring = null;
    sh.ed = newEd(sh.mem.title, sh.mem.date, sh.mem.content, sh.mem.tags, sh.mem.updated_at || null);
    sh.mode = 'edit';
    sh.prevOpen = false;
    renderSheet();
    $('sheet').scrollTop = 0;
    loadTagCands(sh);
  }

  function startNew(opener) {
    S.sheet = { mode: 'new', id: null, mem: null, full: false, prevOpen: false, opener: opener || null, req: 0, busy: false, ring: null };
    S.sheet.ed = newEd('', L.bjDay(0), '', '', null);
    showSheet();
    renderSheet();
    loadTagCands(S.sheet);
  }

  function cancelEdit() {
    var sh = S.sheet;
    if (!sh) {
      return;
    }
    if (sh.mode === 'new') {
      closeSheet();
      return;
    }
    sh.mode = 'view';
    sh.ed = null;
    renderSheet();
  }

  /** 标签候选：这条自己的 + 用得最多的前 12 个 */
  function loadTagCands(sh) {
    var ready = S.tags ? Promise.resolve(S.tags) : api('GET', '/api/tags').then(function (d) {
      S.tags = d.tags || [];
      return S.tags;
    });
    ready.then(function (tags) {
      if (S.sheet !== sh || !sh.ed) {
        return;
      }
      tags.slice(0, TOP_TAGS).forEach(function (t) {
        if (sh.ed.cands.indexOf(t.tag) === -1) {
          sh.ed.cands.push(t.tag);
        }
      });
      renderTagChips(sh);
    }, function () { /* 拿不到常用标签也能手动加 */ });
  }

  function renderTagChips(sh) {
    var ed = sh.ed;
    var wrap = ed.refs.tagwrap;
    if (!wrap) {
      return;
    }
    L.clear(wrap);
    ed.cands.forEach(function (t) {
      var on = ed.sel.indexOf(t) !== -1;
      wrap.appendChild(h('button', {
        class: on ? 'press tagbtn tagbtn-on' : 'press tagbtn',
        type: 'button',
        'aria-pressed': on ? 'true' : 'false',
        onclick: function () {
          var i = ed.sel.indexOf(t);
          if (i === -1) {
            ed.sel.push(t);
          } else {
            ed.sel.splice(i, 1);
          }
          renderTagChips(sh);
          updateEdit(sh);
        }
      }, t));
    });
    wrap.hidden = !ed.cands.length;
  }

  function addNewTags(sh) {
    var ed = sh.ed;
    var input = ed.refs.newtag;
    var added = L.splitTags(input.value);
    if (!added.length) {
      input.focus();
      return;
    }
    added.forEach(function (t) {
      if (ed.cands.indexOf(t) === -1) {
        ed.cands.push(t);
      }
      if (ed.sel.indexOf(t) === -1) {
        ed.sel.push(t);
      }
    });
    input.value = '';
    renderTagChips(sh);
    updateEdit(sh);
    input.focus();
  }

  function dateInput(sh) {
    var ed = sh.ed;
    var input = h('input', {
      id: 'ed-date', class: 'field', type: ed.prec === 'month' ? 'month' : 'date',
      placeholder: ed.prec === 'month' ? '2026-07' : '2026-10-17', 'aria-describedby': 'ed-date-hint'
    });
    input.value = ed.date;
    var onChange = function () {
      ed.date = input.value;
      updateEdit(sh);
    };
    input.addEventListener('input', onChange);
    input.addEventListener('change', onChange);
    return input;
  }

  function setPrec(sh, p) {
    var ed = sh.ed;
    if (ed.prec === p) {
      return;
    }
    if (p === 'month') {
      var mm = /^(\d{4})-(\d{1,2})/.exec(String(ed.date || ''));
      ed.date = mm ? (mm[1] + '-' + L.pad2(mm[2])) : '';
    } else {
      ed.date = '';
    }
    ed.prec = p;
    var old = ed.refs.date;
    ed.refs.date = dateInput(sh);
    old.parentNode.replaceChild(ed.refs.date, old);
    updateEdit(sh);
  }

  function autoGrow(ta) {
    ta.style.height = 'auto';
    ta.style.height = Math.max(150, ta.scrollHeight + 2) + 'px';
  }

  function renderEdit(el, sh) {
    var ed = sh.ed;
    var r = ed.refs = {};
    var isNew = sh.mode === 'new';
    el.appendChild(h('div', { class: 'edhead' },
      h('h2', { class: 'edtitle', text: isNew ? '写一张新卡' : '标题和便利贴 #' + sh.id }),
      closeBtn(isNew ? '不写了' : '取消编辑', cancelEdit)));

    // 标题（可选）
    r.title = h('input', { id: 'ed-title', class: 'field', type: 'text', placeholder: '不写就显示内容开头', autocomplete: 'off', maxlength: '80', enterkeyhint: 'done' });
    r.title.value = ed.title;
    r.title.addEventListener('input', function () {
      ed.title = r.title.value;
      updateEdit(sh);
    });
    el.appendChild(h('div', { class: 'fgroup' }, h('label', { class: 'flabel', for: 'ed-title' }, '标题（可选）'), r.title));
    if (!isNew) {
      el.appendChild(h('span', { class: 'fhint fhint-block', text: '日子和内容在年轮里改：点那一圈右边的笔。' }));
    }
    if (isNew) {
      renderFirstRing(el, sh);
    }
    renderTagsGroup(el, sh);

    r.warn = h('span', { class: 'fwarn', 'aria-live': 'polite' });
    el.appendChild(r.warn);
    r.save = h('button', { class: 'press btn-primary bigbtn', type: 'button', onclick: function () { saveEdit(sh); } }, '保存');
    el.appendChild(h('div', { class: 'grid2' },
      h('button', { class: 'press btn-ghost bigbtn', type: 'button', onclick: cancelEdit }, '取消'),
      r.save));

    renderTagChips(sh);
    updateEdit(sh);
    if (r.text) {
      requestAnimationFrame(function () { autoGrow(r.text); });
    }
  }

  /** 新写一张：第一圈的日子和内容 */
  function renderFirstRing(el, sh) {
    var ed = sh.ed;
    var r = ed.refs;
    // 发生的日子
    r.segDay = h('button', { class: 'press segbtn', type: 'button', onclick: function () { setPrec(sh, 'day'); } }, '具体到哪天');
    r.segMonth = h('button', { class: 'press segbtn', type: 'button', onclick: function () { setPrec(sh, 'month'); } }, '只记得月份');
    r.date = dateInput(sh);
    r.dateHint = h('span', { class: 'fhint', id: 'ed-date-hint' });
    el.appendChild(h('div', { class: 'fgroup' },
      h('label', { class: 'flabel', for: 'ed-date' }, '发生的日子'),
      h('div', { class: 'seg seg2', role: 'group', 'aria-label': '日期精确到' }, r.segDay, r.segMonth),
      r.date,
      r.dateHint));

    // 内容
    r.text = h('textarea', { id: 'ed-text', class: 'field ta', rows: '5', placeholder: '写点什么……' });
    r.text.value = ed.text;
    r.text.addEventListener('input', function () {
      ed.text = r.text.value;
      autoGrow(r.text);
      updateEdit(sh);
    });
    el.appendChild(h('div', { class: 'fgroup' }, h('label', { class: 'flabel', for: 'ed-text' }, '内容（第一圈）'), r.text));
  }

  function renderTagsGroup(el, sh) {
    var ed = sh.ed;
    var r = ed.refs;
    r.tagwrap = h('div', { class: 'tagwrap', role: 'group', 'aria-labelledby': 'ed-tags-label' });
    r.newtag = h('input', {
      id: 'ed-newtag', class: 'field', type: 'text', placeholder: '新标签，几个可以用逗号隔开',
      'aria-label': '新标签', autocomplete: 'off', enterkeyhint: 'done', maxlength: '40'
    });
    r.newtag.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        addNewTags(sh);
      }
    });
    el.appendChild(h('div', { class: 'fgroup' },
      h('span', { class: 'flabel', id: 'ed-tags-label' }, '便利贴（可选）'),
      r.tagwrap,
      h('div', { class: 'tagadd' }, r.newtag,
        h('button', { class: 'press btn-ghost', type: 'button', onclick: function () { addNewTags(sh); } }, '添加'))));
  }

  function updateEdit(sh) {
    var ed = sh.ed;
    var r = ed.refs;
    var warn = '';
    if (sh.mode === 'new') {
      warn = firstRingWarn(ed, r);
    }
    r.warn.textContent = warn;
    r.warn.hidden = !warn;
    // 正在存：aria-disabled 变灰（saveEdit 自己挡重复点）；用 disabled 的话存失败后键盘焦点就丢了
    r.save.disabled = !!warn;
    r.save.setAttribute('aria-disabled', ed.busy ? 'true' : 'false');
    r.save.textContent = ed.busy ? '正在存……' : '保存';
  }

  /** 新写一张：日子、内容都要填好，日子不能是以后 */
  function firstRingWarn(ed, r) {
    var day = ed.prec === 'day';
    r.segDay.classList.toggle('seg-on', day);
    r.segMonth.classList.toggle('seg-on', !day);
    r.segDay.setAttribute('aria-pressed', day ? 'true' : 'false');
    r.segMonth.setAttribute('aria-pressed', day ? 'false' : 'true');
    r.dateHint.textContent = day ? '点一下选是哪天' : '只记得月份就选月份，比如 2026-07';
    var date = L.normalizeInputDate(ed.prec, ed.date);
    var dateOk = !!date;
    var textOk = ed.text.trim().length > 0;
    var today = L.bjDay(0);
    if (dateOk && date > (day ? today : today.slice(0, 7))) {
      return '不能写以后的日子，打算哪天做写进内容里';
    }
    if (!textOk && !dateOk) {
      return '内容和发生的日子都填好，就能保存';
    }
    if (!textOk) {
      return '内容不能是空的';
    }
    return dateOk ? '' : '发生的日子还没填好';
  }

  function saveEdit(sh) {
    var ed = sh.ed;
    var isNew = sh.mode === 'new';
    var date = L.normalizeInputDate(ed.prec, ed.date);
    var content = ed.text.trim();
    if (ed.busy || (isNew && (!date || !content || firstRingWarn(ed, ed.refs)))) {
      return;
    }
    ed.busy = true;
    updateEdit(sh);
    var body = isNew
      ? { title: ed.title.trim(), date: date, content: content, tags: ed.sel.join(',') }
      : { title: ed.title.trim(), tags: ed.sel.join(',') };
    var done = function () {
      ed.busy = false;
      if (S.sheet === sh && sh.ed === ed) {
        updateEdit(sh);
      }
    };

    if (sh.mode === 'new') {
      api('POST', '/api/memories', body).then(function (data) {
        toast('存好了一张新卡 #' + data.id);
        if (S.sheet === sh) {
          closeSheet();
        }
        // 回到「全部」，新写的那张能看见
        App.resetLibraryView();
        afterChange(null);
      }, function (err) {
        done();
        toast(err.message);
      });
      return;
    }

    body.expect_updated_at = ed.expect;
    api('PATCH', '/api/memories/' + sh.id, body).then(function (data) {
      toast(data.changed && data.changed.length ? '改好了，旧的放进了上一版' : '没有改动');
      if (S.sheet === sh) {
        sh.mem = data.memory;
        sh.full = true;
        sh.mode = 'view';
        sh.ed = null;
        sh.prevOpen = false;
        sh.req++;
        renderSheet();
      }
      if (data.changed && data.changed.length) {
        afterChange(false);
      }
    }, function (err) {
      done();
      if (err.status === 409) {
        toast('这条刚被改过，已刷新');
        if (S.sheet === sh) {
          sh.mode = 'view';
          sh.ed = null;
          sh.full = false;
          renderSheet();
        }
        afterChange(sh.id);
        return;
      }
      toast(err.message);
      if (err.status === 404) {
        if (S.sheet === sh) {
          closeSheet();
        }
        afterChange(null);
      }
    });
  }

  window.EmberSheet = {
    openDetail: openDetail,
    fetchDetail: fetchDetail,
    renderSheet: renderSheet,
    startNew: startNew,
    requestClose: requestClose
  };
})();
