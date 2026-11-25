# The card format

Every card is a to-do item: a **title**, an optional **due date**, and a **description**
that holds everything else as plain text. The panel, the to-do lists on your phone, voice
and any AI session all read and write the same text, so a card edited anywhere stays one card.

## The lean card

```
LANE: need
OWNER: me
TOOLBOX: Electrical tools out
BUY FIRST: Hardware store run
EFFORT: 1h
COST: $25
BLOCKED BY: 3f2a9c1e, 2026-12-01
PARENT: 9b7d4e20
TAGS: projects
REPEAT: every 6 months
NEXT STEP: Turn the breaker off and swap the switch.

Free-text details: anything worth keeping.

PROGRESS:
2026-11-24 — Bought the switch.
2026-11-20 — Created.
```

- **Header lines** come first, one per line, in this order. Every one is optional except
  `NEXT STEP`, which should be one plain sentence.
- **Details** are free text after a blank line.
- **PROGRESS** is an append-only log of dated notes (`YYYY-MM-DD — text`). Notes are never rewritten.
- Titles are short and verb-first ("Replace the hallway switch"). An appointment reads
  `Dec 03 @ 9:30 AM: Dentist (cleaning)`.

## Lanes

| Lane | Meaning |
|---|---|
| `urgent` | An undated safety, leak, electrical or money risk that must stay in view until done. A deadline is never a reason for urgent: set a due date and the card rises on its own. |
| `work` | Work. Only on a branch marked as a work branch; an untagged card there counts as work. |
| `need` | Chores and obligations. |
| `want` | Nice-to-haves. |

## Whose move

`OWNER: me` (you) or `OWNER: claude` (the AI assistant). Without an `OWNER` line the board
decides: a next step addressed to you ("You …", your name, "Decide …"), a hands-on toolbox,
a trade visit or something to buy first is yours; on a branch marked *the assistant can
usually do this work* the rest is the assistant's. Your name, set under Configure, is accepted
as `OWNER: <name>`.

## Toolbox

Batch work by the setup it needs: `Electrical tools out`, `Plumbing tools out`, `DIY session`,
`Attic`, `Exterior`, `Car`, `Network closet`, `On-site`, `Physical checks`,
`Portals & follow-ups`, `Session` (at a computer), or `<Trade> visit` (e.g. `Plumber visit`).
`BUY FIRST: Hardware store run | Online order` marks a part to buy before you can start.

## Effort and cost

`EFFORT` is hands-on time to finish: `15m`, `30m`, `1h`, `1.5h`, `1d` (a day is 8 hours).
`COST` is what must be bought, hardware plus paid services: `$45`. Leave it out when nothing
has to be bought.

## Waiting: BLOCKED BY

A list of blocks, comma-separated. Three kinds:

- a **card uid** (8 characters) — clears itself when that card closes;
- a **date** (`2026-12-01`) — clears itself when the day passes;
- **text** — an event only a person can confirm; cleared from the card panel.
  `order A-1001` clears itself when that order is marked delivered (package module).

When the last block clears, the card gets an `unblocked:` note and shows up as *came free*.

## Tags

`TAGS: projects, home` — the other branches a card also belongs to. The card is stored once,
in its own branch; tagged branch pages show it too. A branch's key or its name works.

## Repeat

`REPEAT: every 6 months` (or `every N days|weeks|months|years`, optionally `from close`).
Closing a repeating card reopens it with the next due date.

## Today rank

What to do now: open, unblocked cards ranked **overdue → due today → urgent with no date →
tomorrow → this week**, ties broken by lane and then the smaller effort. The hub's *Today*
block, the branch tables' *Due* column, the morning push and `todo.mission_today` all use
this one rule.

## Daily pushes

`mission_control.digest` builds two texts (see `packages/mission_control.yaml`):

- **Morning plan:** today's appointments (if a calendar is set), up to three things for you
  (due today by smallest effort → overdue on its 1st/3rd/7th/14th/21st day late and every
  30 days after → undated urgent → soonest this week), tomorrow, what came free, and what
  the assistant could take.
- **Evening wrap:** what was closed and what moved today (from the store's own history, so
  phone check-offs count), then tomorrow.

The panel's **Digest** page (`/mission-control?page=digest`) is the full live picture.

## Reading view

Branch pages group rows by whose move it is (yours, the assistant's, waiting), show the last
note a person wrote, name a blocking card by its title, and pin overdue cards on top. The card
panel shows the first sentence of a next step until expanded — keep the next step to one
sentence and put any plan in the details.

## Work hours

The *Auto* mode brings work cards forward on weekdays 08:00–17:00 and leaves them out of the
way otherwise. Override it with *Work* / *Home* in the sidebar. An
`input_boolean.work_hours_boost_enabled` helper, if you create one, switches the boost off
when it is off.
