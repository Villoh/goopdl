// Motion for the landing page. No dependencies; everything degrades to static content.
document.documentElement.classList.add("js");
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
const sleep = (ms) => new Promise((r) => setTimeout(r, still ? 0 : ms));

// ── install tabs: pick the visitor's OS first, others one click away ─────
const guess = { Win: "windows", Mac: "macos" }[navigator.userAgent.match(/Win|Mac/)?.[0]] ?? "linux";
document.querySelectorAll(".install").forEach((box) => {
  const strip = box.querySelector(".tabs");
  const tabs = [...strip.querySelectorAll('[role="tab"]')];
  const panels = [...box.querySelectorAll('[role="tabpanel"]')];
  const current = () => tabs.find((b) => b.ariaSelected === "true");
  // The thumb slides to the selected tab; the new command slides in from that side.
  const thumb = () => {
    strip.style.setProperty("--x", `${current().offsetLeft}px`);
    strip.style.setProperty("--w", `${current().offsetWidth}px`);
  };
  const select = (t) => {
    const from = tabs.indexOf(t) < tabs.indexOf(current()) ? "-28px" : "28px";
    tabs.forEach((b) => b.setAttribute("aria-selected", b === t));
    panels.forEach((p) => {
      p.style.setProperty("--from", from);
      p.hidden = p.dataset.tab !== t.dataset.tab;
    });
    thumb();
  };
  tabs.forEach((t) => t.addEventListener("click", () => t !== current() && select(t)));
  select(tabs.find((t) => t.dataset.tab === guess) ?? tabs[0]);
  document.fonts.ready.then(thumb); // tab widths change once the font loads
  addEventListener("resize", thumb);
  requestAnimationFrame(() => strip.classList.add("ready")); // no slide on first paint
});

// ── scroll reveal: adds .in once an element enters the viewport ───────────
const seen = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    e.target.classList.add("in");
    e.target.dispatchEvent(new Event("in"));
    seen.unobserve(e.target);
  }
}, { threshold: 0.25 });
document.querySelectorAll(".reveal, .pipeline, .account-stage, .stat, .final").forEach((el) => seen.observe(el));

// ── terminal: types the command, then plays the download, on a loop ───────
const term = document.getElementById("term");
const cmd = document.getElementById("cmd");
const TOKENS = [["goopdl", "b"], [" download", ""], [" com.whatsapp", "pkg"], [" --splits", "flag"]];

function bar(row, seconds) {
  const fill = row.querySelector(".bar i");
  const pc = row.querySelector(".pc");
  const t0 = performance.now();
  return new Promise((done) => {
    (function frame(now) {
      const p = still ? 1 : Math.min((now - t0) / (seconds * 1000), 1);
      const eased = 1 - (1 - p) ** 3;
      fill.style.transform = `scaleX(${eased})`;
      pc.textContent = `${Math.round(eased * 100)}%`;
      if (p < 1) return requestAnimationFrame(frame);
      row.classList.add("done");
      done();
    })(t0);
  });
}

async function play() {
  const steps = term.querySelectorAll(".step");
  const rows = term.querySelectorAll(".row");
  cmd.innerHTML = "";
  term.classList.remove("ran");
  steps.forEach((s) => s.classList.remove("on"));
  rows.forEach((r) => { r.classList.remove("done"); r.querySelector(".bar i").style.transform = "scaleX(0)"; r.querySelector(".pc").textContent = "0%"; });

  await sleep(700);
  for (const [text, cls] of TOKENS) {
    const span = document.createElement(cls === "b" ? "b" : "span");
    if (cls && cls !== "b") span.className = cls;
    cmd.append(span);
    for (const ch of text) { span.textContent += ch; await sleep(55); }
  }
  await sleep(500);
  term.classList.add("ran");
  steps[0].classList.add("on"); await sleep(450);
  steps[1].classList.add("on"); await sleep(300);
  steps[2].classList.add("on");
  await Promise.all([...rows].map(async (r, i) => { await sleep(i * 120); return bar(r, parseFloat(r.style.getPropertyValue("--t"))); }));
  steps[3].classList.add("on"); await sleep(400);
  steps[4].classList.add("on");
  if (still) return;
  await sleep(5000);
  play();
}
play();

// ── devices: slot roll through the arches, phone turns into a TV at the end ─
const ARCHS = [["arm64-v8a"], ["armeabi-v7a"], ["x86"], ["x86_64"], ["Android TV", true]];
const slot = document.getElementById("slot");
const device = document.getElementById("device");
const label = document.getElementById("device-label");
let arch = 0;
function nextArch() {
  arch = (arch + 1) % ARCHS.length;
  slot.style.transform = `translateY(${-arch * 1.2}em)`;
  label.textContent = ARCHS[arch][0];
  device.classList.toggle("tv", !!ARCHS[arch][1]);
  device.classList.remove("bump"); void device.offsetWidth; device.classList.add("bump");
}
if (!still) setInterval(nextArch, 1400);

// ── stats: count up when visible ──────────────────────────────────────────
document.querySelectorAll("[data-count]").forEach((el) => {
  el.closest(".stat").addEventListener("in", () => {
    const to = +el.dataset.count, suffix = el.dataset.suffix || "", t0 = performance.now();
    (function frame(now) {
      const p = still ? 1 : Math.min((now - t0) / 1200, 1);
      el.textContent = Math.round(to * (1 - (1 - p) ** 3)) + suffix;
      if (p < 1) requestAnimationFrame(frame);
    })(t0);
  });
});

