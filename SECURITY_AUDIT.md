# Security Audit Report

**Application:** ShowAdvance (3·2·1→THEATER)
**Date:** 2026-03-18
**Scope:** Full codebase — `app.py`, `db_adapter.py`, `init_db.py`, `static/js/app.js`, all templates, `install.sh`, `start.sh`

---

## Executive Summary

A comprehensive security audit of the ShowAdvance codebase identified **28 unique findings** across the application. One is **critical**, six are **high** severity, eleven are **medium**, and ten are **low**. The most urgent issue is a hardcoded secret key fallback that enables complete authentication bypass via session cookie forgery. The absence of CSRF protection and several authorization gaps compound the risk.

| Severity | Count |
|----------|-------|
| CRITICAL | 1     |
| HIGH     | 6     |
| MEDIUM   | 11    |
| LOW      | 10    |

---

## CRITICAL

### 1. Hardcoded Secret Key Fallback
- **Location:** `app.py:27`
- **Code:** `app.secret_key = os.environ.get('SECRET_KEY', 'dpc-advance-secret-change-in-production')`
- **Impact:** If `SECRET_KEY` is not set (e.g. running via `python app.py` instead of the systemd service), the app uses a static, publicly visible default. An attacker who knows this key can forge Flask session cookies and impersonate any user, including admins — bypassing all authentication.
- **Fix:** Remove the fallback. Fail at startup if `SECRET_KEY` is not configured:
  ```python
  app.secret_key = os.environ['SECRET_KEY']  # Fail hard if not set
  ```

---

## HIGH

### 2. No CSRF Protection
- **Location:** Entire application (all POST routes)
- **Impact:** No CSRF tokens exist anywhere. Every state-changing endpoint (delete shows, delete users, create admin accounts, reset passwords, change settings, send emails) can be triggered by a malicious website visited by a logged-in user.
- **Fix:** Add `flask_wtf.CSRFProtect(app)` and include CSRF tokens in all forms. For AJAX endpoints, validate the `Origin`/`Referer` header or require a custom header like `X-Requested-With`.

### 3. Open Redirect on Login
- **Location:** `app.py:904-905`
- **Code:** `next_url = request.form.get('next') or url_for('dashboard'); return redirect(next_url)`
- **Impact:** Attacker crafts `/login?next=https://evil.com` — after login, victim is redirected to a phishing site.
- **Fix:** Validate that `next_url` starts with `/` and does not start with `//`.

### 4. Missing Session Security Configuration
- **Location:** `app.py:26-27`
- **Impact:** No `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_SAMESITE`, or `PERMANENT_SESSION_LIFETIME` configured. Cookies sent over HTTP can be intercepted. Sessions never expire.
- **Fix:** Set `SESSION_COOKIE_SECURE=True`, `SESSION_COOKIE_SAMESITE='Lax'`, and `PERMANENT_SESSION_LIFETIME=timedelta(hours=8)`.

### 5. Rate Limiting Degrades Silently
- **Location:** `app.py:42-55`
- **Impact:** If `flask-limiter` is not installed, login has zero rate limiting, enabling unlimited brute-force. No warning is logged.
- **Fix:** Make `flask-limiter` a hard dependency. Tighten rate limit (e.g. 5/minute). Add account lockout.

### 6. Settings Page Exposes Sensitive Data to Non-Admin Users
- **Location:** `app.py:2547-2662`
- **Impact:** The `/settings` route requires only `@login_required`. SMTP password, WiFi password, and database credentials are passed to the template for all authenticated users.
- **Fix:** Gate sensitive settings behind `@admin_required`, or split into profile vs. admin settings routes.

### 7. SSRF via Ollama URL Setting
- **Location:** `app.py:3796-3802, 4085-4092`
- **Impact:** An admin can set `ollama_url` to any URL. The server makes HTTP requests to it, enabling probing of internal networks and cloud metadata endpoints (e.g. `169.254.169.254`).
- **Fix:** Validate the URL against an allowlist. Block private/link-local/metadata IP ranges.

---

## MEDIUM

### 8. Missing `can_access_show` on Archive/Delete Endpoints (IDOR)
- **Location:** `app.py:2370-2408`
- **Impact:** Archive, restore, and delete endpoints use `@staff_or_admin_required` but do not check `can_access_show()`. A restricted staff user can delete ANY show by guessing the integer ID.
- **Fix:** Add `can_access_show()` check, or restrict these to admin-only.

### 9. Stored XSS via Comment Bodies
- **Location:** `app.py:1570-1602`
- **Impact:** Comment bodies are stored and returned as raw text in JSON. If the frontend renders with `innerHTML`, stored XSS results.
- **Fix:** Sanitize HTML on input or ensure frontend always uses safe text insertion (`textContent`).

### 10. DOM XSS in Presence Badge
- **Location:** `static/js/app.js:162-165`
- **Impact:** `u.name` from server JSON is inserted into HTML via template literal without escaping. A malicious display name injects HTML.
- **Fix:** Wrap `u.name` and `u.tab` with `_esc()`.

### 11. DOM XSS in Schedule Copy/Template Functions
- **Location:** `static/js/app.js:485-537`
- **Impact:** Schedule cell values are interpolated into `innerHTML` without escaping. Double-quote in cell value breaks out of `value` attribute.
- **Fix:** Use `_esc()` on all interpolated values, or build DOM elements programmatically.

### 12. Content-Disposition Header Injection
- **Location:** `app.py:1840, 2238, 2246, 2296`
- **Impact:** Database-stored filenames placed directly into `Content-Disposition` headers. A filename containing `"` or newlines could inject headers.
- **Fix:** Apply `secure_filename()` at download time or use RFC 5987 encoding.

