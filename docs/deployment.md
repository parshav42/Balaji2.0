# BALAJI 2.0 deployment

## Launch and migrate

~~~bash
./setup_linux.sh --check
./setup_linux.sh
./run_linux.sh
~~~




Setup checks Linux and model prerequisites, installs dependencies into the project environment, runs tests, and applies additive database migrations. BALAJI creates a private SQLite snapshot under data/backups before upgrading an existing database. It also copies local environment configuration. Setup leaves system packages, firewall settings, services, and accounts to you. Local access is at **http://127.0.0.1:8000**.

## Approve laptop projects

Save a JSON object such as `{"Python AI project":"/home/your-user/projects/ai-agent"}` in `data/projects.json`. Use the full path from `pwd -P` and choose a specific project directory. Set `BALAJI_PROJECTS_FILE` to that file's absolute path before starting BALAJI. Paths stay within the configured projects. Hidden paths, symlinks, overwrites and arbitrary command execution are blocked.

Run BALAJI from the laptop's graphical session. Ask “Open Python AI project in VS Code,” then review and approve the action. You can also request it from **Files**. BALAJI reports the launcher result or a started process. GUI window visibility depends on the session and application. Approved text and Markdown reports can be downloaded from the dashboard.

## Private phone access

1. With your consent, install Tailscale on your laptop and phone and sign both into your tailnet. Keep the laptop awake, connected and running BALAJI. Follow Tailscale’s prompt to enable HTTPS if needed. Limit laptop HTTPS access to your identity or devices in the tailnet policy. [Tailscale Serve documentation](https://tailscale.com/docs/features/tailscale-serve)
2. Run these commands on the laptop. Use the exact hostname printed by Serve. Keep Uvicorn and Ollama bound to loopback.

   ~~~bash
   tailscale status
   tailscale serve --bg http://127.0.0.1:8000
   tailscale serve status
   export BALAJI_REMOTE_HOST='your-laptop.your-tailnet.ts.net'
   ./run_linux.sh
   ~~~

3. In a second terminal on the laptop using the same database settings, run `.venv/bin/python -m balaji.access owner`. Open **http://127.0.0.1:8000** on the laptop and use the one-use owner code to sign in.
4. In **Connected Devices**, create a pairing code. On your phone, open the exact private **https://** URL, enter a device name and the code, then approve the displayed device in the laptop dashboard. Codes expire after ten minutes. Approved phone sessions expire after seven days.
5. Use the phone browser’s **Install** or **Add to Home Screen** action to install the PWA. If prompted, tap the microphone once and allow access, then say “Check my laptop status.” Revoke lost or unused browsers from **Devices**. The public app shell displays an offline state and reconnects when the laptop becomes reachable. Remote actions require the laptop to be powered on and reachable.

Use private Tailscale Serve. This setup leaves Funnel, public port forwarding, and remote Ollama disabled. [Serve command reference](https://tailscale.com/docs/reference/tailscale-cli/serve)

## Gmail

1. In a Google Cloud project you control, enable Gmail API, configure OAuth consent, and add your account as a test user if the app uses Testing. Create a **Web application** OAuth client with this exact redirect URI: `http://127.0.0.1:8000/api/gmail/callback`. Google may require verification for broader deployment or restricted scopes. [Google web-server OAuth setup](https://developers.google.com/identity/protocols/oauth2/web-server) · [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
2. Protect the downloaded client configuration with mode 600 outside the repository or under ignored `data/`. Start BALAJI with:

   ~~~bash
   chmod 600 /path/to/client.json
   export BALAJI_GMAIL_CLIENT_FILE=/path/to/client.json
   export BALAJI_GMAIL_REDIRECT_URI=http://127.0.0.1:8000/api/gmail/callback
   ./run_linux.sh
   ~~~

3. In the laptop browser at **http://127.0.0.1:8000**, select **Email → Connect Gmail** and review Google’s consent screen. BALAJI uses `gmail.readonly` and `gmail.send`. The credentials are kept in a private file under `private/` beside the database. You can disconnect from Email or revoke the grant from your Google account.
4. Ask “Summarize important unread emails” or “Draft a reply to the client in the Delivery thread.” Drafts stay local. To send one, request send approval and review the exact recipient, subject and body. BALAJI records the Gmail API receipt. If a send is interrupted, check Gmail Sent before sending again.

## Messaging and Android

The PWA can prepare an SMS or WhatsApp message and open a composer for you to review and send. It requires an installed device app and a full international phone number. For SMS on iPhone, use **Copy message** and paste the body in Messages after opening the composer. [Apple SMS link format](https://developer.apple.com/library/archive/featuredarticles/iPhoneURLScheme_Reference/SMSLinks/SMSLinks.html)

A native Android build, notification listener, WhatsApp previews and inline notification replies remain future work. An Android preview connector would require an optional notification listener, a WhatsApp package allowlist, minimal redacted payloads, and revocable pairing. Android SMS compose should use the system `ACTION_SENDTO` intent. Browser reminder notifications require a connected dashboard and explicit browser permission. [Android notification listener](https://developer.android.com/reference/android/service/notification/NotificationListenerService) · [Android SMS intents](https://developer.android.com/guide/components/intents-common#Messaging)

## Optional systemd user service

Copy [the example unit](../scripts/balaji.service.example) to `~/.config/systemd/user/balaji.service` and adjust `WorkingDirectory` and `ExecStart` to your checkout. Set project variables in `~/.config/balaji/environment` using `NAME=value` lines. From the laptop desktop session:

~~~bash
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_SESSION_TYPE XDG_RUNTIME_DIR
systemctl --user daemon-reload
systemctl --user enable --now balaji.service
~~~

Stop the manual server before enabling the service on the same port. A user service depends on the login/session configuration and cannot wake a sleeping laptop.

## Verification and limits

- Run browser composer tests with `node --test tests/test_composers.cjs`. Test initial chat, a spoken request and follow-up, enrollment and revocation from a phone, mock Gmail approval in the test suite, and a near-future recurring reminder from **Automations**. Experience depends on the active Linux session, your physical microphone, and tailnet connectivity.
- Reminders and task summaries persist in SQLite, default to `Asia/Kolkata`, and run while BALAJI is up. A missed recurring job delivers once at next startup and advances to its next future slot. In-app deliveries stay in history for offline devices to see when they reconnect. Background Web Push and SMS provider delivery are not implemented.
- The tool loop can make several bounded planning and read steps. An approval resumes the proposed action and reports the result. Ask a follow-up for subsequent steps. Audit shows steps taken and outcomes without model chain-of-thought.
- Project file tools list directories and read selected text/PDF files. Document RAG searches the documents uploaded to BALAJI. Memory categories apply to saved notes, preferences and project notes. Chat, device, task and automation data remain independently managed.
