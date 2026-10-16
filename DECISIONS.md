# Decisions

## 1. Stack

### Python 3.14
I chose Python 3.14 because it is the latest stable version. It was released a year ago and has had
a year of bug-fix releases since. The newer version is still a beta, so I didn't use it. I also kept
the previous version, 3.13, supported, and CI tests both.

### uv, not pip
uv is written in Rust, so it is fast, and most importantly it can download Python by itself. It is one
tool that does the job of pip, venv and pipx together, and it locks every dependency with its
fingerprint (hash), so everyone gets exactly the same versions. Running the project takes a single
command. With pip you have to create a venv, activate it and install from `requirements.txt`, and even
then different machines can end up with different versions.

I would choose pip in a corporate setting where the IT security team doesn't allow downloading
binaries that haven't been scanned and approved, because uv downloads pre-built Python binaries from
the internet.

### Docker
I didn't really need Docker for this task, because SQLite and uv leave nothing much to containerise.
I only used it to deploy on Fly.io, which runs images, and I already have a Fly.io account.

### SQLite, not a Postgres server
I used SQLite because there isn't much data and there aren't many users, so I don't need the
concurrency a Postgres server provides.

This raises a question. Postgres is a server with its own authentication, but SQLite has no usernames
or passwords, so how do we make it secure? It is just a file on the server's private disk, so the
security comes from who can reach that file and what the program lets them do. Only the program can
access the file, and every way to the data needs a token, which decides what that person is allowed to
do. HTTPS is forced, so tokens and other sensitive information can't be read on a public network.

### FastAPI, not Flask or Django
I needed three things: long-lived SSE connections to keep pushing changes to the dashboard, the MCP
server mounted alongside the API, and strict validation. FastAPI is ASGI, so open streams don't tie up
workers, and the MCP SDK's ASGI app mounts into it in one line. It also has Pydantic validation
driven by Python type hints, dependency injection, security helpers for bearer tokens and OAuth2 (I use
its bearer-token helper; OAuth2 is there for when real sign-in is added), and automatic API docs.

I rejected Flask because it is WSGI, so each SSE client would tie up a worker thread. I rejected Django
because I don't need what it offers, like its ORM.

### Project layout
At a high level it is one program that does everything. I didn't build it as microservices, because
at this stage that would only add complexity, network calls and cost. Approvals also need to be
atomic: everything about a request being raised, approved or declined has to be saved together, which
is simple in one program with one database.

There are three entry points: the REST API in `api.py`, the MCP server in `mcp_server.py` and a CLI.
They are thin adapters that all call one service module, `service.py`, which holds every business rule
and runs each change in a single SQLite transaction together with an event row. Under that is a small
domain layer: the data types, a Money type in pence, the errors, and the plain-English explainer. Then
persistence: `db.py` for transactions and migrations, and `schema.sql`, where the database enforces the
same rules again with constraints and triggers. `stream.py` watches the events table and pushes changes
to the dashboard over SSE.

## 2. Live updates, and running it

### How updates reach the dashboard
I used Server-Sent Events (SSE).

- Every change (raise, approve, decline) is saved in one SQLite transaction together with a row in an
  `events` table. The event exists only if the change committed, so the wall can never show something
  that didn't happen, or miss something that did.
- A background task watches the events table. It is woken instantly when this process saves something,
  and it also checks every second, which catches changes made by the separate local MCP process writing
  to the same file.
- The dashboard keeps one connection open to `/api/events/stream`, and the server pushes each event
  down it.
- A new screen gets the full picture first (every waiting request, and up to the latest 1,000
  decisions), then live updates. It starts listening before it reads that snapshot, so nothing slips
  through the gap.
- Every event is numbered. After a dropped connection, the browser reconnects by itself and sends the
  last number it saw, and the server sends exactly what was missed.
- When an approval makes another waiting request out of date, that request is pushed again, marked
  stale.
- A heartbeat every 15 seconds lets the page notice a connection that died silently: if it hears
  nothing for 40 seconds, it reconnects. If the page has been disconnected for more than 10 seconds,
  it shows a red NOT LIVE banner, instead of showing old data as if it were live.
- On shutdown the server closes the streams, so a restart takes a fraction of a second, and the screens
  reconnect and catch up by themselves.

### What the alternatives would have cost
- **WebSockets:** two-way, which the wall doesn't need. There is no built-in way to catch up after a
  drop, so I'd have had to build my own "what did I miss" protocol. They also need extra setup on
  proxies.
- **Polling** (the page asks every few seconds): either updates arrive late, or every screen hits the
  database every second. That isn't "live".
- **Postgres LISTEN/NOTIFY on its own:** a notification sent while a screen is disconnected is lost, so
  I'd still need the events table. It also needs Postgres.
