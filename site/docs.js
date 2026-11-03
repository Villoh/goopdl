// Docs pages are prerendered by build.py; this only highlights the current section in the TOC
// and, on mobile, scrolls the horizontal guide tabs so the active one is visible.
{
  const headings = [...document.querySelectorAll("#doc h2, #doc h3")];
  const links = document.querySelectorAll("#toc a");

  const spy = () => {
    let current = headings[0];
    for (const h of headings) if (h.getBoundingClientRect().top < 140) current = h;
    links.forEach((a) => a.classList.toggle("on", a.hash === `#${current?.id}`));
  };
  addEventListener("scroll", () => requestAnimationFrame(spy), { passive: true });
  spy();

  const active = document.querySelector('#doc-nav [aria-current="page"]');
  active?.parentElement.scrollTo({ left: active.offsetLeft - 20 });
}
