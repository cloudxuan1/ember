/* ember 记忆库网页 · 底部面板往上推，整个展开到顶；往下拉，一级一级收：到顶 → 半高 → 关掉。
   打开时是「半高」：内容多高面板就多高，最高 86%。手指在面板上往上推，面板跟着手指长高，
   松手按速度和距离决定：展开到顶（顶上只留状态栏）或者弹回半高；展开以后滚到最上面再往下拉，收回半高。
   半高时滚到最上面再往下拉：面板整个跟着手指往下走、遮罩跟着变淡，松手按速度和距离决定关掉还是弹回去。
   看大图的面板（.sheet-tall）一打开就是内容的高度、最高到顶，没有「往上推展开」这一步：往上滑直接滚里面，往下拉就是关；
   内容后来变了（拉完全文、点⭐多一行字）面板要变高变矮时，从原来的高度弹过去，不一下跳。
   电脑上在面板里往下滚滚轮，也会先展开到顶。
   跟手的规矩照 apple-design：拖的时候 1:1 跟着、拖过头越拉越紧（橡皮筋）、松手用手指的速度接一个弹簧，
   中途能一把抓住；系统开了「减少动态」就不弹，直接到位。 */
(function () {
  'use strict';

  var L = window.EmberLib;
  var Sheet = function () { return window.EmberSheet; };

  var SLOP = 6;           // 手指先动这么多再判断是推面板还是别的（点按钮不受影响）
  var FLICK = 0.11;       // px/ms：快速一甩，不看距离（emil-design-eng 的数）
  var COMMIT = 80;        // 慢慢拖：拖过这么多（或剩下距离的 1/4）就算
  var STIFF = 320;        // 弹簧：response ≈ 0.35s
  var DAMP = 2 * 0.86 * Math.sqrt(STIFF); // 阻尼比 0.86：到顶时几乎不过冲，甩的时候有一点点弹

  var dialog = null;
  var inner = null;
  var scrim = null;
  var full = false;
  var st = null;          // 正在拖：{y0, x0, h0, mode: null | 'grow' | 'shrink' | 'drop', lo, hi, pts}
  var anim = 0;
  var dropping = false;   // 正在播的是「往下拉关」那一段（弹回去或者滑出去），一把抓住时接着拖
  var off = 0;            // 往下拉关：面板往下挪了多少 px（0 = 原位）
  var dropH = 0;          // 往下拉关：面板多高（滑出屏幕要走多远、遮罩按它算淡多少）
  var reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  function height() {
    return dialog.getBoundingClientRect().height;
  }

  function tall() {
    return dialog.classList.contains('sheet-tall');
  }

  /** 在输入框里按下去的不算拉面板：选字、挪光标不能把面板拉走 */
  function inField(el) {
    return !!(el && el.closest && el.closest('input, textarea, select, [contenteditable="true"]'));
  }

  /** 往下拉关：面板往下挪（只动 translate，不重排里面的内容），遮罩跟着变淡 */
  function setOff(o) {
    off = o;
    dialog.style.translate = o ? '0 ' + o + 'px' : '';
    if (!scrim) {
      return;
    }
    if (o > 0 && dropH > 0) {
      // 遮罩自己有淡入动画，动画的值盖过普通的行内样式，所以这里要 important
      scrim.style.setProperty('opacity', String(Math.max(0, 1 - o / dropH)), 'important');
    } else {
      scrim.style.removeProperty('opacity');
    }
  }

  /** 量一下面板在某个状态下的高度（同步改类名量完再改回去，中间不会画出来） */
  function measure(asFull) {
    var hadFull = dialog.classList.contains('sheet-full');
    var hadDrag = dialog.classList.contains('sheet-dragging');
    var inline = dialog.style.height;
    dialog.classList.toggle('sheet-full', asFull);
    dialog.classList.remove('sheet-dragging');
    dialog.style.height = '';
    var v = height();
    dialog.classList.toggle('sheet-full', hadFull);
    dialog.classList.toggle('sheet-dragging', hadDrag);
    dialog.style.height = inline;
    return v;
  }

  function stop() {
    if (anim) {
      cancelAnimationFrame(anim);
      anim = 0;
    }
    dropping = false;
  }

  function settle(toFull) {
    stop();
    full = toFull;
    dialog.classList.toggle('sheet-full', toFull);
    dialog.classList.remove('sheet-dragging');
    dialog.style.height = '';
  }

  /** 从现在的高度（或给定的 fromH）、带着手指的速度（px/s，正 = 变高）弹到目标 */
  function springTo(toFull, v0, fromH) {
    var from = fromH == null ? height() : fromH;
    var target = measure(toFull);
    stop();
    full = toFull;
    if (reduce.matches || Math.abs(target - from) < 1) {
      settle(toFull);
      return;
    }
    dialog.classList.add('sheet-dragging');
    dialog.style.height = from + 'px';
    var x = from;
    var v = v0 || 0;
    var last = 0;
    var step = function (now) {
      var dt = last ? Math.min(0.032, (now - last) / 1000) : 1 / 60;
      last = now;
      for (var i = 0; i < 4; i++) { // 一帧分四小步算，掉帧也稳
        var a = -STIFF * (x - target) - DAMP * v;
        v += a * dt / 4;
        x += v * dt / 4;
      }
      if (Math.abs(x - target) < 0.5 && Math.abs(v) < 8) {
        settle(toFull);
        return;
      }
      dialog.style.height = x + 'px';
      anim = requestAnimationFrame(step);
    };
    anim = requestAnimationFrame(step);
  }

  /** 往下拉关松手以后：从现在挪到的位置、带着手指的速度（px/s，正 = 往下）弹到 target。
      target 是 0 就是弹回原位；比面板高就是滑出屏幕，整个出去了叫 done(true) 收尾。
      系统开了「减少动态」：不滑，回原位后 done(false)，照平常那样淡出关掉。 */
  function dropTo(target, v0, done) {
    stop();
    var out = target > 0;
    if (reduce.matches) {
      setOff(0);
      dialog.classList.remove('sheet-swiping');
      if (out) {
        done(false);
      }
      return;
    }
    dropping = true;
    dialog.classList.add('sheet-swiping');
    var x = off;
    var v = v0 || 0;
    var last = 0;
    var step = function (now) {
      var dt = last ? Math.min(0.032, (now - last) / 1000) : 1 / 60;
      last = now;
      for (var i = 0; i < 4; i++) {
        var a = -STIFF * (x - target) - DAMP * v;
        v += a * dt / 4;
        x += v * dt / 4;
      }
      if (out ? x >= dropH : Math.abs(x) < 0.5 && Math.abs(v) < 8) {
        anim = 0;
        dropping = false;
        dialog.classList.remove('sheet-swiping');
        setOff(out ? dropH : 0);
        if (out) {
          done(true);
        }
        return;
      }
      setOff(Math.max(0, x));
      anim = requestAnimationFrame(step);
    };
    anim = requestAnimationFrame(step);
  }

  /** 拖过头：越往外拉越紧，不是一下子停死 */
  function rubber(over, dim) {
    return (over * dim * 0.55) / (dim + 0.55 * over);
  }

  /** 松手前 ~100ms 的速度，px/ms（正 = 手指往下）；手指停住再松开算 0 */
  function velocity(pts, endT) {
    var n = pts.length;
    if (n < 2 || endT - pts[n - 1][0] > 80) {
      return 0;
    }
    var j = n - 2;
    while (j > 0 && pts[n - 1][0] - pts[j][0] < 100) {
      j--;
    }
    var dt = pts[n - 1][0] - pts[j][0];
    return dt > 0 ? (pts[n - 1][1] - pts[j][1]) / dt : 0;
  }

  function onStart(e) {
    if (e.touches.length !== 1 || L.alertOpen()) {
      st = null;
      return;
    }
    var t = e.touches[0];
    if (dropping) { // 往下拉关松了手、还在弹回去 / 滑出去的途中一把抓住：从现在的位置接着拖
      stop();
      st = { y0: t.clientY, x0: t.clientX, mode: 'drop', off0: off, pts: [] };
      dialog.classList.add('sheet-swiping');
      return;
    }
    var grabbed = !!anim;
    stop(); // 弹的途中一把抓住：从现在的高度接着拖
    st = { y0: t.clientY, x0: t.clientX, h0: height(), mode: null, pts: [], grabbed: grabbed, field: inField(e.target) };
    if (grabbed) {
      dialog.style.height = st.h0 + 'px';
    }
  }

  function onMove(e) {
    if (!st) {
      return;
    }
    if (e.touches.length !== 1) { // 第二根手指放上来：不跟了，回到最近的一头
      var was = st;
      st = null;
      if (was.mode === 'drop') {
        dropTo(0, 0, null);
      } else {
        springTo(full, 0);
      }
      return;
    }
    var t = e.touches[0];
    var dy = t.clientY - st.y0;
    if (!st.mode) {
      var dx = t.clientX - st.x0;
      if (Math.abs(dy) < SLOP && Math.abs(dx) < SLOP) {
        return;
      }
      if (Math.abs(dx) > Math.abs(dy)) {
        st = null;
        return;
      }
      if (!full && dy < 0 && !tall()) {
        st.mode = 'grow';
      } else if (full && dy > 0 && inner.scrollTop <= 0) {
        st.mode = 'shrink';
      } else if (!full && dy > 0 && inner.scrollTop <= 0 && !st.field && !st.grabbed) {
        st.mode = 'drop'; // 半高、已经在最上面，还往下拉：要关了
      } else {
        if (st.grabbed) {
          springTo(full, 0); // 抓住了又不拖：回到原来那头
        }
        st = null; // 交给面板里面自己滚
        return;
      }
      st.y0 = t.clientY; // 从判断好的这一点算起，不跳
      dy = 0;
      if (st.mode === 'drop') {
        st.off0 = 0;
        dropH = height();
        dialog.classList.add('sheet-swiping');
      } else {
        st.hi = measure(true);
        st.lo = st.mode === 'grow' ? Math.min(st.h0, st.hi) : measure(false);
        dialog.classList.add('sheet-dragging');
      }
    }
    if (e.cancelable) {
      e.preventDefault(); // 推面板的时候，里面的内容不跟着滚
    }
    st.pts.push([e.timeStamp, t.clientY]);
    if (st.pts.length > 8) {
      st.pts.shift();
    }
    if (st.mode === 'drop') {
      setOff(Math.max(0, st.off0 + dy)); // 1:1 跟着手指；推回原位就停在原位
      return;
    }
    var hNow = st.h0 - dy;
    if (hNow > st.hi) {
      hNow = st.hi + rubber(hNow - st.hi, st.hi);
    } else if (hNow < st.lo) {
      hNow = st.lo - rubber(st.lo - hNow, st.lo);
    }
    dialog.style.height = hNow + 'px';
  }

  function onEnd(e) {
    var s = st;
    st = null;
    if (!s) {
      return;
    }
    if (!s.mode) {
      if (s.grabbed) {
        springTo(full, 0);
      }
      return;
    }
    if (s.mode === 'drop') {
      // 跟展开 / 收回同一个规矩：松手时还在往下甩就关，往回推就弹回去；手停住了看拉了多远
      var vd = velocity(s.pts, e.timeStamp); // px/ms，正 = 往下
      var close = vd > FLICK || (vd > -FLICK && off > Math.min(COMMIT, dropH / 4));
      if (!close || !Sheet()) {
        dropTo(0, vd * 1000, null);
      } else if (Sheet().isDirty()) {
        dropTo(0, vd * 1000, null); // 写了一半没存：先弹回去，再问一句（跟点遮罩、按关闭一样）
        Sheet().requestClose();
      } else {
        dropTo(dropH + 24, vd * 1000, function (swiped) {
          Sheet().close(swiped ? { swiped: true } : null);
        });
      }
      return;
    }
    var hNow = height();
    var vh = -velocity(s.pts, e.timeStamp); // 高度变化的速度：正 = 变高
    var toFull;
    if (s.mode === 'grow') {
      toFull = vh > FLICK || (vh > -FLICK && hNow - s.lo > Math.min(COMMIT, (s.hi - s.lo) / 4));
    } else {
      toFull = !(vh < -FLICK || (vh < FLICK && s.hi - hNow > Math.min(COMMIT, (s.hi - s.lo) / 4)));
    }
    springTo(toFull, vh * 1000);
  }

  function onWheel(e) {
    if (!full && !anim && e.deltaY > 0 && !tall()) {
      springTo(true, 0);
    }
  }

  function bind() {
    dialog = document.getElementById('sheet-dialog');
    inner = document.getElementById('sheet');
    scrim = document.getElementById('scrim');
    dialog.addEventListener('touchstart', onStart, { passive: true });
    dialog.addEventListener('touchmove', onMove, { passive: false });
    dialog.addEventListener('touchend', onEnd);
    dialog.addEventListener('touchcancel', onEnd);
    inner.addEventListener('wheel', onWheel, { passive: true });
  }

  window.EmberSheetDrag = {
    /** 每次打开面板：回到半高、回到原位（上次往下拉关留下的挪动和遮罩淡度清掉） */
    reset: function () {
      st = null;
      if (dialog) {
        settle(false);
        dialog.classList.remove('sheet-swiping');
        setOff(0);
        dropH = 0;
      }
    },
    isFull: function () {
      return full;
    },
    /** 换内容（update 里重画）：面板是「内容多高就多高」的，高度变了就从原来的高度弹过去。
        用 offsetHeight（排版高度）：刚打开那半秒面板还在缩放着出来，量外框会差几像素。
        正在拖、正在弹、展开到顶（高度不看内容）、减少动态时直接换。 */
    resize: function (update) {
      if (!dialog || st || anim || dropping || full || reduce.matches) {
        update();
        return;
      }
      var from = dialog.offsetHeight;
      update();
      if (Math.abs(dialog.offsetHeight - from) >= 1) {
        springTo(false, 0, from);
      }
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
