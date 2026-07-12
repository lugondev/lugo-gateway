let _statusPromise = null;

export function fetchAuthStatus() {
  if (!_statusPromise) {
    _statusPromise = fetch("/api/auth/status").then((r) => r.json());
  }
  return _statusPromise;
}
