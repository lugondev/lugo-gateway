import { escapeHtml } from "./helpers.js";

// Renders a checkbox-selectable table into `container`. Returns the built
// <table> (so callers can wire up their own per-column controls), or null
// if there are no rows (container is filled with `emptyMessage` instead).
export function renderDataTable({
  container,
  columns,
  rows,
  rowKey,
  getRowClass,
  bulkActions = [],
  emptyMessage = "No entries yet.",
}) {
  if (!container) return null;
  if (!rows.length) {
    container.innerHTML = `<p class="hint">${escapeHtml(emptyMessage)}</p>`;
    return null;
  }

  const selected = new Set();
  const toolbar = document.createElement("div");
  const table = document.createElement("table");
  table.className = "data-table";

  const thead = document.createElement("thead");
  thead.innerHTML = `
    <tr>
      <th class="dt-checkbox-cell"><input type="checkbox" /></th>
      ${columns.map((c) => `<th${c.headerClass ? ` class="${c.headerClass}"` : ""}>${escapeHtml(c.label)}</th>`).join("")}
    </tr>
  `;
  const selectAllCheckbox = thead.querySelector("input");

  const tbody = document.createElement("tbody");
  tbody.innerHTML = rows.map((row) => `
    <tr class="${getRowClass ? getRowClass(row) : ""}">
      <td class="dt-checkbox-cell"><input type="checkbox" /></td>
      ${columns.map((c) => `<td${c.cellClass ? ` class="${c.cellClass}"` : ""}>${c.render(row)}</td>`).join("")}
    </tr>
  `).join("");

  table.append(thead, tbody);

  function renderToolbar() {
    if (selected.size === 0) {
      toolbar.innerHTML = "";
      toolbar.classList.remove("dt-toolbar");
      return;
    }
    toolbar.classList.add("dt-toolbar");
    toolbar.innerHTML = `
      <span class="dt-toolbar-count">${selected.size} selected</span>
      <div class="dt-toolbar-actions"></div>
    `;
    const actionsHost = toolbar.querySelector(".dt-toolbar-actions");
    bulkActions.forEach((action) => {
      const btn = document.createElement("button");
      btn.className = "mini ghost";
      btn.type = "button";
      btn.textContent = action.label;
      btn.addEventListener("click", () => {
        const ids = [...selected];
        const selectedRows = rows.filter((r) => selected.has(rowKey(r)));
        action.run(ids, selectedRows);
      });
      actionsHost.appendChild(btn);
    });
  }

  function updateSelectAllState() {
    selectAllCheckbox.checked = selected.size > 0 && selected.size === rows.length;
    selectAllCheckbox.indeterminate = selected.size > 0 && selected.size < rows.length;
  }

  [...tbody.children].forEach((tr, i) => {
    const id = rowKey(rows[i]);
    const cb = tr.querySelector("input[type=checkbox]");
    cb.addEventListener("change", () => {
      if (cb.checked) selected.add(id); else selected.delete(id);
      tr.classList.toggle("dt-row-selected", cb.checked);
      updateSelectAllState();
      renderToolbar();
    });
  });

  selectAllCheckbox.addEventListener("change", () => {
    const checked = selectAllCheckbox.checked;
    selected.clear();
    [...tbody.children].forEach((tr, i) => {
      const cb = tr.querySelector("input[type=checkbox]");
      cb.checked = checked;
      tr.classList.toggle("dt-row-selected", checked);
      if (checked) selected.add(rowKey(rows[i]));
    });
    updateSelectAllState();
    renderToolbar();
  });

  container.innerHTML = "";
  container.append(toolbar, table);
  return table;
}
