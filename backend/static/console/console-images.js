/* ember 记忆库网页 · 图片页（#41）：生图画好的图——两列网格、⭐、勾选放进回收站、回收站（恢复 / 彻底删 / 清空）、点开看大图。
   图、任务记录、回收站都在生图 worker 那边，这里只调 /api/images/*；真正的核对（⭐ 的不能删、排队任务的参考图不能删、
   只从回收站彻底删）在 worker，每次都重读一遍，所以页面开着时小克动了什么，这边的操作会被拒并说原因，不会冲掉。
   样子照轩挑定的预览：两列卡片，型号是图右下角的半透明小牌子（跟⭐一个样子），型号写在前面。
   状态放在 console.js 的 S.img；看大图借记忆库的底部面板（S.sheet.mode = 'image'）。服务器给的字一律 textContent。 */
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

  var PAGE_SIZE = 60;
  var DAY_MS = 86400e3;
  var VIEWS = ['all', 'starred', 'trash'];
  var EMPTY = {
    all: ['还没有图', '让小克画一张，画好的都在这里'],
    starred: ['还没有收藏的图', '点图右上角的星星就收藏了'],
    trash: ['回收站是空的', '删掉的图在这里放 30 天，满了自动彻底删除']
  };
  // 勾的几张里有没放进去的：提示条上一句话说原因（worker 的原话较长，放不下）
  var WHY = { starred: '刚被点了⭐', in_use: '正被排队的画图当参考图', purged: '已经彻底删了', not_found: '找不到了', no_image: '没有画好的图' };

  // view = all（没进回收站的）/ starred（只看⭐）/ trash（回收站）；picked = 勾上的编号
  var G = S.img = {
    view: 'all', items: [], page: 0, hasMore: false, loaded: false, loading: false, req: 0, err: '',
    stats: null, statsReq: 0, picking: false, picked: {}, busy: false
  };
  var starWrites = {}; // 同一张图串行写⭐，只把最后一次点选当目标（同记忆库）

  function Sheet() {
    return window.EmberSheet;
  }

  // =====================================================================
  // 小工具
  // =====================================================================
  /** worker 存的 UTC 时间 → 北京时间 'YYYY-MM-DD HH:MM'（L.stampLabel 认这个） */
  function bjStamp(iso) {
    var t = Date.parse(iso || '');
    return isNaN(t) ? '' : new Date(t + 8 * 3600e3).toISOString().slice(0, 16).replace('T', ' ');
  }

  /** 'YYYY-MM-DD…' → 11 月 7 日（不是今年的加上年份） */
  function dayText(bj) {
    var p = L.parseMemDate(bj.slice(0, 10));
    if (!p || !p.d) {
      return '';
    }
    return (p.y === L.curYear() ? '' : p.y + ' 年 ') + p.m + ' 月 ' + p.d + ' 日';
  }

  /** 面板标题：11 月 7 日 14:09 */
  function longStamp(iso) {
    var bj = bjStamp(iso);
    return bj ? dayText(bj) + ' ' + bj.slice(11, 16) : '图片';
  }

  /** 型号写在前面（轩定）：GPT Image 那几个写成「2.6 Sunburst」，跟模型编号 gpt-image-2.6-sunburst 一个顺序；
      别的只有一个，照名字；不传 model 的是 Codex 订阅 */
  function modelTag(item) {
    var m = /^openai\/gpt-image-(\d+(?:\.\d+)*)-([a-z]+)$/.exec(item.model || '');
    if (m) {
      return m[1] + ' ' + m[2].charAt(0).toUpperCase() + m[2].slice(1);
    }
    return item.model ? (item.model_label || item.model) : 'Codex 订阅';
  }

  function sizeText(bytes) {
    var mb = (bytes || 0) / 1048576;
    if (mb >= 1024) {
      return (mb / 1024).toFixed(1) + ' GB';
    }
    if (mb >= 10) {
      return Math.round(mb) + ' MB';
    }
    if (mb >= 1) {
      return mb.toFixed(1) + ' MB';
    }
    return Math.max(1, Math.round((bytes || 0) / 1024)) + ' KB';
  }

  /** 回收站里还剩几天自动彻底删（worker 每小时清一次，到点那天就写「今天删」） */
  function daysLeft(item) {
    var t = Date.parse(item.purge_at || '');
    return isNaN(t) ? null : Math.ceil((t - Date.now()) / DAY_MS);
  }

  function leftText(d) {
    if (d == null) {
      return '';
    }
    return d <= 0 ? '今天删' : '还剩 ' + d + ' 天';
  }

  function shortPrompt(item, n) {
    var p = String(item.prompt_preview || item.prompt || '').replace(/\s+/g, ' ').trim();
    return p.length > n ? p.slice(0, n) + '…' : p;
  }

  /** 焦点用的 key（只能是小写字母、数字、横线；编号本身就是这样） */
  function keyOf(id) {
    return 'g' + String(id).toLowerCase().replace(/[^a-z0-9-]/g, '');
  }

  function find(id) {
    for (var i = 0; i < G.items.length; i++) {
      if (G.items[i].id === id) {
        return G.items[i];
      }
    }
    return null;
  }

  function sheetJob(id) {
    var sh = S.sheet;
    return sh && sh.mode === 'image' && sh.id === id ? (sh.full || sh.img) : null;
  }

  function imgUrl(id, kind) {
    return '/api/images/' + encodeURIComponent(id) + '/' + kind;
  }

  function emptyState(title, sub) {
    return h('div', { class: 'empty' }, h('span', { class: 'empty-t', text: title }), h('span', { class: 'empty-s', text: sub }));
  }

  // =====================================================================
  // 拉数据
  // =====================================================================
  /** 顶上的数字；返回刚数的（失败返回 null） */
  function loadStats() {
    var req = ++G.statsReq;
    return api('GET', '/api/images/stats').then(function (data) {
      if (req === G.statsReq) {
        G.stats = data;
        renderChips();
        renderSum();
      }
      return data;
    }, function () {
      return null; // 数字旧一点不要紧；网格那边会报错
    });
  }

  function loadGrid(more) {
    var page = more ? G.page + 1 : 1;
    var req = ++G.req;
    G.loading = true;
    $('img-list').setAttribute('aria-busy', 'true');
    return api('GET', '/api/images' + L.qs({ view: G.view, page: page, page_size: PAGE_SIZE })).then(function (data) {
      if (req !== G.req) {
        return;
      }
      G.loading = false;
      G.loaded = true;
      G.err = '';
      if (more) {
        var seen = {};
        G.items.forEach(function (it) { seen[it.id] = true; });
        // 翻页之间新画好一张，下一页会把上一页最后一张再给一次：去掉重复的
        G.items = G.items.concat(data.items.filter(function (it) { return !seen[it.id]; }));
      } else {
        G.items = data.items;
      }
      G.page = data.page;
      G.hasMore = data.has_more;
      renderGrid();
    }, function (err) {
      if (req !== G.req) {
        return;
      }
      G.loading = false;
      if (!more) {
        G.loaded = false;
        G.err = err.message;
      }
      renderGrid();
      toast(err.message);
    });
  }

  function reload() {
    loadStats();
    return loadGrid(false);
  }

  // =====================================================================
  // 顶上：全部 / ⭐ / 回收站 / 选择，下面一行几张、多大
  // =====================================================================
  function renderChips() {
    var wrap = $('img-chips');
    var sl = wrap.scrollLeft;
    var st = G.stats;
    L.clear(wrap);
    var chip = function (view, kids, extra, label) {
      var on = G.view === view;
      return h('button', {
        class: (on ? 'press chip chip-sel' : 'press glass chip') + (extra ? ' ' + extra : ''),
        type: 'button',
        'aria-pressed': on ? 'true' : 'false',
        'aria-label': label || null,
        onclick: function () { setView(view); }
      }, h('span', { class: 'chip-t' }, kids));
    };
    var starred = st ? st.kept.starred : 0;
    var binned = st ? st.trash.count : 0;
    wrap.appendChild(chip('all', '全部'));
    wrap.appendChild(chip('starred', [icon('star', 17, null, G.view === 'starred'), starred ? h('span', { class: 'chip-n', text: starred }) : null],
      'chip-ico chip-star', '只看收藏' + (starred ? '，' + starred + ' 张' : '')));
    wrap.appendChild(chip('trash', ['回收站', binned ? h('span', { class: 'chip-n', text: binned }) : null], null,
      '回收站' + (binned ? '，' + binned + ' 张' : '')));
    if (G.view === 'all') {
      wrap.appendChild(h('button', {
        class: (G.picking ? 'press chip chip-sel' : 'press glass chip') + ' chip-act',
        type: 'button',
        'aria-pressed': G.picking ? 'true' : 'false',
        onclick: function () { setPicking(!G.picking); }
      }, h('span', { class: 'chip-t', text: G.picking ? '取消' : '选择' })));
    }
    wrap.scrollLeft = sl;
  }

  function renderSum() {
    var el = L.clear($('img-sum'));
    var st = G.stats;
    if (G.picking) {
      el.appendChild(h('span', { class: 'imgsum-t', text: '点图勾选；点了⭐的留着，勾不上' }));
      return;
    }
    if (!st) {
      return;
    }
    if (G.view === 'trash') {
      var t = st.trash;
      el.appendChild(h('span', {
        class: 'imgsum-t',
        text: t.count ? t.count + ' 张 · 占 ' + sizeText(t.bytes) + ' · 满 ' + st.trash_days + ' 天自动彻底删除'
          : '删掉的图在这里放 ' + st.trash_days + ' 天，满了自动彻底删除'
      }));
      if (t.count) {
        el.appendChild(h('button', { class: 'press btn-danger', type: 'button', onclick: askEmpty }, '清空回收站'));
      }
      return;
    }
    var line = '共 ' + st.kept.count + ' 张 · 占 ' + sizeText(st.kept.bytes);
    if (st.trash.count) {
      line += ' · 回收站 ' + sizeText(st.trash.bytes);
    }
    el.appendChild(h('span', { class: 'imgsum-t', text: line }));
  }

  function setView(view) {
    if (VIEWS.indexOf(view) === -1) {
      return;
    }
    if (G.picking) {
      setPicking(false);
    }
    if (G.view === view) {
      reload();
      return;
    }
    G.view = view;
    G.items = [];
    G.loaded = false;
    G.err = '';
    renderChips();
    renderSum();
    renderGrid();
    window.scrollTo(0, 0);
    loadGrid(false);
  }

  // =====================================================================
  // 网格
  // =====================================================================
  function renderGrid() {
    var list = $('img-list');
    // 焦点在网格里（键盘用户）：重画后放回同一张，不让它掉回页面开头
    var keep = list.contains(document.activeElement) ? L.focusKeys(document.activeElement) : [];
    L.clear(list);
    list.setAttribute('aria-busy', G.loading ? 'true' : 'false');
    if (!G.loaded) {
      list.appendChild(G.err ? emptyState('没打开', G.err) : h('span', { class: 'loading', text: '正在翻……' }));
      return;
    }
    if (!G.items.length) {
      list.appendChild(emptyState(EMPTY[G.view][0], EMPTY[G.view][1]));
      return;
    }
    var grid = h('div', { class: 'imggrid' });
    G.items.forEach(function (item) {
      grid.appendChild(card(item));
    });
    list.appendChild(grid);
    if (G.hasMore) {
      // 加载中用 aria-disabled 不用 disabled：按钮变灰会把键盘焦点弄丢
      list.appendChild(h('button', {
        class: 'press btn-ghost wide',
        type: 'button',
        'data-key': 'gmore',
        onclick: function (e) {
          var b = e.currentTarget;
          if (G.loading || b.getAttribute('aria-disabled') === 'true') {
            return;
          }
          b.setAttribute('aria-disabled', 'true');
          b.textContent = '正在加载……';
          loadGrid(true);
        }
      }, '加载更多'));
    }
    if (keep.length && L.focusLost()) {
      L.focusByKeys(keep, list);
    }
  }

  function cardLabel(item, when) {
    var lead = '看大图：';
    if (G.picking) {
      lead = item.starred ? '点了⭐，留着：' : (G.picked[item.id] ? '已勾选：' : '勾选：');
    }
    return lead + when + '，' + modelTag(item) + '，' + shortPrompt(item, 24);
  }

  function card(item) {
    var binned = G.view === 'trash';
    var kept = G.picking && item.starred;
    var picked = G.picking && !!G.picked[item.id];
    var when = L.stampLabel(bjStamp(item.created_at), true);
    var left = binned ? daysLeft(item) : null;
    return h('article', { class: 'imgcard' + (picked ? ' picked' : '') + (kept ? ' kept' : '') + (binned ? ' binned' : '') },
      h('button', {
        class: 'imgopen',
        type: 'button',
        'data-key': keyOf(item.id),
        'aria-label': cardLabel(item, when),
        'aria-pressed': G.picking && !kept ? (picked ? 'true' : 'false') : null,
        onclick: function (e) { tapCard(item.id, e.currentTarget); }
      }),
      h('div', { class: 'imgthumb' },
        h('img', {
          src: imgUrl(item.id, 'thumb'), alt: '', loading: 'lazy', decoding: 'async',
          onerror: function (e) { e.currentTarget.style.visibility = 'hidden'; } // 取不到就露出底色，不显示破图标
        }),
        binned ? h('span', { class: 'imgleft' + (left != null && left <= 3 ? ' imgleft-soon' : ''), text: leftText(left) }) : null,
        h('span', { class: 'imgmodel', text: modelTag(item) })),
      h('div', { class: 'imgcap' },
        h('div', { class: 'imgmeta' }, h('span', { class: 'imgdate', text: when })),
        h('span', { class: 'imgprompt', text: item.prompt_preview || '' })),
      G.picking ? (kept ? h('span', { class: 'keptbadge' }, icon('star', 12, null, true), '留着') : h('span', { class: 'imgpick' }, icon('tick', 15))) : null,
      !G.picking && !binned ? starBtn(item, 'imgstar') : null);
  }

  function tapCard(id, btn) {
    var item = find(id);
    if (!item) {
      return;
    }
    if (!G.picking) {
      openImage(item, btn);
      return;
    }
    if (item.starred) {
      toast('点了⭐的留着，要删先取消⭐');
      return;
    }
    if (G.picked[id]) {
      delete G.picked[id];
    } else {
      G.picked[id] = true;
    }
    // 只改这一张，不整片重画（小图不闪）
    btn.parentNode.classList.toggle('picked', !!G.picked[id]);
    btn.setAttribute('aria-pressed', G.picked[id] ? 'true' : 'false');
    btn.setAttribute('aria-label', cardLabel(item, L.stampLabel(bjStamp(item.created_at), true)));
    renderSelbar();
  }

  // =====================================================================
  // ⭐（网格和大图面板共用）
  // =====================================================================
  function starredNow(id) {
    var item = find(id) || sheetJob(id);
    return !!(item && item.starred);
  }

  /** cls = 'imgstar'（网格：小玻璃圆）或 'starbtn shead-star'（面板顶上） */
  function starBtn(item, cls) {
    var on = !!item.starred;
    var face = cls === 'imgstar';
    var ico = icon('star', face ? 18 : 22, null, on);
    return h('button', {
      class: 'press ' + cls + (on ? ' star-on' : ''),
      type: 'button',
      'data-imgstar': item.id,
      'aria-pressed': on ? 'true' : 'false',
      'aria-label': '收藏这张图',
      onclick: function (e) { setStar(item.id, !starredNow(item.id), e.currentTarget); }
    }, face ? h('span', { class: 'starface' }, ico) : ico);
  }

  /** 网格、面板里同一张图的⭐都跟着变；面板里「删除」能不能按也跟着⭐走 */
  function paintStar(id, on) {
    G.items.forEach(function (it) {
      if (it.id === id) {
        it.starred = on;
      }
    });
    var sh = S.sheet;
    if (sh && sh.mode === 'image' && sh.id === id) {
      sh.img.starred = on;
      if (sh.full) {
        sh.full.starred = on;
      }
    }
    document.querySelectorAll('[data-imgstar]').forEach(function (b) {
      if (b.getAttribute('data-imgstar') !== id) {
        return;
      }
      b.classList.toggle('star-on', on);
      b.classList.remove('star-pop');
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      var old = b.querySelector('svg');
      old.parentNode.replaceChild(icon('star', b.classList.contains('imgstar') ? 18 : 22, null, on), old);
    });
    if (sh && sh.mode === 'image' && sh.id === id) {
      renderImageBody(sh);
    }
  }

  function sendStar(id, write) {
    var sent = write.wanted;
    write.busy = true;
    api('POST', '/api/images/' + encodeURIComponent(id) + '/star', { starred: sent }).then(function () {
      write.saved = sent;
      write.busy = false;
      if (write.wanted !== sent) {
        sendStar(id, write); // 上一次落定后再写最后一次点选，回包不会倒着覆盖
        return;
      }
      delete starWrites[id];
      paintStar(id, write.wanted);
      loadStats();
    }, function (err) {
      delete starWrites[id];
      paintStar(id, write.saved);
      toast(err.message); // 比如刚被小克放进回收站：回收站里的不能点⭐
      reload();
    });
  }

  /** 点⭐立即亮，连续点同一张排队；只看⭐时取消，图先留在眼前，换筛选再走（同记忆库） */
  function setStar(id, on, btn) {
    var write = starWrites[id];
    if (!write) {
      write = starWrites[id] = { saved: !on, wanted: on, busy: false };
    }
    write.wanted = on;
    paintStar(id, on);
    if (on && btn && document.contains(btn)) {
      btn.classList.add('star-pop');
    }
    if (!write.busy) {
      sendStar(id, write);
    }
  }

  // =====================================================================
  // 勾选几张一起放进回收站
  // =====================================================================
  function setPicking(on) {
    G.picking = on;
    G.picked = {};
    $('tabbar').hidden = on;
    $('img-selbar').hidden = !on;
    renderChips();
    renderSum();
    renderGrid();
    renderSelbar();
  }

  function renderSelbar() {
    var n = Object.keys(G.picked).length;
    $('img-selcount').textContent = '已选 ' + n + ' 张';
    $('img-seltrash').setAttribute('aria-disabled', n && !G.busy ? 'false' : 'true');
  }

  function trashPicked() {
    var ids = Object.keys(G.picked);
    if (!ids.length || G.busy) {
      return;
    }
    G.busy = true;
    renderSelbar();
    api('POST', '/api/images/trash', { ids: ids }).then(function (data) {
      G.busy = false;
      var done = [];
      var refused = [];
      data.results.forEach(function (r) {
        if (r.ok && r.status === 'trashed') {
          done.push(r.id);
        } else if (!r.ok) {
          refused.push(r);
        }
      });
      setPicking(false);
      // 勾的时候没⭐、到了 worker 那边刚被点了⭐的（或者正被排队的画图当参考图的）不动，说一声
      var msg = done.length ? done.length + ' 张放进了回收站' : '一张都没放进去';
      if (refused.length) {
        msg += '；' + refused.length + ' 张没动（' + (WHY[refused[0].code] || refused[0].message) + '）';
      }
      toast(msg, done.length ? { label: '撤回', run: function () { undoTrash(done); } } : null);
      reload();
    }, function (err) {
      G.busy = false;
      renderSelbar();
      toast(err.message);
    });
  }

  /** 防手滑：放进回收站以后提示条上的「撤回」= 一张张恢复 */
  function undoTrash(ids) {
    Promise.all(ids.map(function (id) {
      return api('POST', '/api/images/' + encodeURIComponent(id) + '/restore').then(function () { return true; }, function () { return false; });
    })).then(function (oks) {
      var back = oks.filter(Boolean).length;
      toast(back === ids.length ? '撤回了' : '撤回了 ' + back + ' 张，' + (ids.length - back) + ' 张没撤回');
      reload();
    });
  }

  // =====================================================================
  // 回收站：清空（确认一次）
  // =====================================================================
  function askEmpty() {
    if (G.busy) {
      return;
    }
    loadStats().then(function (st) { // 确认框里写的张数用刚数的
      if (!st) {
        toast('没数清回收站里有几张，稍后再试');
        return;
      }
      if (!st.trash.count) {
        toast('回收站已经是空的');
        reload();
        return;
      }
      L.confirmDialog({
        title: '清空回收站？',
        msg: '回收站里的 ' + st.trash.count + ' 张图会彻底删除，原图和预览都没了，没法恢复。',
        ok: '清空'
      }).then(function (ok) {
        if (!ok || G.busy) {
          return;
        }
        G.busy = true;
        // 只清确认时看到的那些：点确认之后才进回收站的（比如小克刚删的）不动
        api('POST', '/api/images/trash/empty', { until: st.trash.latest }).then(function (data) {
          G.busy = false;
          toast('彻底删除了 ' + data.purged + ' 张' + (data.left ? '；有 ' + data.left + ' 张是刚放进来的，没动' : ''));
          reload();
        }, function (err) {
          G.busy = false;
          toast(err.message);
          reload();
        });
      });
    });
  }

  // =====================================================================
  // 点开看大图（借记忆库的底部面板）
  // =====================================================================
  function openImage(item, opener) {
    var sh = S.sheet = {
      mode: 'image', id: item.id, img: item, full: null, opener: opener || null, focusKeys: L.focusKeys(opener), req: 0, busy: false
    };
    Sheet().show();
    renderImageSheet(sh);
    fetchFull(sh);
  }

  function fetchFull(sh) {
    var req = ++sh.req;
    api('GET', '/api/images/' + encodeURIComponent(sh.id)).then(function (data) {
      if (S.sheet !== sh || req !== sh.req) {
        return;
      }
      if (data.job.purged_at) {
        toast('这张已经彻底删除了');
        Sheet().close();
        reload();
        return;
      }
      if (starWrites[sh.id]) {
        data.job.starred = starWrites[sh.id].wanted; // 正在写⭐：以这边刚点的为准
      }
      sh.full = data.job;
      renderImageSheet(sh);
    }, function (err) {
      if (S.sheet !== sh || req !== sh.req) {
        return;
      }
      toast(err.message);
      if (err.status === 404) {
        Sheet().close();
        reload();
      }
    });
  }

  /** 面板顶上不跟着滚的那一条：日子、⭐（回收站里的没有）、关闭 */
  function renderImageSheet(sh) {
    if (S.sheet !== sh) {
      return;
    }
    var job = sh.full || sh.img;
    var title = longStamp(job.created_at);
    $('sheet-dialog').setAttribute('aria-label', '图片 · ' + title);
    var head = L.clear($('sheet-head'));
    head.hidden = false;
    head.appendChild(h('h2', { class: 'stitle', text: title }));
    if (!job.trashed_at) {
      head.appendChild(starBtn(job, 'starbtn shead-star'));
    }
    head.appendChild(h('button', {
      class: 'press closebtn', type: 'button', 'aria-label': '关闭', onclick: function () { Sheet().requestClose(); }
    }, icon('x', 16)));
    renderImageBody(sh);
  }

  function renderImageBody(sh) {
    if (S.sheet !== sh) {
      return;
    }
    var el = $('sheet');
    var top = el.scrollTop; // 拉完全文、点⭐后重画，不让面板跳回顶上
    var job = sh.full || sh.img;
    var binned = !!job.trashed_at;
    L.clear(el);
    el.appendChild(h('img', { class: 'imgbig', src: imgUrl(sh.id, 'preview'), alt: '这张图的预览' }));
    var facts = [job.model ? modelTag(job) : (job.model_label || 'Codex 订阅')];
    if (job.width && job.height) {
      facts.push(job.width + ' × ' + job.height);
    }
    if (job.image_bytes) {
      facts.push(sizeText(job.image_bytes));
    }
    if (typeof job.cost_usd === 'number') {
      facts.push('花了 $' + job.cost_usd.toFixed(job.cost_usd < 0.1 ? 3 : 2));
    }
    el.appendChild(h('div', { class: 'imgfacts' }, facts.map(function (t) { return h('span', { class: 'tag tag-no', text: t }); })));
    if (binned) {
      var purgeDay = dayText(bjStamp(job.purge_at));
      el.appendChild(h('div', { class: 'imgnote' }, icon('clock', 18),
        h('span', { text: (purgeDay ? purgeDay + '自动彻底删除（' + leftText(daysLeft(job)) + '）。' : '') + '恢复了就回到「全部」里。' })));
      el.appendChild(h('div', { class: 'grid2 imgbtns' },
        h('button', { class: 'press btn-soft bigbtn', type: 'button', onclick: function () { restoreOne(sh); } }, icon('revert', 18), '恢复'),
        h('button', { class: 'press btn-danger bigbtn', type: 'button', onclick: function () { askPurge(sh); } }, icon('trash', 18), '彻底删除')));
    } else {
      if (job.starred) {
        el.appendChild(h('div', { class: 'imgnote imgnote-star' }, icon('star', 18, null, true), h('span', { text: '点了⭐，留着。要删先取消⭐。' })));
      }
      el.appendChild(h('div', { class: 'grid2 imgbtns' },
        h('a', { class: 'press btn-soft bigbtn', href: imgUrl(sh.id, 'original'), target: '_blank', rel: 'noopener' }, icon('open', 18), '看原图'),
        // 点了⭐：样子变灰，点了说一声（不用 disabled，免得键盘焦点丢）
        h('button', {
          class: 'press btn-danger bigbtn', type: 'button', 'aria-disabled': job.starred ? 'true' : null,
          onclick: function () { trashOne(sh); }
        }, icon('trash', 18), '删除')));
    }
    el.appendChild(h('span', { class: 'imgptitle', text: '提示词' }));
    el.appendChild(h('span', { class: 'imgptext', text: sh.full ? sh.full.prompt : shortPrompt(job, 80) }));
    el.scrollTop = top;
  }

  function trashOne(sh) {
    var job = sh.full || sh.img;
    if (job.starred) {
      toast('点了⭐的不能删，先取消⭐');
      return;
    }
    if (sh.busy) {
      return;
    }
    sh.busy = true;
    api('POST', '/api/images/trash', { ids: [sh.id] }).then(function (data) {
      sh.busy = false;
      var r = data.results[0];
      if (!r.ok) {
        toast(r.message);
        if (S.sheet === sh) {
          fetchFull(sh);
        }
        reload();
        return;
      }
      if (S.sheet === sh) {
        Sheet().close();
      }
      toast('放进了回收站', { label: '撤回', run: function () { undoTrash([sh.id]); } });
      reload();
    }, function (err) {
      sh.busy = false;
      toast(err.message);
    });
  }

  function restoreOne(sh) {
    if (sh.busy) {
      return;
    }
    sh.busy = true;
    api('POST', '/api/images/' + encodeURIComponent(sh.id) + '/restore').then(function () {
      sh.busy = false;
      if (S.sheet === sh) {
        Sheet().close();
      }
      toast('恢复了，回到「全部」里');
      reload();
    }, function (err) {
      sh.busy = false;
      toast(err.message);
      if (S.sheet === sh) {
        Sheet().close();
      }
      reload();
    });
  }

  function askPurge(sh) {
    L.confirmDialog({ title: '彻底删除这张图？', msg: '原图和预览都会删掉，没法再恢复。', ok: '彻底删除' }).then(function (ok) {
      if (!ok || S.sheet !== sh || sh.busy) {
        return;
      }
      sh.busy = true;
      api('POST', '/api/images/' + encodeURIComponent(sh.id) + '/purge').then(function () {
        sh.busy = false;
        if (S.sheet === sh) {
          Sheet().close();
        }
        toast('彻底删除了');
        reload();
      }, function (err) {
        sh.busy = false;
        toast(err.message); // 比如刚被恢复了：不在回收站里的不删
        if (S.sheet === sh) {
          fetchFull(sh);
        }
        reload();
      });
    });
  }

  // =====================================================================
  // 启动
  // =====================================================================
  /** 切到图片 tab：数字和网格都拉一遍新的（小克可能刚删了几张） */
  function show() {
    renderChips();
    renderSum();
    if (!G.loaded) {
      renderGrid();
    }
    reload();
  }

  /** 顶上那一排贴顶时才垫渐隐底色（同记忆库） */
  function watchSticky() {
    var st = $('img-sticky');
    if (!('IntersectionObserver' in window)) {
      st.classList.add('stuck');
      return;
    }
    new window.IntersectionObserver(function (entries) {
      st.classList.toggle('stuck', !entries[0].isIntersecting);
    }).observe($('img-stick-sentinel'));
  }

  function init() {
    watchSticky();
    $('img-seltrash').addEventListener('click', trashPicked);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && G.picking && !L.alertOpen() && !S.sheet) {
        e.preventDefault();
        setPicking(false);
      }
    });
    renderChips();
  }

  window.EmberImages = { show: show };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
