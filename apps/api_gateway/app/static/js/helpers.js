export function pretty(value) {
  return JSON.stringify(value, null, 2);
}
export function print(el, value, isError = false) {
  el.textContent = typeof value === "string" ? value : pretty(value);
  el.classList.toggle("error", isError);
}
export function el(id) {
  return document.getElementById(id);
}
export function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(n) / Math.log(1024));
  return `${(n / 1024 ** i).toFixed(1)} ${u[i]}`;
}
export function wsUrl(path) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}${path}`;
}

// ---- persisted UI preferences (localStorage) ----
export const PREFS_KEY = "stt-ui-prefs";
export function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
  } catch {
    return {};
  }
}
export function savePref(id, value) {
  const p = loadPrefs();
  p[id] = value;
  localStorage.setItem(PREFS_KEY, JSON.stringify(p));
}
export function controlValue(e) {
  return e.type === "checkbox" ? e.checked : e.value;
}
// Restore a control's saved value (selects: only if the option exists), then
// persist future changes. Safe to call after options are populated.
export function restoreAndBind(id) {
  const e = el(id);
  if (!e) return;
  const prefs = loadPrefs();
  if (id in prefs) {
    const v = prefs[id];
    if (e.tagName === "SELECT") {
      if ([...e.options].some((o) => o.value === v)) e.value = v;
    } else if (e.type === "checkbox") {
      e.checked = !!v;
    } else {
      e.value = v;
    }
  }
  if (!e.dataset.prefBound) {
    e.dataset.prefBound = "1";
    const evt = e.tagName === "SELECT" || e.type === "checkbox" ? "change" : "input";
    e.addEventListener(evt, () => savePref(id, controlValue(e)));
  }
}


export function setBadge(id, ok) {
  const e = el(id);
  if (!e) return;
  e.classList.toggle("ok", !!ok);
  e.classList.toggle("err", !ok);
}

export function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

// Runs `fn(id)` for every id in sequence. `fn` must resolve to
// `{ ok: true }` or `{ ok: false, error }` and must never throw (catch
// internally). Returns one "<label>: <error>" string per failed id — an
// empty array means every id succeeded.
export async function runBulk(ids, fn, describeId) {
  const errors = [];
  for (const id of ids) {
    const result = await fn(id);
    if (!result.ok) errors.push(`${describeId(id)}: ${result.error}`);
  }
  return errors;
}

// Prints a one-line summary of a completed bulk action to `statusEl`.
export function printBulkSummary(statusEl, total, errors, verb = "Updated") {
  if (errors.length) {
    print(statusEl, `${errors.length} of ${total} failed:\n${errors.join("\n")}`, true);
  } else {
    print(statusEl, `${verb} ${total} item${total === 1 ? "" : "s"}`);
  }
}
