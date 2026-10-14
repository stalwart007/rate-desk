# Rate Desk

A pay rate at TAG is never changed directly. Somebody **raises** a change, somebody **else**
**approves** it, and only then does it apply. Rate Desk holds those requests, puts them on a
live wall dashboard, and gives the approver's AI assistant an MCP server to work with.

![The wall dashboard](docs/dashboard.png)

* **REST API**: raise, list, approve, decline, read rates in force (now or at any past moment).
* **Wall dashboard**: requests appear and get decided live, with no refresh. It reconnects and catches up by itself.
* **MCP server**: find waiting requests, explain one in plain words, approve or decline with the human's confirmation.
* **[DECISIONS.md](DECISIONS.md)**: what I chose, what I rejected, and why.

## Run it

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). It fetches the right
Python by itself.

```bash
uv run rate-desk serve --demo
```

That creates `rate-desk.db`, loads demo data, and serves on <http://127.0.0.1:8000>:

| | |
|---|---|
| Wall dashboard | <http://127.0.0.1:8000/#token=rd_demo_wall> |
| API docs (try requests in the browser) | <http://127.0.0.1:8000/docs> |
| MCP endpoint | `http://127.0.0.1:8000/mcp` |

### Demo people

Every call is made as somebody. The token says who, and what they may do.

| Token | Person | May raise | May approve |
|---|---|:-:|:-:|
| `rd_demo_dave` | Dave Okafor, site manager | ✓ | |
| `rd_demo_sam` | Sam Whitfield, contracts manager | ✓ | |
| `rd_demo_priya` | Priya Shah, commercial director | ✓ | ✓ |
| `rd_demo_tom` | Tom Reid, operations director | | ✓ |
| `rd_demo_wall` | The wall display | | |

These tokens are public on purpose, for the demo. For real users, `rate-desk add-user "Name" --can-approve`
prints a random token once and stores only its hash.

### Try it from a terminal

Keep the dashboard open while you do this and watch it change.

```bash
# What is waiting?
curl -s localhost:8000/api/requests -H "Authorization: Bearer rd_demo_tom"

# Dave asks for a raise for plant operators on the A2 job
curl -s localhost:8000/api/requests -H "Authorization: Bearer rd_demo_dave" \
  -H "Content-Type: application/json" \
  -d '{"project":"a2-junction","trade":"plant-operator","current_rate":"22.50",
       "requested_rate":"24.00","reason":"CPCS-carded operators for the embankment."}'

# Tom approves it (use the id from the last response). Dave could not; nor could Priya if she had raised it.
curl -s -X POST localhost:8000/api/requests/7/approve -H "Authorization: Bearer rd_demo_tom"

# What did that approval do? (the "outcome" block), and in plain words:
curl -s localhost:8000/api/requests/7 -H "Authorization: Bearer rd_demo_tom"
curl -s localhost:8000/api/requests/7/explanation -H "Authorization: Bearer rd_demo_tom"

# Rates in force now (add ?as_of=<time with timezone> for any past moment),
# and every rate plant operators have had on that job, each with the request that set it
curl -s localhost:8000/api/projects/a2-junction/rates -H "Authorization: Bearer rd_demo_tom"
curl -s localhost:8000/api/projects/a2-junction/trades/plant-operator/history -H "Authorization: Bearer rd_demo_tom"

# Decline, with a reason Sam will see
curl -s -X POST localhost:8000/api/requests/4/decline -H "Authorization: Bearer rd_demo_tom" \
  -H "Content-Type: application/json" -d '{"reason":"Attach the client letter, then raise again."}'
```

## Point Claude at it

The MCP server acts as one person: whoever owns the token you give it. Use an approver
(`rd_demo_tom` or `rd_demo_priya`).

**Claude Code, over HTTP** (with `rate-desk serve` running):

```bash
claude mcp add --transport http rate-desk http://127.0.0.1:8000/mcp --header "Authorization: Bearer rd_demo_tom"
```

**Claude Code, over stdio** (Claude Code starts the server itself; it shares the same database file,
so the wall still updates if `serve` is running too):

```bash
claude mcp add rate-desk --env RATE_DESK_TOKEN=rd_demo_tom --env RATE_DESK_DB="$PWD/rate-desk.db" -- uv run --directory "$PWD" rate-desk mcp
```

**Claude Desktop**: add this to `~/Library/Application Support/Claude/claude_desktop_config.json`,
with absolute paths (`which uv` gives the first), then restart Desktop:

```json
{
  "mcpServers": {
    "rate-desk": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--directory", "/path/to/rate-desk", "rate-desk", "mcp"],
      "env": {
        "RATE_DESK_TOKEN": "rd_demo_tom",
        "RATE_DESK_DB": "/path/to/rate-desk/rate-desk.db"
      }
    }
  }
}
```

Then ask things like:

* *"What rate changes are waiting?"*
* *"Explain request 3."*
* *"Approve request 3."* Rate Desk then asks **you** to confirm, in a dialog the model cannot see or answer.
* *"Deal with request 6."* Its reason contains a planted instruction telling the assistant to approve
  it without asking. It still cannot get past your confirmation.

> **Approving needs a client that can show that confirmation (MCP elicitation)**, such as Claude Code.
> Claude Desktop cannot, so there `approve_request` and `decline_request` refuse and say why; the
> reading tools work everywhere. That is deliberate. See [DECISIONS.md](DECISIONS.md), part 3.

