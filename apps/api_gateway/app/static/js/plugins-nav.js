// Discovers feature plugins from GET /v1/plugins and renders one nav item per
// enabled, kind:"feature" plugin — instead of a hardcoded tab per plugin.
// Adding a second plugin (e.g. lugo, after livehost) needs no change here or
// in index.html: it just shows up the next time this runs.
//
// A plugin has no in-page "section" to switch to (its UI is served by the
// plugin itself, cross-origin), so its nav item opens `<url>/ui` in a new tab
// rather than participating in the sidebar's section-switching. That is also
// why these buttons intentionally do NOT get a `data-section` attribute:
// sidebar-nav.js only wires its section-switch handling to nav items that
// have one, so plugin nav items stay inert to that logic regardless of
// load-order between this module and sidebar-nav.js.

// Turns "my-plugin_name" into "My Plugin Name" for a readable label.
function pluginLabel(name) {
  const words = String(name)
    .split(/[-_]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1));
  return words.length ? words.join(" ") : String(name);
}

function pluginUiUrl(pluginUrl) {
  return `${String(pluginUrl).replace(/\/+$/, "")}/ui`;
}

function addPluginNavItem(name, entry) {
  const list = document.querySelector(".nav-list");
  if (!list) return;

  const li = document.createElement("li");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "nav-item nav-item-plugin";
  btn.dataset.plugin = name;
  btn.title = `Open ${name} in a new tab`;

  const icon = document.createElement("span");
  icon.className = "nav-icon";
  icon.textContent = "⧉"; // ⧉ — two overlapping squares, reads as "opens elsewhere"
  const label = document.createElement("span");
  label.className = "nav-label";
  label.textContent = pluginLabel(name);

  btn.appendChild(icon);
  btn.appendChild(label);
  btn.addEventListener("click", () => {
    window.open(pluginUiUrl(entry.url), "_blank", "noopener");
  });
  li.appendChild(btn);

  // Keep plugin tabs grouped right after "Chat", same slot the old hardcoded
  // Livehost tab occupied. Falls back to prepending if that anchor is ever
  // renamed or removed.
  const chatLi = list.querySelector('.nav-item[data-section="chat"]')?.closest("li");
  const lastPluginLi = list.querySelector("li:has(> .nav-item-plugin):last-of-type");
  const after = lastPluginLi || chatLi;
  if (after && after.nextSibling) list.insertBefore(li, after.nextSibling);
  else if (after) list.appendChild(li);
  else list.insertBefore(li, list.firstChild);
}

export async function loadPluginNav() {
  try {
    const resp = await fetch("/v1/plugins");
    if (!resp.ok) return; // no permission (not logged in / guard rejected it), or a server error — leave the rest of the sidebar alone
    const body = await resp.json();
    const plugins = (body && body.data) || {};
    Object.entries(plugins)
      .filter(([, entry]) => entry && entry.enabled && entry.kind === "feature")
      .forEach(([name, entry]) => addPluginNavItem(name, entry));
  } catch {
    /* network error, or an unparseable response — a broken plugin list must
       not take down the rest of the panel; the built-in sections still work. */
  }
}