- **Redis pub/sub:** another service to run, and it also forgets anything sent while a screen is
  offline.
- **A paid push service** (Pusher, Ably): it costs money, and pay data leaves the company.
- **Keeping events only in memory:** everything is lost on restart, and changes made by the other
  process are never seen.

### Running it on one Linux box
1. systemd keeps the process running and restarts it if it crashes.
2. Caddy in front gives HTTPS, renewing the TLS certificates automatically, and passes the live stream
   through without buffering.
3. The SQLite file sits on the local disk, because its locking isn't safe on network drives.
4. Litestream streams changes to off-site storage, and I'd rehearse restores.
5. The open-file limit is raised, because every wall screen is an open connection. Logs go to journald,
   and an uptime checker watches `/healthz`.
6. A deploy is a quick restart; the screens reconnect and replay what they missed.
7. The live demo is the same shape on Fly.io: one machine with a persistent disk, because SQLite means
   one writer.

### What breaks first as it gets busier
- Not the writes; that's a few dozen a day.
- First, availability: it is one machine, so it needs backups and a standby.
- Then open connections: first the open-file limit, then one CPU core sending each event to every
  screen.
- Going past one machine means Postgres, and three things to handle. Postgres can commit events out of
  id order, so the feed needs care. MCP sessions need sticky routing. And the MCP confirmation's retry
  data needs a key shared across machines.

## 3. The approve tool

Yes, the `approve_request` tool exists, but only a human can finish an approval. The confirmation is a
pop-up built with MCP elicitation, so the AI can't approve anything on its own.

### Why the AI must not approve by itself
The maker-checker rule exists so that the person who wants the money is not the person who releases
it. The assistant reads each request's reason, which is free text written by the requester, and a
requester can write anything there to mislead or manipulate the AI. If the model could approve by
itself, the requester would in effect have approved their own request through the AI, and the
maker-checker rule simply falls apart.

### What I rejected
An approval or verification code sent by email or SMS. It would work, but it is heavy for this task.

### How it works
1. The approver writes "approve request #4" in Claude Code.
2. The AI decides to use the `approve_request` tool from the MCP server.
3. Claude Code sends an HTTPS request to `https://rate-desk-tag.fly.dev/mcp`.
4. The MCP server checks that the request came to the right address, that the token is valid, and
   whose token it is.
5. The server checks the rules: can this app show a confirmation dialog, is this person really an
   approver, did they raise this request themselves, is it already decided, is it stale? If any check
   fails, it stops with a clear message, and no dialog is shown.
6. The server writes the confirmation text itself and sends it to Claude Code, which shows it as a
   dialog to the approver. This is MCP elicitation. The AI doesn't see this dialog and can't answer it.
7. Only if the approver accepts does the approval go through, and the dashboard updates through the
   live stream.

## 4. Gaps

### What I did not build
- **Real sign-in:** company login (SSO, and OAuth so a remote Claude can sign in), token expiry, and a
  command to revoke or rotate a token. Today tokens never expire, and the demo tokens are public on
  purpose.
- **Rate limiting,** so nobody can hammer the API or guess tokens.
- **Effective dates:** a change applies the moment it is approved, as the brief says. Payroll usually
  wants "from the start of next pay week", and sometimes backdating.
- **Charge rate beside pay rate:** TAG's margin is the gap between what the client pays and what the
  operative gets. An approver should see what a rise does to that margin.
- **Approval thresholds:** for example, a second approver or a director for rises over 10%.
- **Requester extras:** withdrawing your own request, and being notified when it's declined. Today you
  only see the reason if you look.
- **Duplicate protection:** if a raise request is sent twice (say, a retry on a bad connection), two
  requests are created. An idempotency key would stop that.
- **Paging:** request lists stop at 500, and the wall at the latest 1,000 decisions.
- **Admin screens:** projects and trades are added from the command line only.
- **Privacy mode for the wall:** showing counts without names and figures.
- **Tamper-evidence and backups:** an audit trail that would reveal tampering even by someone with
  server access (each event sealed with a fingerprint of the one before), and Litestream backups on the
  live demo.
- **Approval outside Claude Code:** Claude Desktop Chat can't show the confirmation dialog, so
  approvals are refused there, deliberately. MCP's "URL" confirmation mode would be the way to support
  it.
- **Load testing:** I haven't measured how many wall screens one server holds. The limits above are
  reasoned, not measured.

### What was unclear or wrong in the brief
1. Multiple requests for the same rate aren't covered. I allow all of them, and once one is approved,
   the others go stale. The alternative, one waiting request per rate, is simpler but lets one person
   block another.
2. "Who asked" as a field doesn't fit "somebody else approves". If the requester's name is just a
   field, anyone can type someone else's name, so I take identity from the token instead.
