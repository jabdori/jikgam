// 화면 동작. 서버가 만든 HTML의 data-* 속성을 보고 동작만 붙인다. 글자는 만들지 않는다(ADR 0022).
//   data-autofocus        열린 드로어의 제목으로 포커스를 옮긴다
//   data-drawer="주소"     Escape를 누르면 그 주소로 가서 드로어를 닫는다
//   data-player           녹음 재생 위치에 맞춰 data-cue-start/end 요소에 is-playing을 붙인다.
//                         data-start(초)에서 시작, data-stops(초 목록)와 data-stop-toggle 상자로 판단 시점마다 멈춘다
//   data-live="주소"       data-interval(밀리초)마다 그 주소의 문서를 다시 받아 안의 data-slot 영역을 바꾼다
//   data-listen="audio id" 누르면 그 녹음을 data-audio-at 요소가 가리키는 지금 위치부터 재생하거나 멈춘다
//   data-cursor           재생 위치를 전체 길이 대비 비율(--at)로 옮긴다
(function () {
  "use strict";

  function onReady(fn) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fn);
    else fn();
  }

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

  function syncPlayer(box) {
    var audio = box.querySelector("audio");
    if (!audio) return;
    var targetId = box.getAttribute("data-target");
    var scope = (targetId && document.getElementById(targetId)) || document;
    var cues = Array.prototype.slice.call(scope.querySelectorAll("[data-cue-start]"));
    var stops = (box.getAttribute("data-stops") || "").split(" ").filter(Boolean).map(parseFloat);
    var toggle = document.querySelector("[data-stop-toggle]");
    var cursor = document.querySelector("[data-cursor]");
    var start = parseFloat(box.getAttribute("data-start") || "");
    var last = isNaN(start) ? 0 : start;
    var current = null;

    audio.addEventListener("loadedmetadata", function () {
      if (!isNaN(start)) audio.currentTime = start;
    });
    audio.addEventListener("timeupdate", function () {
      var t = audio.currentTime;
      if (toggle && toggle.checked && !audio.paused) {
        for (var i = 0; i < stops.length; i++) {
          if (stops[i] > last && stops[i] <= t) {
            audio.pause();
            audio.currentTime = stops[i];
            t = stops[i];
            break;
          }
        }
      }
      last = t;
      var on = null;
      cues.forEach(function (el) {
        var s = parseFloat(el.getAttribute("data-cue-start"));
        var e = parseFloat(el.getAttribute("data-cue-end"));
        var hit = t >= s && t < e;
        el.classList.toggle("is-playing", hit);
        if (hit) on = el;
      });
      if (on && on !== current) {
        current = on;
        // 재생 중일 때만 따라 내린다(처음 위치를 맞출 때 스크롤하면 Tab 시작점이 그 줄로 옮겨진다)
        if (!audio.paused) on.scrollIntoView({ block: "nearest" });
      }
      if (cursor && audio.duration) cursor.style.setProperty("--at", (t / audio.duration) * 100 + "%");
    });
  }

  function live(el) {
    var url = el.getAttribute("data-live");
    var every = parseInt(el.getAttribute("data-interval") || "", 10);
    if (!url || !every) return;
    var busy = false;
    setInterval(function () {
      if (busy || document.hidden) return;
      busy = true;
      fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (text) {
          if (text === null) return;
          var doc = new DOMParser().parseFromString(text, "text/html");
          el.querySelectorAll("[data-slot]").forEach(function (target) {
            var src = doc.querySelector('[data-slot="' + target.getAttribute("data-slot") + '"]');
            if (!src) return;
            var html = src.innerHTML;
            if (target.innerHTML !== html) target.innerHTML = html;
          });
          var at = doc.querySelector("[data-audio-at]");
          var mine = document.querySelector("[data-audio-at]");
          if (at && mine) mine.setAttribute("data-audio-at", at.getAttribute("data-audio-at"));
        })
        .catch(function () {})
        .then(function () { busy = false; });
    }, every);
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

  function scrollCurrent(box) {
    var cur = box.querySelector("[data-current-col]");
    if (cur) box.scrollLeft = Math.max(0, cur.offsetLeft - box.clientWidth / 2);
  }

  onReady(function () {
    focusDrawer();
    drawerEscape();
    document.querySelectorAll("[data-player]").forEach(syncPlayer);
    document.querySelectorAll("[data-live]").forEach(live);
    document.querySelectorAll("[data-listen]").forEach(listen);
    document.querySelectorAll("[data-play]").forEach(playButton);
    document.querySelectorAll("form").forEach(showFor);
    document.querySelectorAll("[data-scroll-current]").forEach(scrollCurrent);
  });
})();
