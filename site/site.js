// Shared by every page: theme toggle, language picker, copy buttons, nav shadow.
// Pages are static per language (/ and /es/); nothing here translates text.
{
  const root = document.documentElement;
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
  };

  addEventListener("scroll", () => document.querySelector(".nav").classList.toggle("scrolled", scrollY > 10), { passive: true });
  addEventListener("load", () => root.classList.add("loaded")); // enables smooth scrolling, see style.css

  // ── theme: the <head> script already applied it before paint ────────────
  const button = document.querySelector(".theme");
  const meta = document.querySelector('meta[name="theme-color"]');
  const paint = (theme) => {
    root.dataset.theme = theme;
    button.setAttribute("aria-pressed", theme === "dark");
    meta.content = theme === "dark" ? "#0F0F0E" : "#F3F0E8";
  };
  paint(root.dataset.theme);

  button.addEventListener("click", () => {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    store.set("theme", next);
    if (!document.startViewTransition || reduced()) return paint(next);
    // The new theme grows as a circle out of the toggle. .theme-vt scopes the CSS to this transition,
    // so page-to-page navigations keep the default crossfade.
    const r = button.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const end = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    root.classList.add("theme-vt");
    const vt = document.startViewTransition(() => paint(next));
    vt.ready.then(() => root.animate(
      { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${end}px at ${x}px ${y}px)`] },
      { duration: 700, easing: "cubic-bezier(.16, 1, .3, 1)", pseudoElement: "::view-transition-new(root)" },
    ));
    vt.finished.finally(() => root.classList.remove("theme-vt"));
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
    if (!store.get("theme")) paint(e.matches ? "dark" : "light");
  });

  // ── language: links between / and /es/ ───────────────────────────────────
  const links = document.querySelectorAll("[data-lang]");
  const target = (code) => document.querySelector(`[data-lang="${code}"]`);
  // English page, but the visitor picked Spanish before (or, with no pick, the browser prefers it): go to
  // the Spanish page. Only en → es, so /es/ links always stay Spanish and crawlers (English, no pick) see both.
  if (root.lang === "en") {
    const wanted = store.get("lang") ?? navigator.languages.map((l) => l.slice(0, 2).toLowerCase()).find((l) => l === "en" || l === "es");
    if (wanted === "es") location.replace(target("es").href + location.hash);
  }
  links.forEach((a) => a.addEventListener("click", (e) => {
    store.set("lang", a.dataset.lang);
    if (a.dataset.lang === root.lang || e.metaKey || e.ctrlKey || e.shiftKey) return;
    // Slide the picker's thumb first, then leave.
    e.preventDefault();
    links.forEach((l) => l.setAttribute("aria-current", l === a));
    setTimeout(() => location.assign(a.href + location.hash), reduced() ? 0 : 220);
  }));

  // ── copy: install pills ([data-copy]) and code blocks (.copy-code) ───────
  const COPIED = { en: "Copied", es: "Copiado" };
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy], .copy-code");
    if (!btn) return;
    const text = btn.dataset.copy ?? btn.closest(".code").querySelector("code").textContent.trimEnd();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard blocked (permissions, some in-app browsers): select the code so it can be copied by hand.
      const code = btn.closest(".code")?.querySelector("code"); // install pills have none
      if (code) getSelection().selectAllChildren(code);
      return;
    }
    const label = btn.querySelector(".copy-label") ?? btn;
    const original = label.dataset.label ??= label.textContent;
    btn.classList.add("copied");
    label.textContent = COPIED[root.lang] ?? COPIED.en;
    clearTimeout(btn.timer);
    btn.timer = setTimeout(() => { btn.classList.remove("copied"); label.textContent = original; }, 1600);
  });
}
