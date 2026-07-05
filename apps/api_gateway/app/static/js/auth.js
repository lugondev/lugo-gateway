const ORIGINAL_FETCH = window.fetch.bind(window);

function installUnauthorizedRedirect() {
  window.fetch = async (...args) => {
    const resp = await ORIGINAL_FETCH(...args);
    if (resp.status === 401 && !window.location.pathname.endsWith("/login.html")) {
      window.location.href = "/static/login.html";
    }
    return resp;
  };
}

async function handleLoginSubmit(e) {
  e.preventDefault();
  const password = document.getElementById("login-password").value;
  const status = document.getElementById("login-status");
  status.textContent = "";
  const resp = await ORIGINAL_FETCH("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (resp.ok) {
    window.location.href = "/ui";
  } else {
    status.textContent = "Invalid password";
  }
}

async function handleLogout() {
  await ORIGINAL_FETCH("/api/auth/logout", { method: "POST" });
  window.location.href = "/static/login.html";
}

const loginForm = document.getElementById("login-form");
if (loginForm) {
  loginForm.addEventListener("submit", handleLoginSubmit);
} else {
  installUnauthorizedRedirect();
  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) logoutBtn.addEventListener("click", handleLogout);
}
