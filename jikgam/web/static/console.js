// 화면 동작. 서버가 만든 HTML의 data-* 속성을 보고 동작만 붙인다. 글자는 만들지 않는다(ADR 0022).
//   data-autofocus        열린 드로어의 제목으로 포커스를 옮긴다
//   data-drawer="주소"     Escape를 누르면 그 주소로 가서 드로어를 닫는다
//   data-player           녹음 재생 위치에 맞춰 data-cue-start/end 요소에 is-playing을 붙인다.
//                         data-start(초)에서 시작, data-stops(초 목록)와 data-stop-toggle 상자로 판단 시점마다 멈춘다
//   data-live="주소"       data-interval(밀리초)마다 그 주소의 문서를 다시 받아, 안의 data-slot 영역에서 바뀐 부분만 맞춘다.
//                         이미 있는 요소는 그대로 두므로 색이 바뀌는 상태는 CSS 전환으로 부드럽게 바뀐다
//   data-key              갱신·영역 교체 때 같은 이름의 요소를 같은 것으로 본다. 새로 붙은 요소에는 c-enter를 잠깐 붙인다
//   data-now              실시간 대화의 시계(재생 피드 시각, 밀리초). data-running이 false면 멈춘 통화다
//   data-reveal-from/to   그 시각 사이에 .reveal 안의 글자(.rv)를 차례로 드러낸다(말하는 중인 AI 답)
//   data-follow           따로 스크롤되는 영역. 맨 아래를 보고 있으면 새 줄과 글자를 따라 내린다
//   data-swap             같은 화면의 다른 상태로 가는 링크를 누르면 문서를 새로 열지 않고 이 영역만 바꾼다.
//                         data-key가 같은 순위 행은 새 자리로 옮겨 가고 막대는 새 길이로 바뀐다
//   data-listen="audio id" 누르면 그 녹음을 data-audio-at 요소가 가리키는 지금 위치부터 재생하거나 멈춘다
//   data-cursor           재생 위치를 전체 길이 대비 비율(--at)로 옮긴다
// 움직임의 시간과 곡선은 tokens.css의 --motion-*, --ease-*에서 읽는다. 움직임 줄이기 설정이면 움직이지 않는다.
(function () {
  "use strict";

  var OWN = "c-";  // 스크립트가 붙이는 클래스의 접두어. 갱신 때 서버 내용으로 지우지 않는다
  var reduceQuery = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

  function onReady(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

  function reduced() {
    return !!(reduceQuery && reduceQuery.matches);
  }

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function duration(name) {
    var v = token(name);
    var n = parseFloat(v);
    if (isNaN(n)) return 0;
    return /ms$/.test(v) ? n : n * 1000;
  }

  // --- 바뀐 부분만 맞추기(morph) -------------------------------------------------------------------

  function keyOf(node) {
    return node.nodeType === 1 ? node.getAttribute("data-key") : null;
  }

  function syncClass(from, to) {
    var own = [];
    from.classList.forEach(function (c) { if (c.indexOf(OWN) === 0) own.push(c); });
    var want = (to.getAttribute("class") || "").split(/\s+/).filter(Boolean).concat(own).join(" ");
    if ((from.getAttribute("class") || "") === want) return;
    if (want) from.setAttribute("class", want);
    else from.removeAttribute("class");
  }

  function syncAttrs(from, to) {
    var i, a;
    for (i = from.attributes.length - 1; i >= 0; i--) {
      a = from.attributes[i];
      if (a.name !== "class" && !to.hasAttribute(a.name)) from.removeAttribute(a.name);
    }
    for (i = 0; i < to.attributes.length; i++) {
      a = to.attributes[i];
      if (a.name !== "class" && from.getAttribute(a.name) !== a.value) from.setAttribute(a.name, a.value);
    }
    syncClass(from, to);
  }

  function morphNode(from, to, entered) {
    if (from.nodeType !== 1) {
      if (from.nodeValue !== to.nodeValue) from.parentNode.replaceChild(document.importNode(to, true), from);
      return;
    }
    if (from.nodeName !== to.nodeName) {
      var fresh = document.importNode(to, true);
      from.parentNode.replaceChild(fresh, from);
      entered.push(fresh);
      return;
    }
    syncAttrs(from, to);
    morphChildren(from, to, entered);
  }

  // from의 자식을 to의 자식과 같게 만든다. data-key가 있는 자식은 이름으로, 없는 자식은 순서로 짝짓는다.
  // 짝이 있는 요소는 그대로 두고(필요하면 자리만 옮긴다) 속성과 안쪽만 맞춘다.
  function morphChildren(from, to, entered) {
    var sig = from.getAttribute && from.getAttribute("data-reveal-sig");
    if (sig !== null && sig !== undefined && sig === to.getAttribute("data-reveal-sig")) return;  // 글자 공개는 스크립트가 맡는다
    var olds = Array.prototype.slice.call(from.childNodes);
    var keyed = {};
    olds.forEach(function (n) { var k = keyOf(n); if (k !== null) keyed[k] = n; });
    var used = new Set();
    var cursor = 0;
    var ref = from.firstChild;
    Array.prototype.slice.call(to.childNodes).forEach(function (n) {
      var k = keyOf(n);
      var match = null;
      if (k !== null) {
        match = keyed[k] || null;
        if (match && (used.has(match) || match.nodeName !== n.nodeName)) match = null;
      } else {
        while (cursor < olds.length && (used.has(olds[cursor]) || keyOf(olds[cursor]) !== null)) cursor++;
        var cand = olds[cursor];
        if (cand && cand.nodeType === n.nodeType && cand.nodeName === n.nodeName) {
          match = cand;
          cursor++;
        }
      }
      if (match) {
        used.add(match);
        if (match === ref) ref = ref.nextSibling;
        else from.insertBefore(match, ref);
        morphNode(match, n, entered);
      } else {
        var fresh = document.importNode(n, true);
        from.insertBefore(fresh, ref);
        if (fresh.nodeType === 1) entered.push(fresh);
      }
    });
    olds.forEach(function (n) { if (!used.has(n) && n.parentNode === from) from.removeChild(n); });
  }

  // 새로 붙은 이름 있는 요소(대화 줄, 구분선, 순위 행, 근거)에 나타나는 움직임을 한 번 준다
  function enter(nodes) {
    if (reduced()) return;
    var keep = duration("--motion-emphasis") + duration("--motion-base");
    nodes.forEach(function (el) {
      if (!el.hasAttribute || !el.hasAttribute("data-key")) return;
      el.classList.add(OWN + "enter");
      setTimeout(function () { el.classList.remove(OWN + "enter"); }, keep);
    });
  }

  // --- 실시간 대화: 시계, 글자 공개, 따라 내리기 ---------------------------------------------------------

  var clock = { now: NaN, at: 0, speed: 0 };
  var shownCount = new WeakMap();
  var revealing = false;

  // 서버가 그린 순간의 피드 시각을 받아, 다음 갱신까지는 받아 온 간격으로 어림한 속도로 시계를 앞으로 민다
  function clockFrom(root) {
    var c = root.querySelector("[data-now]");
    if (!c) { clock.now = NaN; return; }
    var now = parseFloat(c.getAttribute("data-now"));
    var at = performance.now();
    var running = c.getAttribute("data-running") === "true";
    if (!isNaN(clock.now) && now >= clock.now && at > clock.at && running) {
      var v = (now - clock.now) / (at - clock.at);
      clock.speed = Math.max(0, Math.min(2, clock.speed * 0.3 + v * 0.7));
    } else {
      clock.speed = 0;
    }
    clock.now = now;
    clock.at = at;
  }

  function feedNow() {
    return clock.now + clock.speed * (performance.now() - clock.at);
  }

  function shownOf(el, t) {
    var from = parseFloat(el.getAttribute("data-reveal-from"));
    var to = parseFloat(el.getAttribute("data-reveal-to"));
    var n = el.children.length;
    if (t <= from) return 0;
    if (t >= to || to <= from) return n;
    return Math.round(n * (t - from) / (to - from));
  }

  function tick() {
    var els = document.querySelectorAll(".reveal[data-reveal-from]");
    if (!els.length || isNaN(clock.now)) { revealing = false; return; }
    var t = feedNow();
    var grew = false;
    els.forEach(function (el) {
      var k = shownOf(el, t);
      var prev = shownCount.has(el) ? shownCount.get(el) : el.querySelectorAll(".rv.on").length;
      if (k === prev) return;
      var chars = el.children;
      for (var i = Math.min(prev, k); i < Math.max(prev, k); i++) chars[i].classList.toggle("on", i < k);
      shownCount.set(el, k);
      if (k > prev) grew = true;
    });
    if (grew) document.querySelectorAll("[data-follow]").forEach(function (area) { keepBottom(area, false); });
    window.requestAnimationFrame(tick);
  }

  function startReveal() {
    if (revealing || isNaN(clock.now) || !document.querySelector(".reveal[data-reveal-from]")) return;
    revealing = true;
    window.requestAnimationFrame(tick);
  }

  var follow = new WeakMap();

  function nearBottom(area) {
    return area.scrollHeight - area.scrollTop - area.clientHeight <= Math.max(1, area.clientHeight * 0.05);
  }

  function followState(area) {
    var st = follow.get(area);
    if (st) return st;
    st = { pinned: true, autoUntil: 0 };
    follow.set(area, st);
    var stop = function () { st.autoUntil = 0; };
    area.addEventListener("wheel", stop, { passive: true });
    area.addEventListener("touchstart", stop, { passive: true });
    area.addEventListener("keydown", stop);
    area.addEventListener("scroll", function () {
      if (performance.now() < st.autoUntil) return;  // 따라 내리는 중의 스크롤은 사용자가 움직인 것이 아니다
      st.pinned = nearBottom(area);
    }, { passive: true });
    return st;
  }

  // 맨 아래를 보고 있던 영역만 맨 아래로 내린다. smooth면 새 줄이 생긴 것이라 부드럽게 내린다
  function keepBottom(area, smooth) {
    var st = followState(area);
    if (!st.pinned || area.scrollHeight <= area.clientHeight) return;
    if (smooth && !reduced()) {
      st.autoUntil = performance.now() + duration("--motion-slow") + duration("--motion-base");
      area.scrollTo({ top: area.scrollHeight, behavior: "smooth" });
    } else if (performance.now() >= st.autoUntil) {
      area.scrollTop = area.scrollHeight;
    }
  }

  function live(el) {
    var url = el.getAttribute("data-live");
    var every = parseInt(el.getAttribute("data-interval") || "", 10);
    if (!url || !every) return;
    var busy = false;
    clockFrom(el);
    el.querySelectorAll("[data-follow]").forEach(function (area) {
      followState(area);
      area.scrollTop = area.scrollHeight;
    });
    startReveal();
    setInterval(function () {
      if (busy || document.hidden) return;
      busy = true;
      fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (text) {
          if (text === null) return;
          var doc = new DOMParser().parseFromString(text, "text/html");
          el.querySelectorAll("[data-follow]").forEach(function (area) {
            var st = followState(area);
            if (performance.now() >= st.autoUntil) st.pinned = nearBottom(area);
          });
          var entered = [];
          el.querySelectorAll("[data-slot]").forEach(function (target) {
            var src = doc.querySelector('[data-slot="' + target.getAttribute("data-slot") + '"]');
            if (src) morphChildren(target, src, entered);
          });
          enter(entered);
          var at = doc.querySelector("[data-audio-at]");
          var mine = document.querySelector("[data-audio-at]");
          if (at && mine) mine.setAttribute("data-audio-at", at.getAttribute("data-audio-at"));
          clockFrom(el);
          var rows = entered.some(function (n) { return n.hasAttribute && n.hasAttribute("data-key"); });
          el.querySelectorAll("[data-follow]").forEach(function (area) { keepBottom(area, rows); });
          startReveal();
        })
        .catch(function () {})
        .then(function () { busy = false; });
    }, every);
  }

  // --- 녹음 재생 ---------------------------------------------------------------------------------------

  var players = new WeakMap();

  function syncPlayer(box) {
    var audio = box.querySelector("audio");
    if (!audio || players.has(box)) return;
    var toggle = document.querySelector("[data-stop-toggle]");
    var start = parseFloat(box.getAttribute("data-start") || "");
    var last = isNaN(start) ? 0 : start;
    var current = null;

    function stops() {
      return (box.getAttribute("data-stops") || "").split(" ").filter(Boolean).map(parseFloat);
    }
    function cues() {
      var targetId = box.getAttribute("data-target");
      var scope = (targetId && document.getElementById(targetId)) || document;
      return Array.prototype.slice.call(scope.querySelectorAll("[data-cue-start]"));
    }

    audio.addEventListener("loadedmetadata", function () {
      if (!isNaN(start)) audio.currentTime = start;
    });
    audio.addEventListener("timeupdate", function () {
      var t = audio.currentTime;
      if (toggle && toggle.checked && !audio.paused) {
        var list = stops();
        for (var i = 0; i < list.length; i++) {
          if (list[i] > last && list[i] <= t) {
            audio.pause();
            audio.currentTime = list[i];
            t = list[i];
            break;
          }
        }
      }
      last = t;
      var on = null;
      cues().forEach(function (el) {
        var s = parseFloat(el.getAttribute("data-cue-start"));
        var e = parseFloat(el.getAttribute("data-cue-end"));
        var hit = t >= s && t < e;
        el.classList.toggle("is-playing", hit);
        if (hit) on = el;
      });
      if (on && on !== current) {
        current = on;
        // 재생 중일 때만 따라 내린다(처음 위치를 맞출 때 스크롤하면 Tab 시작점이 그 줄로 옮겨진다)
        if (!audio.paused) on.scrollIntoView({ block: "nearest", behavior: reduced() ? "auto" : "smooth" });
      }
      var cursor = document.querySelector("[data-cursor]");
      if (cursor && audio.duration) cursor.style.setProperty("--at", (t / audio.duration) * 100 + "%");
    });
    // 영역을 바꾼 뒤(다른 판단으로 이동) 멈춰 있으면 새 판단 시각에서 다시 듣게 한다
    players.set(box, function () {
      start = parseFloat(box.getAttribute("data-start") || "");
      if (isNaN(start)) return;
      last = start;
      if (audio.paused && audio.readyState > 0) audio.currentTime = start;
    });
  }

  // --- 같은 화면의 다른 상태로 가기(관리 콘솔 리플레이) ------------------------------------------------------

  function rowBox(row, sel) {
    var el = row.querySelector(sel);
    return el ? el.getBoundingClientRect().width : null;
  }

  function measure(root) {
    var m = {};
    root.querySelectorAll(".rank-row[data-key]").forEach(function (row) {
      m[row.getAttribute("data-key")] = { top: row.getBoundingClientRect().top, fill: rowBox(row, ".rank-fill"),
                                          prev: rowBox(row, ".rank-prev") };
    });
    return m;
  }

  function grow(el, from, ms, ease) {
    if (!el || from === null) return;
    var to = el.getBoundingClientRect().width;
    if (Math.abs(to - from) < 0.5) return;
    el.animate([{ width: from + "px" }, { width: to + "px" }], { duration: ms, easing: ease });
  }

  // 순위 행은 이전 자리에서 새 자리로 미끄러지고(FLIP), 막대는 이전 길이에서 새 길이로 바뀐다
  function animateRanks(root, before) {
    if (reduced() || !Element.prototype.animate) return;
    var ms = duration("--motion-slow");
    var ease = token("--ease-standard") || "ease";
    root.querySelectorAll(".rank-row[data-key]").forEach(function (row) {
      var b = before[row.getAttribute("data-key")];
      if (!b) {
        grow(row.querySelector(".rank-fill"), 0, ms, ease);
        return;
      }
      var dy = b.top - row.getBoundingClientRect().top;
      if (Math.abs(dy) > 0.5) row.animate([{ transform: "translateY(" + dy + "px)" }, { transform: "none" }], { duration: ms, easing: ease });
      grow(row.querySelector(".rank-fill"), b.fill, ms, ease);
      grow(row.querySelector(".rank-prev"), b.prev, ms, ease);
    });
  }

  function focusedKey(root) {
    var a = document.activeElement;
    if (!a || !root.contains(a)) return null;
    var k = a.closest("[data-key]");
    return k ? k.getAttribute("data-key") : null;
  }

  function restoreFocus(root, key) {
    var a = document.activeElement;
    if (!key || (a && a !== document.body && root.contains(a))) return;
    var el = root.querySelector('[data-key="' + CSS.escape(key) + '"]');
    if (el && !el.disabled) { el.focus(); return; }
    var alt = root.querySelector('a[data-key="prev"], a[data-key="next"]');
    if (alt) alt.focus();
  }

  function scrollCurrent(box, smooth) {
    var cur = box.querySelector("[data-current-col]");
    if (!cur) return;
    var left = Math.max(0, cur.offsetLeft - box.clientWidth / 2);
    if (smooth && !reduced()) box.scrollTo({ left: left, behavior: "smooth" });
    else box.scrollLeft = left;
  }

  var swapping = null;

  function swapTo(root, href, push) {
    var mine = {};
    swapping = mine;
    fetch(href, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (text) {
        if (swapping !== mine) return;  // 더 나중에 누른 이동이 있다
        var doc = text === null ? null : new DOMParser().parseFromString(text, "text/html");
        var src = doc && doc.querySelector("[data-swap]");
        if (!src) { window.location.href = href; return; }
        var key = focusedKey(root);
        var before = measure(root);
        var entered = [];
        morphChildren(root, src, entered);
        if (push) window.history.pushState(null, "", href);
        if (doc.title) document.title = doc.title;
        animateRanks(root, before);
        enter(entered);
        restoreFocus(root, key);
        root.querySelectorAll("[data-player]").forEach(function (box) {
          var reset = players.get(box);
          if (reset) reset(); else syncPlayer(box);
        });
        root.querySelectorAll("[data-scroll-current]").forEach(function (box) { scrollCurrent(box, true); });
      })
      .catch(function () { window.location.href = href; });
  }

  function swapArea(root) {
    root.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var a = e.target.closest ? e.target.closest("a[href]") : null;
      if (!a || !root.contains(a) || a.target) return;
      var url = new URL(a.getAttribute("href"), window.location.href);
      if (url.origin !== window.location.origin || url.pathname !== window.location.pathname) return;
      e.preventDefault();
      swapTo(root, url.href, true);
    });
    window.addEventListener("popstate", function () { swapTo(root, window.location.href, false); });
  }

  // --- 그 밖의 동작 ------------------------------------------------------------------------------------

  function focusDrawer() {
    var el = document.querySelector("[data-autofocus]");
    if (el) el.focus();
  }

  function drawerEscape() {
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      var d = document.querySelector("[data-drawer]");
      if (d) window.location.href = d.getAttribute("data-drawer");
    });
  }

  function listen(btn) {
    var audio = document.getElementById(btn.getAttribute("data-listen"));
    if (!audio) return;
    btn.addEventListener("click", function () {
      if (!audio.paused) {
        audio.pause();
        btn.setAttribute("aria-pressed", "false");
        return;
      }
      var at = document.querySelector("[data-audio-at]");
      var t = at ? parseFloat(at.getAttribute("data-audio-at")) : NaN;
      if (!isNaN(t)) audio.currentTime = t;
      audio.play();
      btn.setAttribute("aria-pressed", "true");
    });
    audio.addEventListener("pause", function () { btn.setAttribute("aria-pressed", "false"); });
  }

  function playButton(btn) {
    var box = btn.closest("[data-player]");
    var audio = box && box.querySelector("audio");
    if (!audio) return;
    btn.addEventListener("click", function () {
      if (audio.paused) audio.play(); else audio.pause();
    });
    audio.addEventListener("play", function () { btn.setAttribute("aria-pressed", "true"); });
    audio.addEventListener("pause", function () { btn.setAttribute("aria-pressed", "false"); });
  }

  function showFor(form) {
    var pick = form.querySelector("select[name=adapter]");
    if (!pick) return;
    function apply() {
      form.querySelectorAll("[data-show-for]").forEach(function (el) {
        el.hidden = el.getAttribute("data-show-for").split(" ").indexOf(pick.value) < 0;
      });
    }
    pick.addEventListener("change", apply);
    apply();
  }

  onReady(function () {
    focusDrawer();
    drawerEscape();
    document.querySelectorAll("[data-player]").forEach(syncPlayer);
    document.querySelectorAll("[data-live]").forEach(live);
    document.querySelectorAll("[data-swap]").forEach(swapArea);
    document.querySelectorAll("[data-listen]").forEach(listen);
    document.querySelectorAll("[data-play]").forEach(playButton);
    document.querySelectorAll("form").forEach(showFor);
    document.querySelectorAll("[data-scroll-current]").forEach(function (box) { scrollCurrent(box, false); });
  });
})();
