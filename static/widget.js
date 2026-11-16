/* Website chat bubble. Paste before </body> on any site:
   <script src="https://YOUR-SERVER/widget.js" data-title="Smile Clinic" data-color="#0a7c66" defer></script>
   Optional: data-welcome="Hi! How can we help?"  data-position="left"                                      */
(function () {
  var s = document.currentScript, base = new URL(s.src).origin;
  var color = s.dataset.color || "#e11d2e", left = s.dataset.position === "left";
  var q = new URLSearchParams();
  ["title", "welcome"].forEach(function (k) { if (s.dataset[k]) q.set(k, s.dataset[k]); });
  q.set("color", color.replace("#", ""));
  var side = left ? "left:20px;" : "right:20px;";
  var frame = document.createElement("iframe");
  frame.src = base + "/widget?" + q.toString();
  frame.title = "Chat";
  frame.style.cssText = "position:fixed;bottom:90px;" + side + "width:370px;max-width:calc(100vw - 40px);height:560px;max-height:calc(100vh - 120px);border:0;border-radius:16px;box-shadow:0 12px 48px rgba(0,0,0,.22);z-index:2147483646;display:none;background:#fff";
  var btn = document.createElement("button");
  btn.setAttribute("aria-label", "Open chat");
  btn.innerHTML = "&#128172;";
  btn.style.cssText = "position:fixed;bottom:20px;" + side + "width:58px;height:58px;border-radius:50%;border:0;background:" + color + ";color:#fff;font-size:26px;cursor:pointer;box-shadow:0 6px 24px rgba(0,0,0,.25);z-index:2147483647";
  btn.onclick = function () {
    var open = frame.style.display === "none";
    frame.style.display = open ? "block" : "none";
    btn.innerHTML = open ? "&#10005;" : "&#128172;";
  };
  document.body.appendChild(frame);
  document.body.appendChild(btn);
})();
