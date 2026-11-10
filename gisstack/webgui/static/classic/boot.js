(function () {   // apply the last used theme before the first paint (the server setting is applied right after load)
  try {
    var t = localStorage.getItem("gs_theme") || "auto";
    if (t === "auto") t = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", t);
  } catch (e) {}
})();
