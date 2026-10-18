/* ember 记忆库网页 · 界面：记忆库 / 最近动静 / 设置 三个 tab、状态、启动。
   工具函数在 console-lib.js（window.EmberLib）；底部面板（看、编辑、新写）在 console-sheet.js。
   服务器给的字一律 textContent，不用 innerHTML。 */
(function () {
  'use strict';

  var L = window.EmberLib;
  var h = L.h;
  var api = L.api;
  var toast = L.toast;

  var PAGE_SIZE = 200;
  var MONTH_CHIPS = 3;
  var SEARCH_DEBOUNCE_MS = 250;

  var VERBS = { save: '存了', edit: '改了', revert: '回退了', delete: '删了', restore: '恢复了' };
  var WHO = {
    ke: { cls: 'who who-ke', label: '(\\/)꒰. .꒱(\\/) 小克', name: '小克' },
    xuan: { cls: 'who who-xuan', label: '꒰ᐢ˶>ᴗ<˶ᐢ꒱ 轩', name: '轩' }
  };

  // ---------- 运行时状态（只在这里声明） ----------
  var S = {
    tab: 'library',
    scroll: { library: 0, activity: 0, settings: 0 },
    lib: { q: '', filter: 'all', items: [], page: 0, totalPages: 1, all: null, loading: false, loaded: false, req: 0 },
    months: [],
    hasOlder: false,
    acts: null,
    actsReq: 0,
    settings: null,
    settingsBusy: false,
    tags: null,
    sheet: null,
    closeTimer: null
  };

  function $(id) {
    return document.getElementById(id);
  }
  /** 底部面板在 console-sheet.js（比本文件后加载，用到时再取） */
  function Sheet() {
    return window.EmberSheet;
  }
  function whoName(actor) {
    return (WHO[actor] || WHO.ke).name;
  }

  // =====================================================================
  // 配色
  // =====================================================================
  function renderThemeControls() {
    var t = L.getTheme();
    document.querySelectorAll('#theme-seg .segbtn').forEach(function (b) {
      var on = b.getAttribute('data-theme') === t;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.classList.toggle('seg-on', on);
    });
  }

  function pickTheme(t) {
    L.setTheme(t);
    renderThemeControls();
  }

  // =====================================================================
  // tab
  // =====================================================================
  var TABS = ['library', 'activity', 'settings'];

  function setTab(tab) {
    if (TABS.indexOf(tab) === -1) {
      return;
    }
    var same = tab === S.tab;
    if (!same) {
      S.scroll[S.tab] = window.scrollY;
    }
    S.tab = tab;
    TABS.forEach(function (t) {
      $('view-' + t).hidden = t !== tab;
    });
    document.querySelectorAll('.tabbtn').forEach(function (b) {
      var on = b.getAttribute('data-tab') === tab;
      b.classList.toggle('tab-on', on);
      b.setAttribute('aria-current', on ? 'page' : 'false');
    });
    $('tab-bubble').className = 'tab-bubble pos-' + TABS.indexOf(tab);
    $('fab').hidden = tab !== 'library';
    window.scrollTo(0, same ? 0 : (S.scroll[tab] || 0));
    if (tab === 'activity') {
      loadActivity();
    } else if (tab === 'settings') {
      loadSettings();
    }
  }

  // =====================================================================
  // 记忆库
  // =====================================================================
  function libParams(page) {
    var p = { page: page, page_size: PAGE_SIZE, q: S.lib.q.trim() };
    var f = S.lib.filter;
    if (f === 'older' && S.months.length) {
      p.date_to = L.prevMonth(S.months[S.months.length - 1]);
    } else if (f !== 'all' && f !== 'older') {
      p.date_from = f;
      p.date_to = f;
    }
    return p;
  }

  function isUnfiltered() {
    return S.lib.filter === 'all' && !S.lib.q.trim();
  }

  function loadLibrary(more) {
    var page = more ? S.lib.page + 1 : 1;
    var req = ++S.lib.req;
    S.lib.loading = true;
    $('lib-list').setAttribute('aria-busy', 'true');
    return api('GET', '/api/memories' + L.qs(libParams(page))).then(function (data) {
      if (req !== S.lib.req) {
        return;
      }
      S.lib.loading = false;
      S.lib.loaded = true;
      S.lib.items = more ? S.lib.items.concat(data.items) : data.items;
      S.lib.page = data.page;
      S.lib.totalPages = data.total_pages;
      S.lib.all = data.stats.all;
      if (!more && isUnfiltered()) {
        updateMonths(data.months);
      }
      renderLibrary();
    }, function (err) {
      if (req !== S.lib.req) {
        return;
      }
      S.lib.loading = false;
      renderLibrary(err.message);
      toast(err.message);
    });
  }

  /** 服务端按全库算月份，返回至多 4 个；不受第一页条数影响。 */
  function updateMonths(months) {
    var ms = months || [];
    S.months = ms.slice(0, MONTH_CHIPS);
    S.hasOlder = ms.length > MONTH_CHIPS;
    renderChips();
  }

  /** 改过数据而当前列表带着筛选时，单独拉一次不带筛选的，重算月份按钮 */
  function refreshMonths() {
    return api('GET', '/api/memories' + L.qs({ page: 1, page_size: PAGE_SIZE })).then(function (data) {
      S.lib.all = data.stats.all;
      updateMonths(data.months);
      var f = S.lib.filter;
      var gone = (f === 'older' && !S.hasOlder) || (f !== 'all' && f !== 'older' && S.months.indexOf(f) === -1);
      if (gone) {
        S.lib.filter = 'all';
        renderChips();
        loadLibrary();
      } else {
        renderLibSub();
      }
    }, function () { /* 月份按钮旧一点不要紧 */ });
  }

  function setFilter(f) {
    if (S.lib.filter === f) {
      return;
    }
    S.lib.filter = f;
    renderChips();
    loadLibrary();
  }

  function renderChips() {
    var wrap = L.clear($('chips'));
    var defs = [['all', '全部']];
    S.months.forEach(function (ym) {
      defs.push([ym, L.monthLabel(ym)]);
    });
    if (S.hasOlder && S.months.length) {
      defs.push(['older', '更早']);
    }
    defs.forEach(function (d) {
      var on = S.lib.filter === d[0];
      wrap.appendChild(h('button', {
        class: on ? 'press chip chip-sel' : 'press glass chip',
        type: 'button',
        'aria-pressed': on ? 'true' : 'false',
        onclick: function () { setFilter(d[0]); }
      }, h('span', { class: 'chip-t', text: d[1] })));
    });
  }

  function renderLibSub() {
    $('q').placeholder = S.lib.all == null ? '搜内容或编号' : ('在 ' + S.lib.all + ' 条记忆里搜，编号也行');
  }

  function memCard(m) {
    var tags = L.splitTags(m.tags);
    return h('button', {
      class: 'card press',
      type: 'button',
      'data-key': 'm' + Number(m.id),
      onclick: function (e) { Sheet().openDetail(m.id, m, e.currentTarget); }
    },
    h('span', { class: 'cardtop' },
      h('span', { class: 'cdate', text: L.dateBig(m.date) }),
      h('span', { class: 'cwk', text: L.weekday(m.date) }),
      h('span', { class: 'cno', text: '#' + m.id })),
    h('span', { class: 'ctext clamp3', text: m.content }),
    tags.length ? h('span', { class: 'tags' }, tags.map(function (t) {
      return h('span', { class: L.tagClass(t), text: t });
    })) : null);
  }

  function emptyState(title, sub) {
    return h('div', { class: 'empty' }, h('span', { class: 'empty-t', text: title }), h('span', { class: 'empty-s', text: sub }));
  }

  function renderLibrary(errMsg) {
    renderLibSub();
    var list = $('lib-list');
    // 焦点在列表里（键盘用户）：重画后放回同一张卡片，不让它掉回页面开头
    var keep = list.contains(document.activeElement) ? L.focusKeys(document.activeElement) : [];
    L.clear(list);
    fillLibrary(list, errMsg);
    if (keep.length && L.focusLost()) {
      L.focusByKeys(keep, list);
    }
  }

  function fillLibrary(list, errMsg) {
    list.setAttribute('aria-busy', S.lib.loading ? 'true' : 'false');
    if (!S.lib.loaded) {
      list.appendChild(errMsg ? emptyState('没打开', errMsg) : h('span', { class: 'loading', text: '正在翻……' }));
      return;
    }
    var items = S.lib.items;
    if (!items.length) {
      if (isUnfiltered() && !S.lib.all) {
        list.appendChild(emptyState('还没有记忆', '点右下角的 + 写第一条'));
      } else {
        list.appendChild(emptyState('没有找到相关的记忆', '换个词，或者换个月份试试'));
      }
      return;
    }
    var group = null;
    var lastYm = null;
    items.forEach(function (m) {
      var ym = String(m.date || '').slice(0, 7);
      if (!group || ym !== lastYm) {
        lastYm = ym;
        list.appendChild(h('h2', { class: 'monthh', text: L.monthLabel(ym) }));
        group = h('div', { class: 'cards' });
        list.appendChild(group);
      }
      group.appendChild(memCard(m));
    });
    if (S.lib.page < S.lib.totalPages) {
      // 加载中用 aria-disabled 不用 disabled：按钮变灰会把键盘焦点弄丢
      list.appendChild(h('button', {
        class: 'press btn-ghost wide',
        type: 'button',
        'data-key': 'more',
        onclick: function (e) {
          var b = e.currentTarget;
          if (S.lib.loading || b.getAttribute('aria-disabled') === 'true') {
            return;
          }
          b.setAttribute('aria-disabled', 'true');
          b.textContent = '正在加载……';
          loadLibrary(true);
        }
      }, '加载更多'));
    }
  }

  /** 搜索框贴顶时才给它垫渐隐底色（没 IntersectionObserver 的老浏览器一直垫着） */
  function watchSticky() {
    var st = $('sticky');
    if (!('IntersectionObserver' in window)) {
      st.classList.add('stuck');
      return;
    }
    new window.IntersectionObserver(function (entries) {
      st.classList.toggle('stuck', !entries[0].isIntersecting);
    }).observe($('stick-sentinel'));
  }

  // 搜索：打字停 250ms 再搜；回车 / 清除马上搜
  var searchTimer = null;
  function runSearch() {
    if (searchTimer) {
      clearTimeout(searchTimer);
      searchTimer = null;
    }
    var v = $('q').value;
    $('clearq').hidden = v === '';
    if (v.trim() === S.lib.q.trim()) {
      S.lib.q = v;
      return;
    }
    S.lib.q = v;
    loadLibrary();
  }

  function bindSearch() {
    var q = $('q');
    q.addEventListener('input', function () {
      $('clearq').hidden = q.value === '';
      if (searchTimer) {
        clearTimeout(searchTimer);
      }
      searchTimer = setTimeout(runSearch, SEARCH_DEBOUNCE_MS);
    });
    q.addEventListener('search', runSearch);
    q.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.isComposing) {
        runSearch();
        q.blur(); // 手机上收起键盘
      }
    });
    $('clearq').addEventListener('click', function (e) {
      e.preventDefault();
      q.value = '';
      runSearch();
      q.focus();
    });
  }

  // =====================================================================
  // 最近动静
  // =====================================================================
  function loadActivity() {
    var req = ++S.actsReq;
    if (!S.acts) {
      renderActivity();
    }
    return api('GET', '/api/activity').then(function (data) {
      if (req !== S.actsReq) {
        return;
      }
      S.acts = data;
      renderActivity();
    }, function (err) {
      if (req !== S.actsReq) {
        return;
      }
      toast(err.message);
      if (!S.acts) {
        renderActivity(err.message);
      }
    });
  }

  function actRow(a) {
    var who = WHO[a.actor] || WHO.ke;
    var exists = !(a.can === 'restore' || a.status === '已删除' || a.status === '已彻底删除');
    var kids = [
      h('span', { class: 'rowtop' },
        h('span', { class: who.cls, text: who.label }),
        h('span', { class: 'rowtime', text: L.stampLabel(a.at, true) })),
      h('span', { class: 'rowtext' },
        h('strong', { text: VERBS[a.action] || a.action }),
        ' #' + a.memory_id + ' ' + (a.title || ''))
    ];
    var rowKey = 'a' + Number(a.id);
    var body = exists
      ? h('button', { class: 'rowbody press', type: 'button', 'data-key': rowKey, onclick: function (e) { Sheet().openDetail(a.memory_id, null, e.currentTarget); } }, kids)
      : h('span', { class: 'rowbody' }, kids);
    var right = null;
    if (a.can === 'revert') {
      right = h('button', {
        class: 'press btn-soft', type: 'button', 'aria-label': '回退 #' + a.memory_id + ' 的这次改动',
        'data-key': rowKey + '-b', 'data-row': rowKey,
        onclick: function (e) { actRevert(a, e.currentTarget); }
      }, '回退');
    } else if (a.can === 'restore') {
      right = h('button', {
        class: 'press btn-soft', type: 'button', 'aria-label': '恢复 #' + a.memory_id,
        'data-key': rowKey + '-b', 'data-row': rowKey,
        onclick: function (e) { actRestore(a, e.currentTarget); }
      }, '恢复');
    } else if (a.status) {
      right = h('span', { class: 'rowstatus', text: a.status });
    }
    return h('div', { class: 'row' }, body, right);
  }

  function renderActivity(errMsg) {
    var list = $('act-list');
    var keep = list.contains(document.activeElement) ? L.focusKeys(document.activeElement) : [];
    L.clear(list);
    fillActivity(list, errMsg);
    if (keep.length && L.focusLost()) {
      L.focusByKeys(keep, list);
    }
  }

  function fillActivity(list, errMsg) {
    if (!S.acts) {
      $('act-sub').textContent = '删掉的在这里还能恢复';
      list.appendChild(errMsg ? emptyState('没打开', errMsg) : h('span', { class: 'loading', text: '正在翻……' }));
      return;
    }
    $('act-sub').textContent = '最近 ' + S.acts.days + ' 天 · 删掉的在这里还能恢复';
    if (!S.acts.items.length) {
      list.appendChild(emptyState('最近 ' + S.acts.days + ' 天没有动静', '存、改、删都会记在这里'));
      return;
    }
    S.acts.items.forEach(function (a) {
      list.appendChild(actRow(a));
    });
  }

  /** 动静行上的按钮：点过一次就先不理，等列表刷新（不用 disabled，免得键盘焦点丢掉） */
  function claimBtn(btn) {
    if (btn.getAttribute('aria-disabled') === 'true') {
      return false;
    }
    btn.setAttribute('aria-disabled', 'true');
    return true;
  }

  function actRevert(a, btn) {
    if (!claimBtn(btn)) {
      return;
    }
    api('POST', '/api/memories/' + a.memory_id + '/revert', {
      history_id: a.history_id, expect_updated_at: a.updated_at
    }).then(function () {
      toast('已回退 #' + a.memory_id);
      afterChange(a.memory_id);
    }, function (err) {
      toast(err.message);
      loadActivity();
    });
  }

  function actRestore(a, btn) {
    if (!claimBtn(btn)) {
      return;
    }
    api('POST', '/api/memories/' + a.memory_id + '/restore').then(function () {
      toast('恢复了，回记忆库就能看到');
      afterChange(a.memory_id);
    }, function (err) {
      toast(err.message);
      loadActivity();
    });
  }

  // =====================================================================
  // 设置
  // =====================================================================
  function loadSettings() {
    return api('GET', '/api/settings').then(function (data) {
      S.settings = data;
      renderSettings();
    }, function (err) {
      toast(err.message);
    });
  }

  // 正在存设置时用 aria-disabled 变灰、不用 disabled：按钮一 disabled，键盘焦点就掉回页面开头
  function renderSettings() {
    var st = S.settings;
    var busy = S.settingsBusy ? 'true' : 'false';
    var keep = st ? st.history_keep : null;
    $('keep-val').textContent = (keep == null ? '–' : keep) + ' 版';
    $('keep-minus').disabled = !st || keep <= 1;
    $('keep-plus').disabled = !st || keep >= 2;
    $('keep-minus').setAttribute('aria-disabled', busy);
    $('keep-plus').setAttribute('aria-disabled', busy);
    document.querySelectorAll('#days-seg .segbtn').forEach(function (b) {
      var on = !!st && Number(b.getAttribute('data-days')) === st.activity_days;
      b.classList.toggle('seg-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.disabled = !st;
      b.setAttribute('aria-disabled', busy);
    });
    var ke = !!(st && st.ke_can_delete);
    var sw = $('ke-switch');
    sw.setAttribute('aria-checked', ke ? 'true' : 'false');
    sw.firstElementChild.classList.toggle('track-on', ke);
    sw.disabled = !st;
    sw.setAttribute('aria-disabled', busy);
    renderThemeControls();
  }

  function settingsReady() {
    return !!S.settings && !S.settingsBusy;
  }

  /** 改完设置：焦点要是因为按钮到头变灰丢了（比如留 1 版后的 -），放到旁边那个按钮上 */
  function keepFocus(from) {
    if (!from || !L.focusLost()) {
      return;
    }
    var alt = { 'keep-minus': 'keep-plus', 'keep-plus': 'keep-minus' }[from.id];
    var el = from.disabled && alt ? $(alt) : from;
    if (el && !el.disabled && document.contains(el)) {
      el.focus({ preventScroll: true });
    }
  }

  function patchSettings(patch, okMsg) {
    var from = document.activeElement;
    S.settingsBusy = true;
    renderSettings();
    return api('PATCH', '/api/settings', patch).then(function (data) {
      S.settings = data;
      S.settingsBusy = false;
      renderSettings();
      keepFocus(from);
      toast(okMsg);
      if (S.acts) {
        loadActivity();
      }
    }, function (err) {
      S.settingsBusy = false;
      renderSettings();
      keepFocus(from);
      toast(err.message);
    });
  }

  function changeKeep(delta) {
    if (!settingsReady()) {
      return;
    }
    var next = S.settings.history_keep + delta;
    if (next < 1 || next > 2 || next === S.settings.history_keep) {
      return;
    }
    if (next < S.settings.history_keep) {
      L.confirmDialog({ title: '只留 1 版？', msg: '每条记忆更早的那一版会马上扔掉。', ok: '只留 1 版' }).then(function (ok) {
        if (ok) {
          patchSettings({ history_keep: next }, '以后每条只留 1 版');
        }
      });
      return;
    }
    patchSettings({ history_keep: next }, '以后每条留 ' + next + ' 版');
  }

  function changeDays(d) {
    if (!settingsReady() || S.settings.activity_days === d) {
      return;
    }
    var msg = '最近动静改成显示 ' + d + ' 天';
    if (d < S.settings.activity_days) {
      L.confirmDialog({
        title: '只显示 ' + d + ' 天？',
        msg: '删掉超过 ' + d + ' 天的记忆会马上彻底删掉，没法再恢复。',
        ok: '改成 ' + d + ' 天'
      }).then(function (ok) {
        if (ok) {
          patchSettings({ activity_days: d }, msg);
        }
      });
      return;
    }
    patchSettings({ activity_days: d }, msg);
  }

  function toggleKe() {
    if (!settingsReady()) {
      return;
    }
    var next = !S.settings.ke_can_delete;
    patchSettings({ ke_can_delete: next }, next ? '小克现在能删了' : '小克现在只能存和改');
  }

  function askClearAll() {
    L.confirmDialog({ title: '清空所有上一版？', msg: '清掉以后，这些记忆就没法回退了。', ok: '清空' }).then(function (ok) {
      if (!ok) {
        return;
      }
      api('DELETE', '/api/history').then(function (data) {
        toast(data.cleared ? ('清掉了 ' + data.cleared + ' 版') : '本来就没有上一版');
        afterChange(null);
      }, function (err) {
        toast(err.message);
      });
    });
  }

  function bindSettings() {
    $('keep-minus').addEventListener('click', function () { changeKeep(-1); });
    $('keep-plus').addEventListener('click', function () { changeKeep(1); });
    document.querySelectorAll('#days-seg .segbtn').forEach(function (b) {
      b.addEventListener('click', function () { changeDays(Number(b.getAttribute('data-days'))); });
    });
    $('ke-switch').addEventListener('click', toggleKe);
    document.querySelectorAll('#theme-seg .segbtn').forEach(function (b) {
      b.addEventListener('click', function () { pickTheme(b.getAttribute('data-theme')); });
    });
    $('clear-all').addEventListener('click', askClearAll);
  }

  // =====================================================================
  // 改完以后：列表、月份、动静、面板都刷新一下
  // =====================================================================
  /** refetch：null = 面板开着就重新拉；某个编号 = 只在开着的是这条时拉；false = 面板已经是新的，不用拉 */
  function afterChange(refetch) {
    S.tags = null;
    loadLibrary();
    if (!isUnfiltered()) {
      refreshMonths();
    }
    if (S.acts || S.tab === 'activity') {
      loadActivity();
    }
    var sh = S.sheet;
    if (refetch !== false && sh && sh.mode === 'view' && (refetch == null || sh.id === refetch)) {
      Sheet().fetchDetail();
    }
  }

  /** 新写完一条：回到「全部」、清掉搜索，好让新的那条看得见 */
  function resetLibraryView() {
    S.lib.filter = 'all';
    S.lib.q = '';
    $('q').value = '';
    $('clearq').hidden = true;
    renderChips();
  }

  // 给 console-sheet.js 用的
  window.EmberApp = {
    S: S,
    $: $,
    whoName: whoName,
    afterChange: afterChange,
    resetLibraryView: resetLibraryView
  };

  // =====================================================================
  // 启动
  // =====================================================================
  function bindGlobal() {
    document.querySelectorAll('.tabbtn').forEach(function (b) {
      b.addEventListener('click', function () { setTab(b.getAttribute('data-tab')); });
    });
    $('fab').addEventListener('click', function (e) { Sheet().startNew(e.currentTarget); });
    $('scrim').addEventListener('click', function () { Sheet().requestClose(); });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') {
        return;
      }
      if (L.alertOpen()) {
        e.preventDefault();
        L.finishAlert(false);
      } else if (S.sheet) {
        e.preventDefault();
        Sheet().requestClose();
      }
    });
    renderThemeControls();
  }

  function init() {
    bindGlobal();
    bindSearch();
    watchSticky();
    bindSettings();
    renderChips();
    renderLibrary();
    renderSettings();
    setTab('library');
    loadLibrary();
    loadSettings();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