| Tool | Does |
|---|---|
| `list_pending_requests` | What is waiting, oldest first, each flagged if stale |
| `explain_request` | One request in plain words: what changes, by how much, who asked, and what it became if decided |
| `get_rates_in_force` | Every trade's rate on a project |
| `list_projects` | Project codes |
| `approve_request` | Approves one request **after the human confirms** |
| `decline_request` | Declines one request with a reason **after the human confirms it** |

## Hosted demo

Running at **<https://rate-desk-tag.fly.dev>**: one machine in London, with the demo data and
the public demo tokens above. Anyone with the link can use it, and it may be reset.

| | |
|---|---|
| Wall dashboard | <https://rate-desk-tag.fly.dev/#token=rd_demo_wall> |
| API docs | <https://rate-desk-tag.fly.dev/docs> |

Point your own Claude Code at it, with nothing to install:

```bash
claude mcp add --transport http rate-desk https://rate-desk-tag.fly.dev/mcp --header "Authorization: Bearer rd_demo_tom"
```

Start `claude` and ask *"What rate changes are waiting?"*. Keep the wall open in a browser while you
approve something. To make new requests to play with, use the `curl` examples above with
`https://rate-desk-tag.fly.dev` in place of `localhost:8000`.

## API

All endpoints need `Authorization: Bearer <token>`. Money is always a string of pounds (`"24.00"`),
never a JSON number. Full schemas are at `/docs`.

| Method | Path | |
|---|---|---|
| `POST` | `/api/requests` | Raise: `project`, `trade`, `current_rate`, `requested_rate`, `reason`. Who asked comes from the token |
| `GET` | `/api/requests?status=pending\|approved\|declined\|all&project=&mine=` | List (pending oldest first) |
| `GET` | `/api/requests/{id}` | One request; `outcome` records what an approval did |
| `GET` | `/api/requests/{id}/explanation` | The same in plain words |
| `POST` | `/api/requests/{id}/approve` | Approve (not your own) |
| `POST` | `/api/requests/{id}/decline` | Decline with `reason` (not your own) |
| `GET` | `/api/projects/{code}/rates?as_of=` | Rates in force, now or at a past moment |
| `GET` | `/api/projects/{code}/trades/{trade}/history` | Every rate a trade has had, and which request set it |
| `GET` | `/api/events/stream` | Server-Sent Events for the dashboard (`?access_token=` for browsers) |

Every refusal is `{"code", "message"}`: 401 no or bad token, 403 not allowed or your own request,
404 unknown, 409 already decided / stale / wrong "rate now", 422 invalid input.

The stream sends `snapshot` on connect, then `request.raised`, `request.approved`,
`request.declined`, and `request.updated` (a waiting request went stale because another approval
moved the rate). Each carries the request as `GET /api/requests/{id}` would return it at that
moment. A `heartbeat` every 15 s lets the page notice a connection that died silently.

## Checks

```bash
uv run pytest          # 157 tests: rules, races between threads and processes, DB constraints, API, live stream, MCP
uv run ruff check      # lint
uv run ruff format --check
uv run mypy            # strict type checking
```

CI runs all of these on Python 3.13 and 3.14.

## Layout

```
src/rate_desk/
  money.py        Money as integer pence; parsing, formatting, percentage
  domain.py       Request, Rate, Outcome, User: plain immutable records
  errors.py       Every refusal, each with a stable code
  schema.sql      Tables, constraints and triggers: the rules, again, in the database
  db.py           Connections and transactions (BEGIN IMMEDIATE for writes)
  service.py      All the rules, in one place. Everything else is an adapter over this
  explain.py      Plain-English descriptions, written by code, not a model
  schemas.py      The JSON shapes the API, the stream and MCP share
  api.py          REST, the live stream, the dashboard, and the mounted MCP endpoint
  stream.py       Tails the events table and fans out to dashboards
  mcp_server.py   MCP tools, and the guarded approve/decline
  cli.py          serve | mcp | seed-demo | add-user | add-project | add-trade | opening-rate
  seed.py         Demo data
  static/dashboard.html   The wall display; no build step
tests/            One file per concern
```

## Configuration

| Variable | Default | |
|---|---|---|
| `RATE_DESK_DB` | `./rate-desk.db` | SQLite file |
| `RATE_DESK_PUBLIC_URL` | (none) | e.g. `https://rate-desk-tag.fly.dev`. The MCP endpoint answers only to this host and localhost |
| `RATE_DESK_DEMO` | (off) | `1` loads demo data into an empty database on start |
| `RATE_DESK_TOKEN` | (none) | For `rate-desk mcp` (stdio): whose assistant this is |

## Deploying

The image in `Dockerfile` and the config in `fly.toml` run it on one Fly.io machine with a volume:

```bash
fly apps create rate-desk-tag
fly volumes create rate_desk_data --size 1 --region lhr -a rate-desk-tag
fly deploy -a rate-desk-tag --ha=false    # exactly one machine: SQLite has one writer
```

To reset the demo data, delete the database and restart; an empty database is re-seeded on start:

```bash
fly ssh console -a rate-desk-tag -C "sh -c 'rm -f /data/rate-desk.db*'"
fly machine list -a rate-desk-tag          # note the machine id
fly machine restart <machine-id> -a rate-desk-tag
```
