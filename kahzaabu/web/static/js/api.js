// Shared client helpers

async function fetchJSON(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
        const text = await r.text();
        throw new Error(`${r.status} ${r.statusText}: ${text}`);
    }
    return r.json();
}

function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    if (attrs) {
        for (const [k, v] of Object.entries(attrs)) {
            if (k === "class") e.className = v;
            else if (k === "html") e.innerHTML = v;
            else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
            else e.setAttribute(k, v);
        }
    }
    for (const c of children) {
        if (c == null) continue;
        if (typeof c === "string" || typeof c === "number") e.appendChild(document.createTextNode(c));
        else e.appendChild(c);
    }
    return e;
}

function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

function catClass(cat) {
    if (!cat) return "";
    return "cat-" + cat.toLowerCase().replace(/\s+/g, "-");
}
function catBadgeClass(cat) {
    if (!cat) return "context";
    return cat.toLowerCase().replace(/\s+/g, "-");
}

function fmtDate(s) {
    if (!s) return "";
    return s.substring(0, 10);
}

function qs(name) {
    const u = new URL(window.location);
    return u.searchParams.get(name);
}

function setNavActive(name) {
    document.querySelectorAll("header.site nav a").forEach(a => {
        a.classList.toggle("active", a.dataset.nav === name);
    });
}
