// 화면 동작. 서버가 만든 HTML의 data-* 속성을 보고 동작만 붙인다. 글자는 만들지 않는다(ADR 0022).
//   data-autofocus        열린 드로어의 제목으로 포커스를 옮긴다
//   data-drawer="주소"     Escape를 누르면 그 주소로 가서 드로어를 닫는다
//   data-player           녹음 재생 위치에 맞춰 data-cue-start/end 요소에 is-playing을 붙인다.
//                         data-start(초)에서 시작, data-stops(초 목록)와 data-stop-toggle 상자로 판단 시점마다 멈춘다
//   data-live="주소"       data-interval(밀리초)마다 그 주소의 문서를 다시 받아, 안의 data-slot 영역에서 바뀐 부분만 맞춘다.
//                         이미 있는 요소는 그대로 두므로 색이 바뀌는 상태는 CSS 전환으로 부드럽게 바뀐다. 받아 온 문서에
//                         갱신 영역이 없으면(처리가 끝남) 화면 전체를 한 번 맞추고 멈춘다
//   data-key              갱신·영역 교체 때 같은 이름의 요소를 같은 것으로 본다. 새로 붙은 요소에는 c-enter를 잠깐 붙인다
//   data-now              실시간 대화의 시계(재생 피드 시각, 밀리초). data-running이 false면 멈춘 통화다
//   data-reveal-from/to   그 시각 사이에 .reveal 안의 글자(.rv)를 차례로 드러낸다(말하는 중인 AI 답)
//   data-follow           따로 스크롤되는 영역. 맨 아래를 보고 있으면 새 줄과 글자를 따라 내린다
//   data-swap             셸. 같은 주소의 다른 상태로 가는 링크(필터, 기간, 문서·구간, 상세 서랍, 이전·다음)와 같은 주소로
//                         보내는 GET 폼은 문서를 새로 열지 않고 이 영역에서 바뀐 부분만 맞춘다. data-key가 같은 순위 행·표 행·
//                         목록 항목은 새 자리로 옮겨 가고 막대는 새 길이로 바뀐다. 다른 화면으로 가는 링크는 CSS 화면 전환으로 연다
//   data-listen="audio id" 누르면 그 녹음을 data-audio-at 요소가 가리키는 지금 위치부터 재생하거나 멈춘다
//   data-cursor           재생 위치를 전체 길이 대비 비율(--at)로 옮긴다. 녹음을 불러온 뒤에는 영역을 바꿔도 서버 값으로
//                         되돌리지 않는다
//   data-scrub            재생 띠. 누르거나 끌면 그 위치로 옮긴다. 위치 선은 키보드 슬라이더다(←/→ data-step 초,
//                         PageUp/PageDown 판단 하나, Home/End). 끄는 동안 화면은 움직임 없이 바로 바뀐다
//   data-decisions        재생기의 판단 시각 목록. 재생하거나 위치를 옮기면 그 시각 직전의 판단 화면(data-decision-href 뒤에
//                         번호)으로 영역을 바꾸고 주소만 갈아 끼운다(뒤로 가기 기록을 쌓지 않는다)
//   data-seek             재생 목록의 발화 줄. 누르면 녹음의 그 위치(초)부터 재생한다
//   data-voice-test       관리 콘솔 음성 시험. 시작하면 에코 제거를 켠 마이크로 WebRTC 연결을 열고(data-offer), AI 음성을
//                         숨은 audio로 재생한다. 종료(data-stop)나 연결이 끊기면 정리하고 화면을 다시 맞춘다
// 움직임의 시간과 곡선은 tokens.css의 --motion-*, --ease-*에서 읽는다. 움직임 줄이기 설정이면 움직이지 않는다.
(function () {
  "use strict";

  var OWN = "c-";  // 스크립트가 붙이는 클래스의 접두어. 갱신 때 서버 내용으로 지우지 않는다
  var keepOpen = false;  // 실시간 갱신 중에는 사용자가 펼치거나 접은 펼침 영역(details)의 상태를 그대로 둔다
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

  var cursorLive = false;  // 녹음을 불러온 뒤에는 재생 위치 선의 위치와 값을 재생기가 맡는다

  function userOwned(el, name) {
    if (cursorLive && (name === "style" || name === "aria-valuenow") && el.hasAttribute("data-cursor")) return true;
    // 재생·듣기 버튼의 눌림 상태는 소리가 정한다(재생 중에 영역을 바꿔도 서버가 그린 '멈춤' 상태로 되돌리지 않는다)
    if (name === "aria-pressed" && (el.hasAttribute("data-play") || el.hasAttribute("data-listen"))) return true;
    return keepOpen && name === "open" && el.nodeName === "DETAILS";
  }

  function syncAttrs(from, to) {
    var i, a;
    for (i = from.attributes.length - 1; i >= 0; i--) {
      a = from.attributes[i];
      if (a.name !== "class" && !userOwned(from, a.name) && !to.hasAttribute(a.name)) from.removeAttribute(a.name);
    }
    for (i = 0; i < to.attributes.length; i++) {
      a = to.attributes[i];
      if (a.name !== "class" && !userOwned(from, a.name) && from.getAttribute(a.name) !== a.value) from.setAttribute(a.name, a.value);
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

  // 새로 붙은 요소 중 이름 있는 것(대화 줄, 구분선, 순위·표 행, 목록 항목, 근거)과 카드·알림·펼침에 나타나는 움직임을 한 번 준다
  var ENTER = ".card, .notice, .disclosure, .steps";

  function enter(nodes) {
    if (reduced()) return;
    var keep = duration("--motion-emphasis") + duration("--motion-base");
    nodes.forEach(function (el) {
      if (!el.hasAttribute || !(el.hasAttribute("data-key") || el.matches(ENTER))) return;
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

  function firstKey(area) {
    var row = area.querySelector("[data-key]");
    return row ? row.getAttribute("data-key") : null;
  }

  // 영역 안의 대화가 다른 통화로 바뀌었으면(첫 줄 이름이 다르다) 다시 맨 아래를 보는 상태에서 시작한다
  function resetIfOther(area) {
    var st = followState(area);
    var first = firstKey(area);
    if (st.first !== undefined && st.first !== first) {
      st.pinned = true;
      st.autoUntil = 0;
      area.scrollTop = area.scrollHeight;
    }
    st.first = first;
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
    var every = parseInt(el.getAttribute("data-interval") || "", 10);
    if (!el.getAttribute("data-live") || !every) return;
    var busy = false;
    var timer = null;
    clockFrom(el);
    el.querySelectorAll("[data-follow]").forEach(function (area) {
      resetIfOther(area);
      area.scrollTop = area.scrollHeight;
    });
    startReveal();
    timer = setInterval(function () {
      var src = el.getAttribute("data-live");
      // 영역을 바꾸며 이 갱신 영역이 사라졌거나, 같은 자리에 맞춰진 새 화면이 더 갱신하지 않는다(통화가 끝났다)
      if (!document.contains(el) || !src) { clearInterval(timer); return; }
      if (busy || document.hidden) return;
      busy = true;
      fetch(src, { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (text) {
          if (text === null || !document.contains(el)) return;
          var doc = new DOMParser().parseFromString(text, "text/html");
          if (!doc.querySelector("[data-live]")) {  // 처리가 끝나 더 받아 올 것이 없다
            clearInterval(timer);
            swapTo(window.location.href, false);
            return;
          }
          el.querySelectorAll("[data-follow]").forEach(function (area) {
            var st = followState(area);
            if (performance.now() >= st.autoUntil) st.pinned = nearBottom(area);
          });
          var entered = [];
          keepOpen = true;
          try {
            el.querySelectorAll("[data-slot]").forEach(function (target) {
              var src = doc.querySelector('[data-slot="' + target.getAttribute("data-slot") + '"]');
              if (src) morphChildren(target, src, entered);
            });
          } finally {
            keepOpen = false;
          }
          enter(entered);
          var at = doc.querySelector("[data-audio-at]");
          var mine = document.querySelector("[data-audio-at]");
          if (at && mine) mine.setAttribute("data-audio-at", at.getAttribute("data-audio-at"));
          clockFrom(el);
          var rows = entered.some(function (n) { return n.hasAttribute && n.hasAttribute("data-key"); });
          el.querySelectorAll("[data-follow]").forEach(function (area) { resetIfOther(area); keepBottom(area, rows); });
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
    var pending = null;  // 재생 위치를 따라 바꾸는 중인 판단 번호

    function stops() {
      return (box.getAttribute("data-stops") || "").split(" ").filter(Boolean).map(parseFloat);
    }
    function cues() {
      var targetId = box.getAttribute("data-target");
      var scope = (targetId && document.getElementById(targetId)) || document;
      return Array.prototype.slice.call(scope.querySelectorAll("[data-cue-start]"));
    }

    function total() {
      var lanes = document.querySelector("[data-scrub]");
      return audio.duration || parseFloat((lanes && lanes.getAttribute("data-duration")) || "");
    }
    function place(t) {
      var cursor = document.querySelector("[data-cursor]");
      var d = total();
      if (!cursor || !d) return;
      cursor.style.setProperty("--at", Math.min(100, (t / d) * 100) + "%");
      if (cursor.hasAttribute("aria-valuenow")) cursor.setAttribute("aria-valuenow", t.toFixed(1));
    }
    function decisionAt(t) {
      var idx = 1;
      (box.getAttribute("data-decisions") || "").split(" ").filter(Boolean).forEach(function (x, i) {
        if (parseFloat(x) <= t + 0.001) idx = i + 1;
      });
      return idx;  // 첫 판단 전이면 첫 판단을 보인다
    }
    function follow(t, still) {
      var base = box.getAttribute("data-decision-href");
      if (!base) return;
      var idx = decisionAt(t);
      if (idx === parseInt(box.getAttribute("data-decision") || "", 10) || idx === pending) return;
      pending = idx;
      swapTo(base + idx, "replace", still);
    }
    function scrubbing() {
      return !!document.querySelector("[data-scrub].c-scrubbing");
    }

    audio.addEventListener("loadedmetadata", function () {
      cursorLive = true;
      if (!isNaN(start)) audio.currentTime = start;
      place(audio.currentTime);
    });
    // 위치를 옮기면(재생 띠, 재생 목록, 판단 시점 멈춤) 선을 옮기고 그 시각의 판단 화면으로 바꾼다
    audio.addEventListener("seeked", function () {
      place(audio.currentTime);
      follow(audio.currentTime, scrubbing());
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
      if (!scrubbing()) place(t);
      if (!audio.paused && !scrubbing()) follow(t, false);
    });
    // 영역을 바꾼 뒤(다른 판단으로 이동) 새 판단 시각으로 옮긴다. 재생 중이면 그 시각부터 이어서 재생한다(옛 위치에서 계속
    // 재생되면 재생 줄을 따라 내리는 스크롤이 화면을 옛 위치로 되돌린다). 같은 판단이면 위치를 그대로 둔다
    // 재생 위치를 따라 바꾼 화면이면 소리와 선은 그대로 둔다
    players.set(box, function () {
      var next = parseFloat(box.getAttribute("data-start") || "");
      if (isNaN(next)) return;
      if (pending !== null && parseInt(box.getAttribute("data-decision") || "", 10) === pending) {
        pending = null;
        start = next;
        return;
      }
      pending = null;
      if (next === start) return;
      start = next;
      last = start;
      current = null;
      if (audio.readyState > 0) audio.currentTime = start;
    });
  }

  // --- 같은 주소의 다른 상태로 가기(모든 화면의 셸) ----------------------------------------------------------

  var MOVERS = ".rank-row[data-key], tbody tr[data-key], .item-list li[data-key]";

  function rowBox(row, sel) {
    var el = row.querySelector(sel);
    return el ? el.getBoundingClientRect().width : null;
  }

  function measure(root) {
    var m = {};
    root.querySelectorAll(MOVERS).forEach(function (row) {
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

  // 남아 있는 행은 이전 자리에서 새 자리로 미끄러지고(FLIP), 순위 막대는 이전 길이에서 새 길이로 바뀐다.
  // 새로 생긴 행의 막대는 CSS가 0에서 자라게 한다
  function flash(row) {
    row.classList.remove("c-moved");
    void row.offsetWidth;  // 같은 행이 다시 옮겨 가도 강조를 처음부터 다시 보인다
    row.classList.add("c-moved");
    row.addEventListener("animationend", function done() {
      row.classList.remove("c-moved");
      row.removeEventListener("animationend", done);
    });
  }

  function animateMoves(root, before) {
    if (reduced() || !Element.prototype.animate) return;
    var slow = duration("--motion-slow");
    var standard = token("--ease-standard") || "ease";
    var rankMs = duration("--motion-rank");
    var move = token("--ease-move") || "ease-in-out";
    root.querySelectorAll(MOVERS).forEach(function (row) {
      var b = before[row.getAttribute("data-key")];
      if (!b) return;
      // 조항 순위는 위아래로 옮겨 가는 것이 보이도록 더 길고 고르게 움직이고, 자리가 바뀐 행을 잠깐 강조한다
      var rank = row.classList.contains("rank-row");
      var ms = rank ? rankMs : slow;
      var ease = rank ? move : standard;
      var dy = b.top - row.getBoundingClientRect().top;
      if (Math.abs(dy) > 0.5) {
        row.animate([{ transform: "translateY(" + dy + "px)" }, { transform: "none" }], { duration: ms, easing: ease });
        if (rank) flash(row);
      }
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

  function restoreFocus(root, key, entered) {
    var auto = null;
    entered.some(function (n) {
      if (!n.matches) return false;
      auto = n.matches("[data-autofocus]") ? n : n.querySelector("[data-autofocus]");
      return !!auto;
    });
    if (auto) { auto.focus(); return; }  // 새로 열린 상세 서랍의 제목
    var a = document.activeElement;
    if (a && a !== document.body && root.contains(a)) return;
    var el = key ? root.querySelector('[data-key="' + CSS.escape(key) + '"]') : null;
    var target = el && (el.matches("a, button") ? el : el.querySelector("a, button"));
    if (target && !target.disabled) { target.focus(); return; }
    if (key) {
      var alt = root.querySelector('a[data-key="prev"], a[data-key="next"]');
      if (alt) { alt.focus(); return; }
    }
    var main = document.getElementById("main");
    if (main) main.focus({ preventScroll: true });
  }

  function scrollCurrent(box, smooth) {
    var cur = box.querySelector("[data-current-col]");
    if (!cur) return;
    var left = Math.max(0, cur.offsetLeft - box.clientWidth / 2);
    if (smooth && !reduced()) box.scrollTo({ left: left, behavior: "smooth" });
    else box.scrollLeft = left;
  }

  var swapping = null;

  // href의 문서를 받아 셸에서 바뀐 부분만 맞춘다. push면 주소를 기록에 더한다(뒤로 가기로 돌아올 수 있다)
  // push: true면 뒤로 가기 기록을 쌓고, "replace"면 주소만 갈아 끼운다. still이면 움직임 없이 바로 바꾼다(재생 띠를 끄는 중)
  function swapTo(href, push, still) {
    var root = document.querySelector("[data-swap]");
    if (!root) { window.location.href = href; return; }
    var mine = {};
    swapping = mine;
    root.setAttribute("aria-busy", "true");
    fetch(href, { credentials: "same-origin" })
      .then(function (r) { return r.ok && !r.redirected ? r.text() : null; })
      .then(function (text) {
        if (swapping !== mine) return;  // 더 나중에 누른 이동이 있다
        var doc = text === null ? null : new DOMParser().parseFromString(text, "text/html");
        var src = doc && doc.querySelector("[data-swap]");
        if (!src) { window.location.href = href; return; }
        var key = focusedKey(root);
        var before = measure(root);
        var entered = [];
        morphChildren(root, src, entered);
        if (push === "replace") window.history.replaceState(null, "", href);
        else if (push) window.history.pushState(null, "", href);
        if (doc.title) document.title = doc.title;
        if (!still) {
          animateMoves(root, before);
          enter(entered);
        }
        init(root);
        restoreFocus(root, key, entered);
        root.querySelectorAll("[data-player]").forEach(function (box) {
          var reset = players.get(box);
          if (reset) reset();
        });
        root.querySelectorAll("[data-scroll-current]").forEach(function (box) { scrollCurrent(box, true); });
        root.querySelectorAll("[data-live]").forEach(function (el) {
          clockFrom(el);
          el.querySelectorAll("[data-follow]").forEach(resetIfOther);
        });
        startReveal();
      })
      .catch(function () { window.location.href = href; })
      .then(function () { if (swapping === mine) root.removeAttribute("aria-busy"); });
  }

  function sameScreen(url) {
    return url.origin === window.location.origin && url.pathname === window.location.pathname;
  }

  function swapLinks() {
    if (!document.querySelector("[data-swap]") || !window.fetch || !window.history.pushState) return;
    document.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var a = e.target.closest ? e.target.closest("a[href]") : null;
      if (!a || a.target || a.hasAttribute("download")) return;
      var raw = a.getAttribute("href");
      if (raw.charAt(0) === "#") return;  // 같은 문서 안의 자리(본문으로 건너뛰기 등)
      var url = new URL(raw, window.location.href);
      if (!sameScreen(url)) return;  // 다른 화면은 문서를 연다(CSS 화면 전환)
      e.preventDefault();
      swapTo(url.href, url.href !== window.location.href);
    });
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (e.defaultPrevented || (form.getAttribute("method") || "get").toLowerCase() !== "get") return;
      var url = new URL(form.getAttribute("action") || window.location.href, window.location.href);
      if (!sameScreen(url)) return;
      e.preventDefault();
      url.search = new URLSearchParams(new FormData(form, e.submitter || null)).toString();
      swapTo(url.href, true);
    });
    window.addEventListener("popstate", function () { swapTo(window.location.href, false); });
  }

  // --- 관리 콘솔 음성 시험: 브라우저 마이크 ↔ 음성 파이프라인(WebRTC) ---------------------------------------
  // 마이크는 에코 제거·잡음 억제·자동 음량을 켜서 연다. 스피커로 나온 AI 목소리가 마이크로 다시 들어가 끼어들기로 잡히지 않게 한다.
  // 알림 글은 서버가 넣어 두고, 여기서는 지금 상태의 알림만 보인다.

  function waitIce(pc) {
    if (pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise(function (resolve) {
      function done() {
        if (pc.iceGatheringState !== "complete") return;
        pc.removeEventListener("icegatheringstatechange", done);
        resolve();
      }
      pc.addEventListener("icegatheringstatechange", done);
      setTimeout(resolve, 2000);  // 이 PC 안의 연결이라 후보를 오래 기다리지 않는다
    });
  }

  function voiceTest(box) {
    var start = box.querySelector("[data-voice-start]");
    var stop = box.querySelector("[data-voice-stop]");
    var audio = box.querySelector("[data-voice-audio]");
    var pc = null;
    var stream = null;
    var call = null;
    if (!start || !stop || !audio) return;

    function show(name) {
      box.querySelectorAll("[data-voice-msg]").forEach(function (el) {
        el.hidden = el.getAttribute("data-voice-msg") !== name;
      });
    }
    function running(on) {
      start.disabled = on;
      stop.disabled = !on;
    }
    function cleanup() {
      if (pc) pc.close();
      pc = null;
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
      running(false);
    }
    function refresh() {
      swapTo(window.location.href, false);
    }

    start.addEventListener("click", function () {
      if (pc) return;
      running(true);
      show("connecting");
      var denied = {};
      navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
                                            video: false })
        .catch(function () { throw denied; })
        .then(function (s) {
          stream = s;
          pc = new RTCPeerConnection();
          s.getAudioTracks().forEach(function (t) { pc.addTrack(t, s); });
          pc.addEventListener("track", function (e) {
            audio.srcObject = e.streams[0] || new MediaStream([e.track]);
            audio.play().catch(function () {});
          });
          pc.addEventListener("connectionstatechange", function () {
            if (!pc || ["failed", "closed", "disconnected"].indexOf(pc.connectionState) < 0) return;
            cleanup();
            show("ended");
            refresh();
          });
          return pc.createOffer()
            .then(function (offer) { return pc.setLocalDescription(offer); })
            .then(function () { return waitIce(pc); });
        })
        .then(function () {
          return fetch(box.getAttribute("data-offer"), {
            method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type,
                                   customer: box.getAttribute("data-customer") })
          });
        })
        .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw j; return j; }); })
        .then(function (answer) {
          call = answer.call;
          return pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
        })
        .then(function () {
          show("");  // 이제부터는 서버가 그리는 상태 줄('연결 중' → '통화 중')이 안내한다
          var url = new URL(window.location.href);
          url.searchParams.set("call", call);
          swapTo(url.href, true);
        })
        .catch(function (e) {
          show(e === denied ? "denied" : "failed");
          cleanup();
        });
    });

    stop.addEventListener("click", function () {
      var id = call || box.getAttribute("data-call");
      stop.disabled = true;
      fetch(box.getAttribute("data-stop"), {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ call: id })
      }).catch(function () {}).then(function () {
        cleanup();
        show("ended");
        refresh();
      });
    });
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
      if (!d) return;
      var url = new URL(d.getAttribute("data-drawer"), window.location.href);
      if (document.querySelector("[data-swap]") && sameScreen(url)) swapTo(url.href, true);
      else window.location.href = url.href;
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
    ["play", "pause", "ended", "emptied"].forEach(function (name) {
      audio.addEventListener(name, function () { btn.setAttribute("aria-pressed", audio.paused ? "false" : "true"); });
    });
  }

  // 재생 띠: 누르거나 끌면 그 위치로 옮긴다(재생 중이었으면 놓은 뒤 이어서 재생). 위치 선은 키보드 슬라이더다
  function scrubber(lanes) {
    var dragging = false;
    var resume = false;
    function audio() {
      return document.querySelector("[data-player] audio");
    }
    function total(a) {
      return (a && a.duration) || parseFloat(lanes.getAttribute("data-duration") || "");
    }
    function seek(a, t) {
      var d = total(a);
      if (!a || !d) return;
      a.currentTime = Math.max(0, Math.min(d, t));
      var cursor = lanes.querySelector("[data-cursor]");
      if (cursor) cursor.style.setProperty("--at", (a.currentTime / d) * 100 + "%");
    }
    function at(x) {
      var track = lanes.querySelector(".lane-track");
      var a = audio();
      if (!track || !a) return;
      var r = track.getBoundingClientRect();
      seek(a, Math.max(0, Math.min(1, (x - r.left) / r.width)) * total(a));
    }
    lanes.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 || e.target.closest("a")) return;  // 판단 표시는 그 판단으로 가는 링크다
      var a = audio();
      if (!a) return;
      dragging = true;
      resume = !a.paused;
      a.pause();
      lanes.classList.add("c-scrubbing");
      lanes.setPointerCapture(e.pointerId);
      at(e.clientX);
      e.preventDefault();
    });
    lanes.addEventListener("pointermove", function (e) {
      if (dragging) at(e.clientX);
    });
    function end() {
      if (!dragging) return;
      dragging = false;
      lanes.classList.remove("c-scrubbing");
      var a = audio();
      if (a && resume) a.play();
    }
    lanes.addEventListener("pointerup", end);
    lanes.addEventListener("pointercancel", end);

    var cursor = lanes.querySelector("[data-cursor][role=slider]");
    if (!cursor) return;
    cursor.addEventListener("keydown", function (e) {
      var a = audio();
      if (!a) return;
      var step = parseFloat(lanes.getAttribute("data-step") || "") || 1;
      var box = a.closest("[data-player]");
      var times = ((box && box.getAttribute("data-decisions")) || "").split(" ").filter(Boolean).map(parseFloat);
      var t = a.currentTime;
      var next = null;
      if (e.key === "ArrowRight" || e.key === "ArrowUp") next = t + step;
      else if (e.key === "ArrowLeft" || e.key === "ArrowDown") next = t - step;
      else if (e.key === "PageUp") {
        // 지금 보이는 판단의 다음 판단(첫 판단 전이면 첫 판단을 보이고 있으므로 둘째 판단)
        var shown = times[parseInt((box && box.getAttribute("data-decision")) || "", 10) - 1];
        var from = Math.max(t, isNaN(shown) || shown === undefined ? t : shown);
        next = times.filter(function (x) { return x > from + 0.05; })[0];
      }
      else if (e.key === "PageDown") next = times.filter(function (x) { return x < t - 0.05; }).pop();
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = total(a);
      else return;
      e.preventDefault();
      if (next !== undefined && next !== null) seek(a, next);
    });
  }

  // 재생 목록의 발화 줄: 그 위치부터 재생한다(판단 줄은 그 판단으로 옮기는 링크다)
  function seekButton(btn) {
    btn.addEventListener("click", function () {
      var audio = document.querySelector("[data-player] audio");
      var t = parseFloat(btn.getAttribute("data-seek") || "");
      if (!audio || isNaN(t)) return;
      audio.currentTime = t;
      audio.play();
    });
  }

  function playButton(btn) {
    var box = btn.closest("[data-player]");
    var audio = box && box.querySelector("audio");
    if (!audio) return;
    function sync() {
      btn.setAttribute("aria-pressed", audio.paused ? "false" : "true");
    }
    btn.addEventListener("click", function () {
      if (audio.paused) audio.play(); else audio.pause();
      sync();
    });
    ["play", "pause", "ended", "emptied"].forEach(function (name) { audio.addEventListener(name, sync); });
    sync();
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

  var inited = new WeakSet();

  function once(el, fn) {
    if (inited.has(el)) return;
    inited.add(el);
    fn(el);
  }

  // 처음 연 문서와, 영역을 바꾼 뒤 새로 생긴 요소에 동작을 붙인다
  function init(scope) {
    scope.querySelectorAll("[data-player]").forEach(syncPlayer);
    scope.querySelectorAll("[data-live]").forEach(function (el) { once(el, live); });
    scope.querySelectorAll("[data-listen]").forEach(function (el) { once(el, listen); });
    scope.querySelectorAll("[data-play]").forEach(function (el) { once(el, playButton); });
    scope.querySelectorAll("form").forEach(function (el) { once(el, showFor); });
    scope.querySelectorAll("[data-voice-test]").forEach(function (el) { once(el, voiceTest); });
    scope.querySelectorAll("[data-seek]").forEach(function (el) { once(el, seekButton); });
    scope.querySelectorAll("[data-scrub]").forEach(function (el) { once(el, scrubber); });
  }

  onReady(function () {
    focusDrawer();
    drawerEscape();
    init(document);
    swapLinks();
    document.querySelectorAll("[data-scroll-current]").forEach(function (box) { scrollCurrent(box, false); });
  });
})();
