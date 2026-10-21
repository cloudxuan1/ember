/* ember 记忆库网页 · 界面：记忆库 / 最近动静 / 设置 三个 tab、状态、启动。
   工具函数在 console-lib.js（window.EmberLib）；底部面板（看、编辑、新写）在 console-sheet.js。
   服务器给的字一律 textContent，不用 innerHTML。 */
(function () {
  'use strict';

  var L = window.EmberLib;
  var h = L.h;
  var icon = L.icon;
  var api = L.api;
  var toast = L.toast;

  var PAGE_SIZE = 200;
  var MONTH_CHIPS = 3;
  var SEARCH_DEBOUNCE_MS = 250;
  var starWrites = {}; // 同一张卡串行写，只把最后一次点选作为目标

  var VERBS = { save: '存了', edit: '改了', revert: '回退了', delete: '删了', restore: '恢复了' };
  var WHO = {
    ke: { cls: 'who who-ke', label: '(\\/)꒰. .꒱(\\/) 小克', name: '小克' },
    xuan: { cls: 'who who-xuan', label: '꒰ᐢ˶>ᴗ<˶ᐢ꒱ 轩', name: '轩' }
  };

  // ---------- 运行时状态（只在这里声明） ----------
  var S = {
    tab: 'library',
    scroll: { library: 0, activity: 0, settings: 0 },
    // filter = 月份按钮（all / YYYY-MM / older）；star、pie = 月份后面的 ⭐ / 饼 两个开关，跟月份叠着筛
    lib: { q: '', filter: 'all', star: false, pie: false, items: [], page: 0, totalPages: 1, all: null, loading: false, loaded: false, req: 0 },
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
    if (S.lib.star) {
      p.starred = 1;
    }
    if (S.lib.pie) {
      p.pie = 1;
    }
    return p;
  }

  function isUnfiltered() {
    return S.lib.filter === 'all' && !S.lib.star && !S.lib.pie && !S.lib.q.trim();
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

  function toggleLib(key) {
    S.lib[key] = !S.lib[key];
    renderChips();
    loadLibrary();
  }

  function renderChips() {
    var wrap = $('chips');
    var sl = wrap.scrollLeft;
    L.clear(wrap);
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
    // 月份后面两个开关：只看⭐、只看饼（贴了四张饼便利贴之一的），跟月份叠着筛
    wrap.appendChild(h('span', { class: 'chip-sep', 'aria-hidden': 'true' }));
    [['star', '只看收藏'], ['pie', '只看饼']].forEach(function (d) {
      var on = S.lib[d[0]];
      wrap.appendChild(h('button', {
        class: (on ? 'press chip chip-sel' : 'press glass chip') + ' chip-ico chip-' + d[0],
        type: 'button',
        'aria-pressed': on ? 'true' : 'false',
        'aria-label': d[1],
        onclick: function () { toggleLib(d[0]); }
      }, h('span', { class: 'chip-t' }, icon(d[0], d[0] === 'pie' ? 22 : 18, null, d[0] === 'star' && on))));
    });
    wrap.scrollLeft = sl;
  }

  function renderLibSub() {
    $('q').placeholder = S.lib.all == null ? '搜内容或编号' : ('在 ' + S.lib.all + ' 张卡里搜，编号也行');
  }

  // =====================================================================
  // 卡片：混着来——点了某个月，那个月有圈的卡都在，卡面露那个月最新的一圈
  // =====================================================================
  function inFilter(date) {
    var f = S.lib.filter;
    if (f === 'all') {
      return true;
    }
    var ym = L.ymOf(date);
    if (f === 'older') {
      return !!S.months.length && ym < S.months[S.months.length - 1];
    }
    return ym === f;
  }

  /** 卡面上露哪一圈：{ring, i（第几圈，从 0 数）, n, rings}。没有圈的老数据当成只有一圈 */
  function shownRing(m) {
    var rs = m.rings && m.rings.length ? m.rings : [{ id: null, date: m.date, content: m.content }];
    var i = rs.length - 1;
    while (i > 0 && !inFilter(rs[i].date)) {
      i--;
    }
    if (!inFilter(rs[i].date)) {
      i = rs.length - 1;
    }
    return { ring: rs[i], i: i, n: rs.length, rings: rs };
  }

  /** 面板打开时滚到哪一圈、闪一下：搜着字 → 范围里写着这个字的最新一圈；从某个月点进来 → 卡上露的那一圈；
      全部、又没搜 → 不闪 */
  function spotRing(m) {
    var sr = shownRing(m);
    var q = S.lib.q.trim().toLowerCase();
    if (q) {
      for (var i = sr.rings.length - 1; i >= 0; i--) {
        var r = sr.rings[i];
        if (inFilter(r.date) && String(r.content || '').toLowerCase().indexOf(q) !== -1) {
          return r.id;
        }
      }
    }
    return S.lib.filter === 'all' ? null : sr.ring.id;
  }

  function starBtn(m, cls) {
    if (starWrites[m.id]) {
      m.starred = starWrites[m.id].wanted ? 1 : 0;
    }
    var on = !!m.starred;
    return h('button', {
      class: 'press starbtn ' + cls + (on ? ' star-on' : ''),
      type: 'button',
      'data-star': String(Number(m.id)),
      'aria-pressed': on ? 'true' : 'false',
      'aria-label': '收藏「' + (m.title || '这张卡') + '」',
      onclick: function (e) { setStar(m.id, !m.starred, e.currentTarget); }
    }, icon('star', 22, null, on));
  }

  /** 页面重画过也更新当前的卡片和面板，不留着请求发出时那批旧对象。 */
  function paintStar(id, on) {
    var targets = S.lib.items.filter(function (m) { return m.id === id; });
    if (S.sheet && S.sheet.mem && S.sheet.id === id) {
      targets.push(S.sheet.mem);
    }
    targets.forEach(function (m) { m.starred = on ? 1 : 0; });
    document.querySelectorAll('[data-star="' + Number(id) + '"]').forEach(function (b) {
      b.classList.toggle('star-on', on);
      b.classList.remove('star-pop');
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.replaceChild(icon('star', 22, null, on), b.firstChild);
    });
  }

  function sendStar(id, write) {
    var sent = write.wanted;
    write.busy = true;
    api('POST', '/api/memories/' + id + '/star', { starred: sent }).then(function () {
      write.saved = sent;
      write.busy = false;
      if (write.wanted !== sent) {
        sendStar(id, write); // 上一次落定后再写最后一次点选，回包不会倒着覆盖
      } else {
        delete starWrites[id];
      }
      paintStar(id, write.wanted);
    }, function (err) {
      delete starWrites[id];
      paintStar(id, write.saved);
      toast(err.message);
    });
  }

  /** 点⭐立即亮，连续点按同一张卡排队；⭐ 筛着时取消，卡先留在眼前，换筛选再走。 */
  function setStar(id, on, btn) {
    var write = starWrites[id];
    if (!write) {
      write = starWrites[id] = { saved: !on, wanted: on, busy: false };
    }
    write.wanted = on;
    paintStar(id, on);
    if (on && btn) {
      btn.classList.add('star-pop');
    }
    if (!write.busy) {
      sendStar(id, write);
    }
  }

  function memCard(m) {
    var sr = shownRing(m);
    var r = sr.ring;
    var tags = L.splitTags(m.tags);
    var label = '打开「' + (m.title || String(r.content || '').slice(0, 12)) + '」，' + L.dateLong(r.date) +
      (sr.n > 1 ? '，共 ' + sr.n + ' 圈' : '');
    return h('article', { class: 'card rcard' },
      h('button', {
        class: 'rcard-open',
        type: 'button',
        'data-key': 'm' + Number(m.id),
        'aria-label': label,
        onclick: function (e) { Sheet().openDetail(m.id, m, e.currentTarget, spotRing(m)); }
      }),
      h('span', { class: 'cardtop rcard-top' },
        h('span', { class: 'cdate', text: L.dateBig(r.date) }),
        h('span', { class: 'cwk', text: L.weekday(r.date) })),
      m.title ? h('span', { class: 'rcard-title', text: m.title }) : null,
      h('span', { class: 'ctext clamp3', text: r.content }),
      sr.n > 1 || tags.length ? h('span', { class: 'tags rcard-meta' },
        sr.n > 1 ? h('span', { class: 'tag tag-rings' }, L.ringDots(sr.n, sr.i)) : null,
        tags.map(function (t) { return h('span', { class: L.tagClass(t), text: t }); })) : null,
      starBtn(m, 'rcard-star'));
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
      var onlyToggle = S.lib.filter === 'all' && !S.lib.q.trim();
      if (isUnfiltered() && !S.lib.all) {
        list.appendChild(emptyState('还没有卡', '点右下角的 + 写第一张'));
      } else if (onlyToggle && S.lib.star && !S.lib.pie) {
        list.appendChild(emptyState('还没有收藏的卡', '点卡片右上角的星星就收藏了'));
      } else if (onlyToggle && S.lib.pie && !S.lib.star) {
        list.appendChild(emptyState('还没有饼', '贴了筑巢饼 / 连理饼 / 养成饼 / 远方饼的卡会在这里'));
      } else {
        list.appendChild(emptyState('没有对得上的卡', '换个月份，或者换个词试试'));
      }
      return;
    }
    var group = null;
    var lastYm = null;
    items.forEach(function (m) {
      var ym = L.ymOf(shownRing(m).ring.date) || String(m.date || '').slice(0, 7);
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
  // 开场带几张饼：0（不带）到 4，跟后端 settings.BRIEFING_PIES_RANGE 一致
  var PIES_MIN = 0;
  var PIES_MAX = 4;

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
    var pies = st ? st.briefing_pies : null;
    $('pies-val').textContent = pies == null ? '– 张' : (pies ? pies + ' 张' : '不带');
    $('pies-minus').disabled = !st || pies <= PIES_MIN;
    $('pies-plus').disabled = !st || pies >= PIES_MAX;
    $('pies-minus').setAttribute('aria-disabled', busy);
    $('pies-plus').setAttribute('aria-disabled', busy);
    document.querySelectorAll('#days-seg .segbtn').forEach(function (b) {
      var on = !!st && Number(b.getAttribute('data-days')) === st.activity_days;
      b.classList.toggle('seg-on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.disabled = !st;
      b.setAttribute('aria-disabled', busy);
    });
    renderSwitch('ke-switch', !!(st && st.ke_can_delete), !st, busy);
    renderSwitch('star-switch', !!(st && st.ke_can_star), !st, busy);
    renderThemeControls();
  }

  function renderSwitch(id, on, disabled, busy) {
    var sw = $(id);
    sw.setAttribute('aria-checked', on ? 'true' : 'false');
    sw.firstElementChild.classList.toggle('track-on', on);
    sw.disabled = disabled;
    sw.setAttribute('aria-disabled', busy);
  }

  function settingsReady() {
    return !!S.settings && !S.settingsBusy;
  }

  /** 改完设置：焦点要是因为按钮到头变灰丢了（比如留 1 版后的 -），放到旁边那个按钮上 */
  function keepFocus(from) {
    if (!from || !L.focusLost()) {
      return;
    }
    var alt = { 'keep-minus': 'keep-plus', 'keep-plus': 'keep-minus', 'pies-minus': 'pies-plus', 'pies-plus': 'pies-minus' }[from.id];
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

  function changePies(delta) {
    if (!settingsReady()) {
      return;
    }
    var next = S.settings.briefing_pies + delta;
    if (next < PIES_MIN || next > PIES_MAX) {
      return;
    }
    patchSettings({ briefing_pies: next }, next ? '开场最多带 ' + next + ' 张饼' : '开场不带饼了');
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

  function toggleStar() {
    if (!settingsReady()) {
      return;
    }
    var next = !S.settings.ke_can_star;
    patchSettings({ ke_can_star: next }, next ? '小克现在能点⭐了' : '现在只有你能点⭐');
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
    $('pies-minus').addEventListener('click', function () { changePies(-1); });
    $('pies-plus').addEventListener('click', function () { changePies(1); });
    document.querySelectorAll('#days-seg .segbtn').forEach(function (b) {
      b.addEventListener('click', function () { changeDays(Number(b.getAttribute('data-days'))); });
    });
    $('ke-switch').addEventListener('click', toggleKe);
    $('star-switch').addEventListener('click', toggleStar);
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

  /** 新写完一条：回到「全部」、清掉搜索和 ⭐ / 饼，好让新的那条看得见 */
  function resetLibraryView() {
    S.lib.filter = 'all';
    S.lib.star = false;
    S.lib.pie = false;
    S.lib.q = '';
    $('q').value = '';
    $('clearq').hidden = true;
    renderChips();
  }

  // 给 console-sheet.js 用的
  window.EmberApp = {
    S: S,
    $: $,
    setStar: setStar,
    starBtn: starBtn,
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
