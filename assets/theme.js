// The only script on the site: the light/dark toggle. Without a stored
// choice the theme follows prefers-color-scheme.
(function () {
  var root = document.documentElement;
  var button = document.querySelector(".theme-toggle");
  var system = window.matchMedia("(prefers-color-scheme: dark)");
  if (!button) return;

  function current() {
    return root.dataset.theme || (system.matches ? "dark" : "light");
  }

  function label() {
    var next = current() === "dark" ? "light" : "dark";
    button.setAttribute("aria-label", "Switch to " + next + " theme");
    button.title = "Switch to " + next + " theme";
  }

  button.addEventListener("click", function () {
    var next = current() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) {}
    label();
  });
  system.addEventListener("change", label);
  label();
  button.hidden = false;
})();