### 13. Email Header Injection
- **Location:** `app.py:394-397, 3857-3862`
- **Impact:** Subject, from, and recipient addresses from user input placed directly into email headers. Newline characters could inject additional headers/BCC recipients.
- **Fix:** Strip `\r` and `\n` from all email header values.

### 14. Direct Email as Open Relay
- **Location:** `app.py:456-529`
- **Impact:** The "direct" MX delivery method allows sending from any `smtp_from` address to any recipient — essentially an open relay for admins.
- **Fix:** Restrict `smtp_from` to verified domains. Rate-limit email sending. Log all sends.

### 15. Logo Upload SVG XSS
- **Location:** `app.py:3550-3572`
- **Impact:** Logo upload accepts any `content_type` from the client. An SVG with embedded JavaScript, stored as a data URI, enables stored XSS when rendered.
- **Fix:** Validate MIME type against allowlist (`image/png`, `image/jpeg`). Reject SVG or sanitize it. Verify magic bytes.

### 16. Session Roles Not Refreshed After Changes
- **Location:** `app.py:893-899`
- **Impact:** Role, `is_restricted`, and `is_content_admin` are cached at login and never refreshed. Demoted users retain privileges until they log out. No session expiration configured.
- **Fix:** Re-check roles from the database periodically or on each request.

### 17. No Password Complexity Requirements
- **Location:** `app.py:2720-2801`
- **Impact:** Any non-empty password is accepted, including single-character passwords. Combined with potentially absent rate limiting, brute-force is trivial.
- **Fix:** Enforce minimum 12 characters. Consider `zxcvbn` for strength checking.

### 18. PostgreSQL Silent Fallback to SQLite
- **Location:** `db_adapter.py:283-288`
- **Impact:** If PostgreSQL is configured but connection fails, the app silently falls back to SQLite, potentially reading/writing stale data with no warning.
- **Fix:** Log a warning or fail hard rather than silently degrading.

---

## LOW

### 19. Debug Mode in Direct Execution
- **Location:** `app.py:4629`
- **Code:** `app.run(debug=True, port=run_port)`
- **Impact:** Running `python app.py` directly enables the Werkzeug interactive debugger, allowing arbitrary code execution if an error page is triggered.
- **Fix:** Use `debug=os.environ.get('FLASK_DEBUG', '0') == '1'`.

### 20. Missing Security Headers
- **Location:** `app.py` (absent)
- **Impact:** No `Content-Security-Policy`, `X-Frame-Options`, `X-Content-Type-Options`, or `Strict-Transport-Security` headers. App can be framed (clickjacking).
- **Fix:** Add headers via `@app.after_request`.

### 21. Public Endpoints Expose All Active Shows
- **Location:** `app.py:3606-3655`
- **Impact:** `/public` endpoints require no auth. Sequential integer IDs make enumeration trivial. PDFs may contain WiFi passwords, contacts, and venue security details.
- **Fix:** Add per-show public toggle with random URL slugs instead of integer IDs.

### 22. WiFi Password in Public PDFs
- **Location:** `app.py:2148-2158, 3638-3655`
- **Impact:** WiFi credentials embedded in schedule PDFs served without authentication via `/public` routes.
- **Fix:** Exclude WiFi credentials from public-facing PDFs.

### 23. SMTP Password in Plaintext DB
- **Location:** `app.py:2619-2624, db_adapter.py:215`
- **Impact:** SMTP and PostgreSQL passwords stored as plaintext in `app_settings` table. DB file access exposes credentials.
- **Fix:** Encrypt at rest or use environment variables.

### 24. f-string SQL Patterns (Maintenance Hazard)
- **Location:** `app.py:2400-2402, 2497-2516; db_adapter.py:239-240`
- **Impact:** Table/schema names interpolated via f-strings. Currently hardcoded (safe), but the pattern invites injection if modified.
- **Fix:** Add security comments. Validate schema names from user settings against strict patterns.

### 25. Database Path Information Disclosure
- **Location:** `app.py:3727`
- **Impact:** Full filesystem path of SQLite DB returned to admin client. Useful for further attacks if session is compromised.
- **Fix:** Return generic success message.

### 26. Exception Messages Leaked to Client
- **Location:** `app.py:3324, 3758, 3809, 4101, 4950`
- **Impact:** Raw exception strings (potentially containing paths, SQL details) returned in JSON error responses.
- **Fix:** Log full exceptions server-side; return generic errors to client.

### 27. Missing Restricted-User Check on Labor Request Reorder
- **Location:** `app.py:4380-4395`
- **Impact:** Read-only users can reorder labor requests (other labor endpoints enforce the check).
- **Fix:** Add `is_restricted` check.

### 28. Unencrypted SMTP Fallback
- **Location:** `app.py:503-510`
- **Impact:** If STARTTLS is not supported, email is sent in plaintext with no warning.
- **Fix:** Log a warning. Add config option to require TLS.

---

## Recommended Priority Actions

1. **Immediately** remove the hardcoded secret key fallback (#1)
2. **Add CSRF protection** across all endpoints (#2)
3. **Fix the open redirect** on login (#3)
4. **Configure session cookie security** (#4)
5. **Make rate limiting a hard dependency** (#5)
6. **Restrict `/settings` to admin users** for sensitive data (#6)
7. **Validate Ollama URL** against allowlist (#7)
8. **Add `can_access_show` to archive/delete** (#8)
9. **Sanitize user input** in comment bodies, email headers, filenames (#9, #12, #13)
10. **Fix DOM XSS** in `app.js` presence badge and schedule functions (#10, #11)
