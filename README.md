# op — OpenProject from the command line

One file. No dependencies. Say names, not numbers.

```bash
op config set --project scrum                    # once
op new "Login button unresponsive" --type bug --assign me --due +1w
op ls --overdue
op close 43 44 45
```

## Setup

```bash
op setup
```

A guided wizard that walks through four steps:

1. **Instance** — checks that an OpenProject API actually answers at the URL
   before asking for anything else, so a typo fails immediately rather than
   as a confusing 401 later.
2. **Token** — offers to open `/my/access_token` in your browser, then
   verifies the token by fetching your account and showing who you are.
3. **Default project** — lists your projects and lets you pick one by number.
   Every `ls`, `new`, `stats` and `types` then uses it.
4. **Shell integration** — optionally adds this directory to your `PATH` and
   installs tab completion into your shell profile. Both are skippable and
   both are idempotent, so re-running is safe.

Unattended, for provisioning:

```bash
op setup --url https://openproject.example.com \
         --token "$OP_TOKEN" --project scrum --yes --skip-shell
```

`--yes` accepts each step's *default* rather than blindly answering yes, so an
unreachable URL still stops setup instead of being written to the config.

Re-run `op setup` any time to reconfigure; it offers to keep the existing
token. Credentials resolve as: flags → `OP_URL` / `OP_TOKEN` →
`~/.openproject/config.json` (written `0600`).

## What makes it easy

**Names anywhere an id works.** `--project scrum`, `--type bug`,
`--status "in progress"`, `--assign me`. Case-insensitive; accepts exact names,
unique prefixes, or unique substrings, so `"in progres"` finds *In progress*.
Ambiguity lists the candidates rather than guessing.

**A default project.** Set it once; every `ls`, `new`, `stats`, and `types`
picks it up. Escape it any time with `--project all` or `--all-projects`.

**Output fits the audience.** Terminal → table with colour (overdue dates red,
closed statuses dim). Pipe → JSON. No flag required.

```bash
op ls                        # table
op ls | jq '.[].subject'     # JSON, automatically
op ls --columns id,subject,dueDate
```

**Batches.** Every id argument takes several:

```bash
op close 43 44 45
op assign 43 44 --to sean
op comment 43 44 --text "Triaged"
op rm 43 44 45 --yes
```

**Lookups are cached** for 10 minutes in `~/.openproject/cache.json`. Name
resolution used to cost an extra request each time; now a filtered `ls` is a
single call. That matters — this server saturates near 4 requests/second.
Bypass with `--no-cache`, reset with `op cache clear`.

**Friendly dates and durations.** `--due +1w`, `--due tomorrow`,
`--on yesterday`, `2026-09-01`. Time as `2`, `2h`, `90m`, or `1h30m`.

**Errors name the valid answers.**

```
$ op new "x" --project demo --type bug
op: no type matches 'bug'
    Available: Milestone, Summary task, Task
```

Checked locally, before the request. 401 says re-run `op init`; 403 on time
tracking explains the module is off for that project.

**Transient failures retry** (429 and 5xx) with backoff.

**`lockVersion` is invisible** — edits re-read first, so optimistic locking
never reaches you.

## Commands

```bash
# look
op ls                          op ls --overdue
op ls --unassigned --type bug  op ls --due-before +3d
op mine                        op search "login"
op show 15                     op show 15 --no-comments
op stats                       op web 15

# change  (ids accept several)
op new "Fix login" --type bug --assign me --due +1w -d "details..."
op new "From a file" -d -  < notes.md
op edit 15 --status "in progress" --percent 50
op assign 15 sean              op assign 1 2 3 --to sean
op close 15                    op reopen 15
op comment 15 "Deployed"
op log 15 2h "pairing"         op time 15
op attach 15 ./shot.png        op files 15
op history 15                  op rm 15

# look up
op projects  op types  op statuses  op priorities  op users
op config    op cache clear
```

## Escape hatch

```bash
op raw GET  /projects/1/types
op raw POST /time_entries --data @body.json
op raw POST /work_packages --data @-   < payload.json
```

## Exit codes

`0` ok · `1` usage or name resolution · `2` HTTP error · `3` host unreachable

Data goes to stdout, everything else to stderr, so `op ls > out.json` is clean.

## OpenProject gotchas this handles for you

**Work package types are enabled per project.** Creating a Bug in a project
where the Bug type is not enabled fails with an unhelpful
`Type is not set to one of the allowed values`. `op` resolves the type against
that project first and tells you which types are actually available.

**Time tracking rides on the "Time and costs" module**, which is enabled per
project — and administrator rights do *not* bypass a disabled module, so
`POST /time_entries` returns a bare 403. `op log` explains that instead.

**The time entry API is not shaped like the rest.** Its work package link is
called `entity`, not `workPackage`; `user` is required; `activity` has a server
side default; and there is no `/time_entries/activities` endpoint. The
collection also has no server-side work package filter, so `op time <id>`
narrows client-side.

**`offset` is a 1-based page number**, not a record offset. Paging is handled
internally; this only matters if you use `op raw`.

**Omitting the `filters` parameter is not the same as sending an empty one.**
Leave it out and the server quietly applies an "open only" default, so
`--status all` would hide closed records. `op` always sends the parameter.

**Updates need a `lockVersion`.** `op` re-reads the record first, at the cost
of one extra request per write.

## Performance

OpenProject work package queries are expensive. On a modest 2 vCPU instance they
saturate the CPU at roughly 4 requests per second, and OpenProject applies no
rate limiting of its own, so a parallel loop can degrade the site for everyone.
`op` caches lookup tables for 10 minutes to keep most commands to a single
request; prefer `--limit` and server-side filters over fetching everything.

## Requirements

Python 3.8 or newer. Nothing else. Tested against OpenProject 17;
should work with 10 and later.

## License

[MIT](LICENSE)
