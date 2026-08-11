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

function pluginUiUrl(pluginUrl, gatewayOrigin, token) {
  const url = new URL(`${String(pluginUrl).replace(/\/+$/, "")}/ui`);
  url.searchParams.set("gateway", gatewayOrigin);
  url.searchParams.set("token", token);
  return url.toString();
}

// The plugin's own page is served cross-origin and has no session cookie of
// its own, so it cannot authenticate to the gateway on its own -- the
// gateway has to hand it a ticket. This page's own fetches ARE same-origin
// (see loadPluginNav below), so this one goes through the browser's session
// cookie automatically, same as every other admin fetch in this file set.
async function mintTicket(pluginName) {
  const resp = await fetch("/v1/plugins/ticket", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plugin: pluginName }),
  });
  if (!resp.ok) return null;
  const body = await resp.json();
  return body?.data?.token || null;
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
  const defaultTitle = btn.title;
  btn.addEventListener("click", async () => {
    // Guard against a double-click minting two tickets and opening two tabs
    // while the first request is still in flight.
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const token = await mintTicket(name);
      if (!token) {
        btn.title = "could not reach the gateway for a ticket -- try again";
        return;
      }
      window.open(pluginUiUrl(entry.url, window.location.origin, token), "_blank", "noopener");
      btn.title = defaultTitle;
    } catch {
      btn.title = "could not reach the gateway for a ticket -- try again";
    } finally {
      btn.disabled = false;
    }
  });
  li.appendChild(btn);

  // Keep plugin tabs grouped right after "Chat", same slot the old hardcoded
  // Livehost tab occupied. Falls back to prepending if that anchor is ever
  // renamed or removed.
  const chatLi = list.querySelector('.nav-item[data-section="chat"]')?.closest("li");
  // NOT `li:has(> .nav-item-plugin):last-of-type` -- :last-of-type restricts
  // candidates to the literal last <li> child of the list regardless of
  // :has(), which in this sidebar is always an admin item (e.g. "system"),
  // never a plugin item, since plugins are inserted mid-list right after
  // "Chat". That combination silently never matched, so every plugin after
  // the first landed right after Chat instead of after its predecessor --
  // reversing registration order. Track the last plugin <li> directly.
  const pluginLis = list.querySelectorAll(".nav-item-plugin");
  const lastPluginLi = pluginLis.length ? pluginLis[pluginLis.length - 1].closest("li") : null;
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
