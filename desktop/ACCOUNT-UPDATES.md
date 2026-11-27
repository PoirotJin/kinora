# Accounts and signed updates

For public Kinora installers and updates, use the [guarded GitHub publishing workflow](https://github.com/PoirotJin/kinora/blob/main/docs/PUBLISHING.md). The server-side update examples below apply to legacy private deployments.

## Current account flow (0.6.0)

The desktop opens the selected server account page in the system default browser. New registration submits an administrator approval request without email. Applicants choose their password and can check progress; approval creates an ordinary user. Without email recovery, password reset is handled by an administrator. The historical email flow below describes compatibility only.

# Accounts and signed updates (0.4.1)

## Account website

The login gate and Settings open `/account?view=register` or `/account?view=forgot`
in a separate remote window without IPC access. The website uses the desktop
paper/ink tokens, hairline fields and bundled OFL Garamond wordmark.

Registration collects a username, email and password. No user is created until
an email link is explicitly confirmed. Confirmation creates an ordinary user
with no mandatory temporary-password change. Email or username can then log in.
Links expire in 30 minutes; only token hashes are stored, links are single-use,
and reset invalidates all browser/desktop login sessions. Disabled accounts
cannot reset their way back in. Existing accounts without verified email retain
the existing administrator reset flow; this change does not invent or assign
email addresses to existing users. Gadget API keys are a separate credential.

Deploy `scripts/review_email_accounts.py`, `scripts/review_account_routes.py`,
the small auth/schema changes, the public-route hook in `review_server.py`, and
`web/account/`. Do not replace a newer deployed server with an older branch copy.

### Enable mail

A real verified sender is required. With no SMTP configuration the page clearly
shows mail unavailable, and account writes return 503 without creating users.
No mail is sent to logs or returned in API responses as a fallback.

1. Provision a sender and its SMTP credentials with a mail provider. Configure
   its required SPF/DKIM records if using your own domain.
2. Copy `deploy/account-mail.env.example` to `/etc/motionfarm/account-mail.env`,
   owned by root with mode 0600, and fill it on the server. Do not commit it.
3. Install `deploy/account-mail.conf` as
   `/etc/systemd/system/motionfarm.service.d/account-mail.conf`.
4. Reload systemd and restart **only** `motionfarm.service`.
5. Confirm `/api/account/status` reports `email_enabled: true`, then verify real
   delivery, registration, login and reset with a disposable test mailbox.

STARTTLS (587) and implicit TLS (465) verify certificates. There is no plaintext
SMTP mode. The canonical HTTPS origin must match the server's allowed origins.

## Updates

0.3.0 had no updater, so users must install 0.4.1 once manually. Subsequent
versions use Settings → Check updates → Download update → Restart to install.
The public key is pinned in `tauri.conf.json`; the private signing key must remain
outside the repository. Keep a secure backup: losing it strands existing clients.
Signing updates is distinct from Apple/Windows OS code signing/notarization.

Ordinary developer builds do not require the private key. To build a publishable
update set `TAURI_SIGNING_PRIVATE_KEY` and, if needed,
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, then use:

```sh
npx tauri build --config src-tauri/tauri.release.conf.json
```

The CI workflow accepts `signed_updates: true` on manual dispatch. Configure the
matching private key in the repository secret `TAURI_SIGNING_PRIVATE_KEY`; no
key is printed or committed. It produces signed macOS `.app.tar.gz`, Windows
NSIS `.exe`, and Linux `.AppImage` artifacts; `.dmg`/`.deb` remain for initial
installation. Linux in-place updates require an AppImage installation.

Assemble a server release directory using `scripts/desktop_release.py`, e.g.:

```sh
python3 scripts/desktop_release.py --version 0.4.1 --output /tmp/mf-release \
  --artifact darwin-aarch64=/path/to/kinora.app.tar.gz \
  --artifact darwin-x86_64=/path/to/kinora.app.tar.gz
```

Only map both macOS architectures if the bundle is universal. The `.sig` must
sit beside each artifact. Use a unique version and never replace an artifact's
bytes. Upload artifacts to `var/desktop-releases/` on the kinora server, then replace
`latest.json` atomically **last**. The directory can be overridden with
`MOTIONFARM_DESKTOP_RELEASES`. It contains public installers, never credentials.
The feed selects the exact platform/architecture. Missing releases return an
unavailable state, not “latest version”. Downloads are streamed with Range
support. Tauri verifies the signature before making installation available;
failures return to a retryable state. A restart during download discards the
in-memory partial download and keeps the installed version intact.

Bump `package.json`, its lockfile, `Cargo.toml`, `Cargo.lock` and
`tauri.conf.json` together for a new release. Before publishing a new platform,
exercise N → N+1 on that OS. Windows and Linux runtime installation cannot be
certified by a macOS-only build/test pass.
