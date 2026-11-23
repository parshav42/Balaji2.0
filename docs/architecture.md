# BALAJI 2.0 architecture

~~~mermaid
flowchart TD
  Phone[Paired phone PWA] --> Serve[Private Tailscale Serve HTTPS]
  Serve --> API[FastAPI on laptop loopback]
  Laptop[Laptop browser] --> API
  API --> Agent[Ollama streaming agent]
  API --> Voice[Whisper.cpp and Piper]
  API --> Live[Authenticated SSE snapshots]
  Agent --> Registry[Schema-validated tool registry]
  Registry --> Gate{Sensitive action?}
  Gate -->|Yes| Approval[SQLite approval and audit]
  Gate -->|Read or local draft| Action[Bounded tool execution]
  Approval --> Action
  Action --> Files[Allowlisted Linux project roots]
  Action --> Gmail[OAuth-authenticated Gmail API]
  Action --> DB[(SQLite)]
  Jobs[Persistent scheduler] --> DB
  DB --> Live
~~~

- Access uses one-use enrollment codes, owner review, and hashed opaque browser credentials. Remote cookies are Secure and HttpOnly. State-changing authenticated requests must match the configured origin. Device sessions are checked again on each SSE snapshot.
- Runs transition through `RECEIVED`, `PLANNING`, `EXECUTING`, `WAITING_FOR_APPROVAL`, `RESUMING` and a terminal state. Chat idempotency keys identify submissions. SQLite claims approvals atomically before executing tools. Uncertain results stay recorded for review.
- The desktop tool registry uses fixed executables and structured arguments. Project reads and writes use directory descriptors with symlinks disabled. New files use exclusive creates.
- Gmail OAuth state is single-use and tied to the owner session. Credentials live outside SQLite in a private file. Drafts live separately from memory. Outbound email requires an approval of the exact recipient, subject, and body.
- The scheduler records delivery and advances recurring jobs together in SQLite. The PWA caches only the public app shell. Device sessions, user data and actions use the live laptop.
