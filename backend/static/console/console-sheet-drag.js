/* ember 记忆库网页 · 底部面板往上推，整个展开到顶。
   打开时是「半高」：内容多高面板就多高，最高 86%。手指在面板上往上推，面板跟着手指长高，
   松手按速度和距离决定：展开到顶（顶上只留状态栏）或者弹回半高；展开以后滚到最上面再往下拉，收回半高。
   电脑上在面板里往下滚滚轮，也会先展开到顶。
   跟手的规矩照 apple-design：拖的时候 1:1 跟着、拖过头越拉越紧（橡皮筋）、松手用手指的速度接一个弹簧，
   中途能一把抓住；系统开了「减少动态」就不弹，直接到位。 */
(function () {
  'use strict';

  var L = window.EmberLib;

  var SLOP = 6;           // 手指先动这么多再判断是推面板还是别的（点按钮不受影响）
  var FLICK = 0.11;       // px/ms：快速一甩，不看距离（emil-design-eng 的数）
  var COMMIT = 80;        // 慢慢拖：拖过这么多（或剩下距离的 1/4）就算
  var STIFF = 320;        // 弹簧：response ≈ 0.35s
  var DAMP = 2 * 0.86 * Math.sqrt(STIFF); // 阻尼比 0.86：到顶时几乎不过冲，甩的时候有一点点弹

  var dialog = null;
  var inner = null;
  var full = false;
  var st = null;          // 正在拖：{y0, x0, h0, mode: null | 'grow' | 'shrink', lo, hi, pts}
  var anim = 0;
  var reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  function height() {
    return dialog.getBoundingClientRect().height;
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
  }

  function settle(toFull) {
    stop();
    full = toFull;
    dialog.classList.toggle('sheet-full', toFull);
    dialog.classList.remove('sheet-dragging');
    dialog.style.height = '';
  }

  /** 从现在的高度、带着手指的速度（px/s，正 = 变高）弹到目标 */
  function springTo(toFull, v0) {
    var from = height();
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
    var grabbed = !!anim;
    stop(); // 弹的途中一把抓住：从现在的高度接着拖
    var t = e.touches[0];
    st = { y0: t.clientY, x0: t.clientX, h0: height(), mode: null, pts: [], grabbed: grabbed };
    if (grabbed) {
      dialog.style.height = st.h0 + 'px';
    }
  }

  function onMove(e) {
    if (!st) {
      return;
    }
    if (e.touches.length !== 1) { // 第二根手指放上来：不跟了，回到最近的一头
      st = null;
      springTo(full, 0);
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
      if (!full && dy < 0) {
        st.mode = 'grow';
      } else if (full && dy > 0 && inner.scrollTop <= 0) {
        st.mode = 'shrink';
      } else {
        if (st.grabbed) {
          springTo(full, 0); // 抓住了又不拖：回到原来那头
        }
        st = null; // 交给面板里面自己滚
        return;
      }
      st.hi = measure(true);
      st.lo = st.mode === 'grow' ? Math.min(st.h0, st.hi) : measure(false);
      st.y0 = t.clientY; // 从判断好的这一点算起，不跳
      dy = 0;
      dialog.classList.add('sheet-dragging');
    }
    if (e.cancelable) {
      e.preventDefault(); // 推面板的时候，里面的内容不跟着滚
    }
    st.pts.push([e.timeStamp, t.clientY]);
    if (st.pts.length > 8) {
      st.pts.shift();
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
    if (!full && !anim && e.deltaY > 0) {
      springTo(true, 0);
    }
  }

  function bind() {
    dialog = document.getElementById('sheet-dialog');
    inner = document.getElementById('sheet');
    dialog.addEventListener('touchstart', onStart, { passive: true });
    dialog.addEventListener('touchmove', onMove, { passive: false });
    dialog.addEventListener('touchend', onEnd);
    dialog.addEventListener('touchcancel', onEnd);
    inner.addEventListener('wheel', onWheel, { passive: true });
  }

  window.EmberSheetDrag = {
    /** 每次打开面板：回到半高 */
    reset: function () {
      st = null;
      if (dialog) {
        settle(false);
      }
    },
    isFull: function () {
      return full;
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
