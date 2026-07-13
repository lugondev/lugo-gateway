import { escapeHtml } from "./helpers.js";

function _buildOverlay(message, danger, inputValue) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal-card">
      <p class="modal-message">${escapeHtml(message)}</p>
      ${inputValue == null ? "" : `<input type="text" class="modal-input" value="${escapeHtml(inputValue)}" />`}
      <div class="modal-actions">
        <button type="button" class="ghost modal-cancel">Cancel</button>
        <button type="button" class="modal-ok${danger ? " danger" : ""}">OK</button>
      </div>
    </div>
  `;
  return overlay;
}

function _closeModal(overlay, keyHandler, resolve, value) {
  document.removeEventListener("keydown", keyHandler);
  overlay.remove();
  resolve(value);
}

// Styled replacement for window.confirm(). Resolves true/false.
export function confirmDialog(message, { danger = false } = {}) {
  return new Promise((resolve) => {
    const overlay = _buildOverlay(message, danger, null);
    document.body.appendChild(overlay);

    const okBtn = overlay.querySelector(".modal-ok");
    const cancelBtn = overlay.querySelector(".modal-cancel");

    function submit() { _closeModal(overlay, keyHandler, resolve, true); }
    function cancel() { _closeModal(overlay, keyHandler, resolve, false); }
    function keyHandler(e) {
      if (e.key === "Escape") cancel();
      if (e.key === "Enter") submit();
    }

    okBtn.addEventListener("click", submit);
    cancelBtn.addEventListener("click", cancel);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) cancel(); });
    document.addEventListener("keydown", keyHandler);
    okBtn.focus();
  });
}

// Styled replacement for window.prompt(). Resolves the entered string, or
// null if cancelled -- same contract as the native prompt().
export function promptDialog(message, defaultValue = "", { danger = false } = {}) {
  return new Promise((resolve) => {
    const overlay = _buildOverlay(message, danger, defaultValue);
    document.body.appendChild(overlay);

    const input = overlay.querySelector(".modal-input");
    const okBtn = overlay.querySelector(".modal-ok");
    const cancelBtn = overlay.querySelector(".modal-cancel");

    function submit() { _closeModal(overlay, keyHandler, resolve, input.value); }
    function cancel() { _closeModal(overlay, keyHandler, resolve, null); }
    function keyHandler(e) {
      if (e.key === "Escape") cancel();
      if (e.key === "Enter") submit();
    }

    okBtn.addEventListener("click", submit);
    cancelBtn.addEventListener("click", cancel);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) cancel(); });
    document.addEventListener("keydown", keyHandler);
    input.focus();
    input.select();
  });
}
