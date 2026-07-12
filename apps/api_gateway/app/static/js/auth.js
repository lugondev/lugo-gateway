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

function showForm(name) {
  document.getElementById("login-form").classList.toggle("active", name === "login");
  document.getElementById("signup-form").classList.toggle("active", name === "signup");
  document.getElementById("login-status").textContent = "";
}

async function handleLoginSubmit(e) {
  e.preventDefault();
  const username = document.getElementById("login-username").value;
  const password = document.getElementById("login-password").value;
  const status = document.getElementById("login-status");
  status.textContent = "";
  const resp = await ORIGINAL_FETCH("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (resp.ok) {
    window.location.href = "/ui";
  } else {
    status.textContent = "Invalid username or password";
  }
}

async function handleSignupSubmit(e) {
  e.preventDefault();
  const username = document.getElementById("signup-username").value;
  const password = document.getElementById("signup-password").value;
  const status = document.getElementById("login-status");
  status.textContent = "";
  const signupResp = await ORIGINAL_FETCH("/api/auth/signup", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!signupResp.ok) {
    status.textContent = signupResp.status === 409
      ? "That username is already taken"
      : "Could not create account";
    return;
  }
  const loginResp = await ORIGINAL_FETCH("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (loginResp.ok) {
    window.location.href = "/ui";
  } else {
    status.textContent = "Account created — please log in";
    showForm("login");
  }
}

async function handleLogout() {
  await ORIGINAL_FETCH("/api/auth/logout", { method: "POST" });
  window.location.href = "/static/login.html";
}

const loginForm = document.getElementById("login-form");
if (loginForm) {
  loginForm.addEventListener("submit", handleLoginSubmit);
  document.getElementById("signup-form").addEventListener("submit", handleSignupSubmit);
  document.getElementById("show-signup").addEventListener("click", (e) => {
    e.preventDefault();
    showForm("signup");
  });
  document.getElementById("show-login").addEventListener("click", (e) => {
    e.preventDefault();
    showForm("login");
  });
} else {
  installUnauthorizedRedirect();
  const logoutBtn = document.getElementById("btn-logout");
  if (logoutBtn) logoutBtn.addEventListener("click", handleLogout);
}
