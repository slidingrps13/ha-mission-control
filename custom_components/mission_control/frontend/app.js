/* =============================================================================
 * Mission Control — application module
 *
 * Loaded by mission-control.js (the panel the integration registers) with a
 * cache-busting query. Branches, your name and the calendar come from the
 * integration (WS mission_control/config) at boot.
 *
 * Parsing + grouping rules are the contract in docs/CARD_FORMAT.md — do not drift.
 * Writes obey the board's guardrails (append-only PROGRESS,
 * tags on their own line, never delete, work-lane approval on non-work branches).
 * Every mutating call funnels through write(); destructive paths ask first.
 *
 * localStorage 'mc.writes' = 'off' is the per-browser kill switch.
 * =========================================================================== */

let WRITES_ENABLED = true;
try { if (localStorage.getItem('mc.writes') === 'off') WRITES_ENABLED = false; } catch (e) { /* private mode */ }

/* --------------------------------------------------------------------------
 * 1. Configuration
 * ------------------------------------------------------------------------ */

/* Local calendar date (YYYY-MM-DD) — toISOString() is UTC and stamps evening notes with tomorrow's date. */
const ymd = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
/* Branches, your name and the calendar come from the integration at boot
   (WS mission_control/config) — applyConfig() fills these in place. */
const BRANCHES = [];
const BY_KEY = {};
const ent = (k) => `todo.${k}_tracker`;

/* Trackers where an untagged item is implicitly LANE: work (see docs/CARD_FORMAT.md (Lanes)). */
const WORK_TRACKERS = new Set();

/* Branches where LANE: work needs your explicit OK (every non-work branch, guardrail 1). */
const WORK_LANE_NEEDS_APPROVAL = new Set();

/* Branches where the AI assistant can usually finish desk work alone. */
const CLAUDE_TRACKERS = new Set();

/* Quick-add branch guesses, [key, RegExp] from each branch's keywords. */
const BRANCH_GUESS = [];

/* Your own name: accepted as OWNER: <name> and at the start of a next step. */
let PERSON = '';

/* External sources a review can stamp (input_datetime.sync_<id>); none by default. */
const SYNC_SOURCES = [];

let CALENDAR = null;
let MC_CONFIG = {};
const reEsc = (x) => String(x).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function applyConfig(cfg) {
  MC_CONFIG = cfg || {};
  const list = (cfg && cfg.branches) || [];
  BRANCHES.splice(0, BRANCHES.length, ...list.map((b) => ({
    key: b.key, label: b.name || b.key, color: b.color || '#8b9098',
    icon: b.icon || 'mdi:folder-outline', group: b.work ? 'work' : 'home',
  })));
  for (const k of Object.keys(BY_KEY)) delete BY_KEY[k];
  for (const b of BRANCHES) BY_KEY[b.key] = b;
  WORK_TRACKERS.clear(); WORK_LANE_NEEDS_APPROVAL.clear(); CLAUDE_TRACKERS.clear(); BRANCH_GUESS.length = 0;
  for (const k of Object.keys(TAG_ALIASES)) delete TAG_ALIASES[k];
  for (const b of list) {
    (b.work ? WORK_TRACKERS : WORK_LANE_NEEDS_APPROVAL).add(b.key);
    if (b.assistant) CLAUDE_TRACKERS.add(b.key);
    const words = String(b.keywords || '').split(',').map((w) => w.trim().toLowerCase()).filter(Boolean);
    if (words.length) BRANCH_GUESS.push([b.key, new RegExp('\\b(' + words.map(reEsc).join('|') + ')\\b')]);
    for (const a of [b.key, b.key.replace(/_/g, ' '), String(b.name || '').trim().toLowerCase()]) {
      const k = a.replace(/-/g, '_');
      if (k && !TAG_ALIASES[k]) TAG_ALIASES[k] = b.key;
    }
  }
  PERSON = String((cfg && cfg.person_name) || '').trim();
  CALENDAR = (cfg && cfg.calendar) || null;
}

/* OWNER values: "me" (you) or "claude" (the AI assistant). Your name counts as "me". */
function ownerKey(v) {
  const x = String(v || '').trim().toLowerCase();
  if (x === 'me' || x === 'you' || (PERSON && x === PERSON.toLowerCase())) return 'me';
  if (x === 'claude' || x === 'ai') return 'claude';
  return '';
}

const WORK_BOOST_TOGGLE = 'input_boolean.work_hours_boost_enabled';

const LANE_COLORS = { work: '#5aa9ff', need: '#f0b35a', want: '#5cc98b', urgent: '#ff6b6b' };
const LANE_LABELS = { work: 'WORK', need: 'NEED', want: 'WANT', urgent: 'URGENT' };

/* Toolbox axis — batch by setup already paid.
   Ordered by setup cost: the expensive, rare setups first; Session (zero setup) last.
   Source of truth is a line-anchored `TOOLBOX:` / `BUY FIRST:` in the item
   description; toolbox.json holds the proposed placements until those are written. */
const TOOLBOXES = [
  { k: 'Electrical tools out', icon: 'mdi:flash-outline',          color: '#f0b35a' },
  { k: 'Plumbing tools out',   icon: 'mdi:pipe-wrench',            color: '#5aa9ff' },
  { k: 'DIY session',          icon: 'mdi:hammer-screwdriver',     color: '#ff9f43' },
  { k: 'Attic',                icon: 'mdi:home-roof',              color: '#c6d24a' },
  { k: 'Exterior',             icon: 'mdi:home-outline',           color: '#66bb6a' },
  { k: 'Car',                  icon: 'mdi:car-wrench',             color: '#c6d24a' },
  { k: 'Network closet',       icon: 'mdi:server-network',         color: '#3fc9d6' },
  { k: 'Visits',               icon: 'mdi:account-hard-hat',       color: '#b07cf0', visit: true },
  { k: 'On-site',              icon: 'mdi:map-marker-outline',     color: '#ff6b6b' },
  { k: 'Physical checks',      icon: 'mdi:walk',                   color: '#f06292' },
  { k: 'Portals & follow-ups', icon: 'mdi:phone-outline',          color: '#9aa0a8' },
  { k: 'Session',              icon: 'mdi:laptop',                 color: '#8b9098' },
];
const BUY_RUNS = [
  { k: 'Hardware store run', icon: 'mdi:cart-outline',       color: '#f0b35a' },
  { k: 'Online order',   icon: 'mdi:package-variant-closed', color: '#f0b35a' },
];
const TOOLBOX_VALUES = TOOLBOXES.filter((d) => !d.visit).map((d) => d.k);

/* ---- lean-card lint (docs/CARD_FORMAT.md (The lean card)) ----
   Flags cards that drift from the lean card. Read-only: it never rewrites. */
const LANES_OK = new Set(['work', 'need', 'want', 'urgent']);
const RETIRED_RE = /^(INITIATION[ _]PROMPT|GOAL|PRIORITY|SCOPE|FANTASY_TITLE|SHORT_TITLE|DEPENDS ON|SDP_STATUS|SDP_PRIORITY|REQUESTER|DUE|LAST_SDP_SYNC|INTERACTION_STATE|LAST_INTERACTION_DATE)\s*:/i;
const HEAD_START_RE = /^(LANE|OWNER|TOOLBOX|BUY FIRST|EFFORT|COST|BLOCKED BY|PARENT|TAGS|REPEAT|REFS|SDP_ID|SOURCE|PIN|NEXT STEP|PROGRESS):/i;
/* REPEAT: every N days|weeks|months|years[, from close] — closing reopens the card with the next due . */
/* EFFORT / COST (2026-10-11, docs/CARD_FORMAT.md (Effort and cost)): hands-on time and money to finish the card.
   EFFORT: 15m | 2h | 1.5h | 1d (a day = 8h).  COST: $45 (hardware + paid services; omit when nothing to buy). */
const EFFORT_RE = /^\s*(\d+(?:\.\d+)?)\s*(m|min|mins|h|hr|hrs|d|day|days)\s*$/i;
function effortMin(v) {
  const m = EFFORT_RE.exec(v || '');
  if (!m) return null;
  const n = +m[1], u = m[2][0].toLowerCase();
  return Math.round(u === 'm' ? n : u === 'h' ? n * 60 : n * 480);
}
const COST_RE = /^\s*~?\$\s*(\d[\d,]*(?:\.\d+)?)\s*$/;
function costNum(v) { const m = COST_RE.exec(v || ''); return m ? +m[1].replace(/,/g, '') : null; }
function fmtMin(n) { return n == null ? '' : n < 60 ? n + 'm' : n < 480 || n % 480 ? (Math.round(n / 6) / 10) + 'h' : (n / 480) + 'd'; }
const REPEAT_RE = /^\s*every\s+(?:(\d+)\s+)?(day|week|month|year)s?(?:\s*[,;]?\s*(from\s+(?:close|closing|done|completion)))?\s*$/i;
/* ---- branch tags (2026-10-07; docs/CARD_FORMAT.md (Tags)) ----
   `TAGS: home, projects` = the OTHER branches a card belongs to. Storage stays the home
   branch (one to-do list per card); the tagged branch pages just show it too. Mirrors
   codec.TAG_ALIASES in the integration: a branch's key, or its name. */
const TAG_ALIASES = {};  /* filled from the configured branches by applyConfig() */
function tagKeys(raw, home) {
  const out = [];
  for (const w of (raw || '').split(/[,;/]+/)) {
    const k = TAG_ALIASES[w.trim().toLowerCase().replace(/-/g, '_')];
    if (k && k !== home && !out.includes(k)) out.push(k);
  }
  return out;
}
function tagUnknown(raw) {
  return (raw || '').split(/[,;/]+/).map((w) => w.trim()).filter((w) => w && !TAG_ALIASES[w.toLowerCase().replace(/-/g, '_')]);
}
const HEAD_BURIED_RE = /\S.*\b(LANE|NEXT STEP|TOOLBOX|BLOCKED BY|PARENT):\s/;
function lintCard(i) {
  const d = i.description || '';
  const lines = d.split('\n').map((l) => l.trim());
  const out = [];
  const laneLine = lines.find((l) => /^LANE:/i.test(l));
  const lane = laneLine ? laneLine.replace(/^LANE:\s*/i, '').trim().toLowerCase() : '';
  if (!laneLine || !lane || /^\(?none\)?$/.test(lane)) out.push('No lane');
  else if (!LANES_OK.has(lane)) out.push(`Lane "${lane}" isn't a lane`);
  else if (lane === 'work' && !WORK_TRACKERS.has(i.branch)) out.push('Work lane on a home branch');
  if (!lines.some((l) => /^NEXT STEP:\s*\S/i.test(l))) out.push('No next step');
  const retired = [...new Set(lines.map((l) => (l.match(RETIRED_RE) || [])[1]).filter(Boolean).map((x) => x.toUpperCase()))];
  if (retired.length) out.push('Retired field: ' + retired.join(', '));
  if (/PROGRESS:\s*\d+\s*%/i.test(d)) out.push('Progress %');
  const tb = lines.find((l) => /^TOOLBOX:/i.test(l));
  if (tb) {
    const v = tb.replace(/^TOOLBOX:\s*/i, '').trim();
    if (v && v.toLowerCase() !== 'none' && !TOOLBOX_VALUES.includes(v) && !/ visit$/i.test(v)) out.push(`Unknown toolbox "${v}"`);
  }
  const ef = lines.find((l) => /^EFFORT:/i.test(l));
  if (!ef || !ef.replace(/^EFFORT:\s*/i, '').trim()) out.push('No effort estimate');
  else if (effortMin(ef.replace(/^EFFORT:\s*/i, '')) == null) out.push('Effort must read like 30m, 2h or 1d');
  const co = lines.find((l) => /^COST:/i.test(l));
  if (co && co.replace(/^COST:\s*/i, '').trim() && costNum(co.replace(/^COST:\s*/i, '')) == null) out.push('Cost must read like $45');
  const ow = lines.find((l) => /^OWNER:/i.test(l));
  if (ow && !ownerKey(ow.replace(/^OWNER:\s*/i, ''))) out.push('Owner must be me or claude');
  if ((i.rawSummary || i.summary || '').length > 80) out.push('Long title');
  const tg = lines.find((l) => /^TAGS:/i.test(l));
  if (tg) { const bad = tagUnknown(tg.replace(/^TAGS:\s*/i, '')); if (bad.length) out.push(`Unknown tag "${bad.join('", "')}"`); }
  const rp = lines.find((l) => /^REPEAT:/i.test(l));
  if (rp && !REPEAT_RE.test(rp.replace(/^REPEAT:\s*/i, ''))) out.push('Repeat must read "every N days/weeks/months/years", optionally "from close"');
  if (lines.some((l) => !HEAD_START_RE.test(l) && !NOTE_RE.test(l) && !/^Original title:/i.test(l) && HEAD_BURIED_RE.test(l))) out.push('Field not on its own line');
  const bl = lines.find((l) => /^BLOCKED BY:/i.test(l));
  if (bl && waitHardToCheck(bl.replace(/^BLOCKED BY:\s*/i, ''))) out.push("Wait can't be checked (name one card uid, date or event)");
  return out;
}

/* ---- who moves it next ----
   Explicit OWNER: wins. Otherwise: a next step addressed to you ("You …", your name) or starting
   "Decide" is yours; hands-on toolboxes, trade visits and things to buy are yours; on an assistant
   branch the rest is desk work Claude can take. */
const HANDS_ON = new Set(['Physical checks', 'On-site', 'Electrical tools out', 'Plumbing tools out', 'DIY session', 'Attic', 'Exterior', 'Car', 'Network closet', 'Portals & follow-ups']);
function ownerOf(i) {
  const o = ownerKey(i.p.owner);
  if (o) return o;
  const ns = i.p.nextStep || '';
  const first = ((ns.match(/^\s*([\w'-]+)/) || [])[1] || '').toLowerCase();
  if (first === 'you' || first === 'decide' || ownerKey(first) === 'me') return 'me';
  const tb = i.p.toolbox || '';
  if (HANDS_ON.has(tb) || / visit$/i.test(tb) || i.p.buyFirst) return 'me';
  if (CLAUDE_TRACKERS.has(i.branch)) return 'claude';
  return 'me';
}
function laneOf(i) { return i.p.lane || (WORK_TRACKERS.has(i.branch) ? 'work' : ''); }
const LANE_RANK = { urgent: 0, work: 1, need: 2, want: 3, '': 4 };

/* ---- calm hub (2026-09-24 remodel) ---- */
const SOMEDAY_DAYS = 60;
const LIVE_POLL_MS = 600000; /* fallback poll while todo/item/subscribe is live (2026-10-07) */
/* A branch review is due only when enough changed since the last one — not on a timer. */
const REVIEW_MIN_CHANGES = 5;
const CREATED_ONLY_RE = /^(created|card created|queued|not started)\b/i;
/* ---- branch-page readability (2026-11-24, docs/CARD_FORMAT.md (Reading view)) ---- */
/* System notes are written by the store or by old bot passes, not by a person or a working session. */
const SYSTEM_NOTE_RE = /^(unblocked:|done; next due|skipped this pass|created\b|card created|queued|not started|created as part of)/i;
function realNote(p) { return ((p && p.notes) || []).find((n) => !SYSTEM_NOTE_RE.test(n.text)) || null; }
/* Provenance lines from the 2026-09-24 lean migration and title standardization: kept, but folded in the drawer. */
const PROVENANCE_RE = /^(Migrated \d{4}-\d{2}-\d{2}:|From the old prompt:|Original title:|Previous title:)/i;
function splitProvenance(details) {
  const keep = [], prov = [];
  for (const l of (details || '').split('\n')) (PROVENANCE_RE.test(l.trim()) ? prov : keep).push(l);
  return { body: keep.join('\n').trim(), prov: prov.map((l) => l.trim()).filter(Boolean) };
}
const UID_TOKEN_G = /\b[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?\b/gi;
const UID_TOKEN_1 = /\b[0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?\b/i;
/* A text wait nobody can test: several waits in one, a long note, "nothing", or a card named in words. */
function waitHardToCheck(text) {
  const t = (text || '').replace(UID_TOKEN_G, '').replace(/\b\d{4}-\d{2}-\d{2}\b/g, '').replace(/\border\s+[\w-]+/gi, '')
    .replace(/\([^)]*\)/g, '').replace(/\s+/g, ' ').trim();
  if (!t || /^[\s,;.·—-]*$/.test(t)) return false;
  if (/^(nothing|none|n\/a|—)\b/i.test(t)) return true;
  if (t.split(';').map((x) => x.replace(/^[\s,.·—-]+|[\s,.·—-]+$/g, '')).filter(Boolean).length > 1 || /[.!?]\s+[A-Z]/.test(t)) return true;
  if (t.length > 100) return true;
  if (/\b(item|card)\b/i.test(t) && !UID_TOKEN_1.test(text)) return true;
  return false;
}
/* Card panel reading view (2026-11-24, docs/CARD_FORMAT.md (Reading view)): the first sentence or clause of a long next step. */
function firstStep(t) {
  const m = (t || '').match(/^(.+?)(?:[.!?](?=\s+[A-Z(])|;(?=\s))/s);
  return m && m[1].length < (t || '').trim().length - 2 ? m[1].replace(/[\s,]+$/, '') + (/[.!?]$/.test(m[1]) ? '' : '.') : (t || '');
}
function longStep(t) { return !!t && (firstStep(t) !== t || t.length > 200); }
const WHO_LABEL = { me: 'Your move', claude: "Claude's move", wait: 'Waiting', done: 'Done' };
const WHO_RANK = { me: 0, claude: 1, wait: 2, done: 3 };
const SETUPS = [
  { k: 'all',    label: 'All tasks',          m: () => true },
  { k: 'desk',   label: 'At my desk',         m: (i, s) => s.tbOf(i) === 'Session' },
  { k: 'calls',  label: 'Calls & follow-ups', m: (i, s) => s.tbOf(i) === 'Portals & follow-ups' },
  { k: 'walk',   label: 'Walking the house',  m: (i, s) => s.tbOf(i) === 'Physical checks' },
  { k: 'diy',    label: 'DIY session',        m: (i, s) => ['DIY session', 'Attic', 'Exterior'].includes(s.tbOf(i)) },
  { k: 'tools',  label: 'Tools out',          m: (i, s) => /tools out$/i.test(s.tbOf(i)) },
  { k: 'visit',  label: 'Trade visits',       m: (i, s) => / visit$/i.test(s.tbOf(i)) },
  { k: 'errand', label: 'Errands',            m: (i, s) => !!s.buyOf(i) },
  { k: 'closet', label: 'Network closet',     m: (i, s) => s.tbOf(i) === 'Network closet' },
  { k: 'onsite', label: 'On-site',            m: (i, s) => s.tbOf(i) === 'On-site' },
  { k: 'car',    label: 'Car',                m: (i, s) => s.tbOf(i) === 'Car' },
];

/* Epoch-default input_datetime helpers read as "never". */
const NEVER_BEFORE = Date.parse('2021-01-01T00:00:00');

const STOPWORDS = new Set([
  'the', 'and', 'for', 'with', 'from', 'into', 'this', 'that', 'over', 'onto', 'per',
  'add', 'new', 'fix', 'set', 'get', 'use', 'via', 'all', 'any', 'out', 'off', 'not',
  'has', 'are', 'was', 'were', 'you', 'your', 'our', 'its', 'but', 'can', 'will',
  'need', 'needs', 'make', 'made', 'run', 'ran', 'one', 'two', 'now', 'then', 'when',
]);

/* --------------------------------------------------------------------------
 * 2. Parsing — the docs/CARD_FORMAT.md contract, verbatim
 * ------------------------------------------------------------------------ */

const NOTE_RE = /^(\d{4}-\d{2}-\d{2})\s*[—-]\s*(.*)$/;

/* ---- search + sort (2026-09-24) ----
   Created time comes from the tracker uid: local_todo uids are UUID v1, which embed the creation
   timestamp (100 ns ticks since 1582-10-15). Last update = the newest dated PROGRESS note. */
function uuidCreated(uid) {
  const m = /^([0-9a-f]{8})-([0-9a-f]{4})-1([0-9a-f]{3})-/i.exec(uid || '');
  if (!m) return null;
  try {
    const t = (BigInt('0x' + m[3]) << 48n) | (BigInt('0x' + m[2]) << 32n) | BigInt('0x' + m[1]);
    return Number(t / 10000n) - 12219292800000;
  } catch (e) { return null; }
}
const SORTS = [
  ['default', 'Default order'], ['updated', 'Last update'], ['created_new', 'Newest first'],
  ['created_old', 'Oldest first'], ['title', 'Title A–Z'], ['branch', 'Branch'],
];
function sortItems(list, how) {
  const arr = list.slice();
  const cr = (i) => { if (i._created === undefined) i._created = uuidCreated(i.uid); return i._created; };
  const num = (v, missing) => (v == null ? missing : v);
  if (how === 'updated') arr.sort((a, b) => (b.p.lastNote || '').localeCompare(a.p.lastNote || '') || num(cr(b), 0) - num(cr(a), 0));
  else if (how === 'created_new') arr.sort((a, b) => num(cr(b), -Infinity) - num(cr(a), -Infinity));
  else if (how === 'created_old') arr.sort((a, b) => num(cr(a), Infinity) - num(cr(b), Infinity));
  else if (how === 'title') arr.sort((a, b) => a.summary.localeCompare(b.summary, undefined, { sensitivity: 'base' }));
  else if (how === 'branch') arr.sort((a, b) => a.branch.localeCompare(b.branch) || (b.p.lastNote || '').localeCompare(a.p.lastNote || ''));
  return arr;
}
function fmtCreated(ms) {
  if (ms == null) return '';
  const d = new Date(ms);
  const opt = d.getFullYear() === new Date().getFullYear() ? { month: 'short', day: 'numeric' } : { month: 'short', year: 'numeric' };
  return d.toLocaleDateString([], opt);
}

function parseDesc(desc) {
  const d = desc || '';

  const progressM = d.match(/PROGRESS:\s*(\d+)%/);
  const progressNum = progressM ? parseInt(progressM[1], 10) : 0;
  const notStarted = /PROGRESS:\s*Not started/i.test(d);

  const priM = d.match(/PRIORITY:\s*(high|medium|low)/i);
  const priority = priM ? priM[1].toLowerCase() : 'medium';
  const priNum = priority === 'high' ? 0 : priority === 'low' ? 2 : 1;

  const laneM = d.match(/LANE:\s*(\w+)/i);
  const lane = laneM ? laneM[1].toLowerCase() : '';

  const pinned = /(^|\n)\s*PIN:/i.test(d);

  const nextM = d.match(/NEXT STEP:\s*(.+?)(?:\n|DEPENDS|BLOCKED|INITIATION|PRIORITY|$)/s);
  const nextStep = nextM ? nextM[1].trim() : '';

  const depM = d.match(/DEPENDS ON:\s*(.+?)(?:\n|INITIATION|NEXT|PRIORITY|BLOCKED|$)/s);
  const depends = depM ? depM[1].trim() : '';

  const blkM = d.match(/BLOCKED BY:\s*(.+?)(?:\n(?=[A-Z])|$)/s);
  const blockedBy = blkM ? blkM[1].trim() : '';

  const hasBlocker = (depends.length > 0 && !/none|n\/a|—/i.test(depends)) || blockedBy.length > 0;

  const promptIdx = d.indexOf('INITIATION PROMPT:');
  const prompt = promptIdx !== -1 ? d.substring(promptIdx + 18).trim() : '';

  /* v2 additions — not parsed by the legacy cards */
  const goalM = d.match(/^GOAL:\s*(.+)$/mi);
  const goals = goalM ? goalM[1].split(',').map((s) => s.trim()).filter(Boolean) : [];

  const notes = [];
  for (const line of d.split('\n')) {
    const m = line.trim().match(NOTE_RE);
    if (m) notes.push({ date: m[1], text: m[2].trim() });
  }
  notes.sort((a, b) => (a.date < b.date ? 1 : -1));
  const lastNote = notes.length ? notes[0].date : null;

  const tbM = d.match(/^TOOLBOX:\s*(.+)$/mi);
  const toolbox = tbM ? tbM[1].trim() : '';
  const buyM = d.match(/^BUY FIRST:\s*(.+)$/mi);
  const buyFirst = buyM ? buyM[1].trim() : '';
  const efM = d.match(/^EFFORT:\s*(.+)$/mi);
  const effort = efM ? efM[1].trim() : '';
  const coM = d.match(/^COST:\s*(.+)$/mi);
  const cost = coM ? coM[1].trim() : '';
  const ownM = d.match(/^OWNER:\s*(\w+)/mi);
  const owner = ownM ? ownM[1].toLowerCase() : '';
  const refM = d.match(/^REFS:\s*(.+)$/mi);
  const refs = refM ? refM[1].split(/\s*[;,]\s*/).map((x) => x.trim()).filter(Boolean) : [];
  const tagM = d.match(/^TAGS:\s*(.+)$/mi);
  const tagsRaw = tagM ? tagM[1].trim() : '';
  const rpM = d.match(/^REPEAT:\s*(.+)$/mi);
  const repeat = rpM ? rpM[1].trim() : '';
  const tags = tagKeys(tagsRaw, null); /* the card's own branch is filtered where it is used */

  /* Free-text body: everything that is not a header field or a dated note (lean card 2026-09-24). */
  const HEADER_RE = /^(LANE|OWNER|TOOLBOX|BUY FIRST|EFFORT|COST|BLOCKED BY|DEPENDS ON|PARENT|TAGS|REPEAT|REFS|SDP_ID|SOURCE|PIN|NEXT STEP|PROGRESS|GOAL|PRIORITY):/i;
  const details = d.split('\n').filter((l) => !HEADER_RE.test(l.trim()) && !NOTE_RE.test(l.trim())).join('\n').trim();

  return {
    details,
    progressNum, notStarted, priority, priNum, lane, pinned,
    nextStep, depends, blockedBy, hasBlocker, prompt, goals, notes, lastNote,
    toolbox, buyFirst, owner, refs, tags, tagsRaw, repeat,
    effort, effortMin: effortMin(effort), cost, costNum: costNum(cost),
  };
}

function daysSince(isoDate) {
  if (!isoDate) return null;
  const t = Date.parse(isoDate + 'T00:00:00');
  if (isNaN(t)) return null;
  return Math.floor((Date.now() - t) / 86400000);
}

function cleanSummary(s) {
  return (s || '').replace(/^[⛔↳]\s*(BLOCKED:\s*)?/, '').trim();
}

/* Group predicates — project-list-card._filterGroup, verbatim semantics. */
function groupOf(p) {
  if (p.hasBlocker) return 'blocked';
  if (p.notes.length) return 'in_progress';   // lean card 2026-09-24: started = has a dated note
  return 'next_up';
}

/* --------------------------------------------------------------------------
 * 3. Overlap detection
 * ------------------------------------------------------------------------ */

function tokenize(s) {
  return new Set(
    (s || '').toLowerCase().split(/[^a-z0-9]+/)
      .filter((w) => w.length > 2 && !STOPWORDS.has(w))
  );
}

function overlapCoefficient(a, b) {
  if (!a.size || !b.size) return 0;
  let hits = 0;
  for (const w of a) if (b.has(w)) hits++;
  return hits / Math.min(a.size, b.size);
}

/* --------------------------------------------------------------------------
 * 4. Small helpers
 * ------------------------------------------------------------------------ */

const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function fmtAge(days) {
  if (days == null) return 'never';
  if (days === 0) return 'today';
  if (days === 1) return '1d ago';
  if (days < 60) return days + 'd ago';
  return Math.round(days / 30) + 'mo ago';
}

function stateAge(hass, entityId) {
  const st = hass && hass.states && hass.states[entityId];
  if (!st || !st.state || st.state === 'unknown' || st.state === 'unavailable') return null;
  const t = Date.parse(st.state.replace(' ', 'T'));
  if (isNaN(t) || t < NEVER_BEFORE) return null;
  return Math.floor((Date.now() - t) / 86400000);
}

function num(hass, entityId) {
  const st = hass && hass.states && hass.states[entityId];
  const v = st ? parseFloat(st.state) : NaN;
  return isNaN(v) ? 0 : v;
}

function copyToClipboard(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  const ta = document.createElement('textarea');
  ta.value = text; document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); } finally { document.body.removeChild(ta); }
  return Promise.resolve();
}

/* --------------------------------------------------------------------------
 * 5. Styles
 * ------------------------------------------------------------------------ */

const CSS = `
:host{display:block;height:100%;
  --bg:#0e1013;--sidebar:#0b0d10;--card:#14171b;--raised:#1a1e23;
  --line:rgba(255,255,255,.07);--line2:rgba(255,255,255,.12);
  --t1:#ebe9e4;--t2:#9aa0a8;--t3:#8b9098;
  --work:#5aa9ff;--warn:#f0b35a;--danger:#ff6b6b;--ok:#5cc98b;--goals:#3fc9d6;
  font-family:'Instrument Sans',system-ui,-apple-system,'Segoe UI',sans-serif;
  color:var(--t1);
}
*{box-sizing:border-box}
.wrap{display:flex;height:100%;min-height:100vh;background:var(--bg);overflow:hidden}
button{font-family:inherit;color:inherit}
.mono{font-family:'JetBrains Mono',ui-monospace,'SF Mono',Menlo,monospace}
.lbl{font-family:'JetBrains Mono',ui-monospace,Menlo,monospace;font-size:10.5px;
  text-transform:uppercase;letter-spacing:.1em;color:var(--t3)}

/* ---- sidebar ---- */
.sb{width:232px;flex:0 0 232px;background:var(--sidebar);border-right:1px solid var(--line);
  display:flex;flex-direction:column;overflow-y:auto}
.sb-mark{padding:18px 16px 14px;font-size:15px;font-weight:600;letter-spacing:-.01em;
  display:flex;align-items:center;gap:9px}
.sb-mark .dot{width:8px;height:8px;border-radius:50%;background:var(--work)}
.sb-hub{margin:0 10px 10px;padding:9px 12px;border-radius:10px;display:flex;align-items:center;
  gap:10px;cursor:pointer;font-size:13.5px;border:1px solid transparent;background:none;width:calc(100% - 20px);text-align:left}
.sb-hub:hover{background:var(--raised)}
.sb-hub.on{background:var(--raised);border-color:var(--line2)}
.sb-grp{padding:14px 16px 6px}
.sb-row{display:block;width:calc(100% - 20px);margin:0 10px 2px;padding:8px 10px;border-radius:10px;
  cursor:pointer;background:none;border:1px solid transparent;text-align:left}
.sb-row:hover{background:var(--raised)}
.sb-row.on{background:var(--raised);border-color:var(--line2)}
.sb-top{display:flex;align-items:center;gap:9px}
.sb-top ha-icon{--mdc-icon-size:17px;flex:0 0 17px}
.sb-nm{flex:1;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-pct{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--t2)}
.sb-rev{width:7px;height:7px;border-radius:50%;flex:0 0 7px}
.sb-bar{height:2px;border-radius:2px;background:rgba(255,255,255,.08);margin-top:7px;overflow:hidden}
.sb-bar i{display:block;height:100%;border-radius:2px}
.sb-foot{margin-top:auto;padding:14px 16px;border-top:1px solid var(--line)}

/* ---- main ---- */
.main{flex:1;min-width:0;display:flex;flex-direction:column;overflow:hidden}
.hdr{position:sticky;top:0;z-index:5;background:rgba(14,16,19,.92);backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line);padding:12px 22px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.mode{display:flex;align-items:center;gap:9px;font-size:12.5px;color:var(--t2)}
.mode .dot{width:8px;height:8px;border-radius:50%}
.mode b{color:var(--t1);font-weight:600}
.seg{display:flex;border:1px solid var(--line2);border-radius:8px;overflow:hidden}
.seg button{background:none;border:none;padding:5px 11px;font-size:11px;cursor:pointer;color:var(--t2)}
.seg button.on{background:var(--raised);color:var(--t1)}
.sync{display:flex;gap:14px;margin-left:auto;flex-wrap:wrap}
.srch{display:flex;align-items:center;gap:6px;background:var(--card);border:1px solid var(--line2);border-radius:9px;
  padding:0 8px;min-width:240px;flex:0 1 340px;--mdc-icon-size:16px;color:var(--t3)}
.srch:focus-within{border-color:var(--work)}
.srch input{flex:1;min-width:0;background:none;border:none;outline:none;color:var(--t1);font:inherit;font-size:12.5px;padding:7px 0}
.srch .x{background:none;border:none;color:var(--t2);font-size:16px;cursor:pointer;padding:0 2px}
#mcSort,#mcBoardSort{background:var(--card);border:1px solid var(--line2);border-radius:9px;color:var(--t2);font:inherit;font-size:11.5px;padding:6px 8px}
#mcBoardSort{margin-left:auto;align-self:center;margin-bottom:4px}
.sync .s{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--t2)}
.sync .s i{width:6px;height:6px;border-radius:50%;display:block}
.body{flex:1;overflow-y:auto;padding:20px 22px 60px}

/* ---- generic card ---- */
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h3{margin:0 0 12px;font-size:12px;font-weight:600;letter-spacing:.02em}
.chip{display:inline-flex;align-items:center;gap:5px;border-radius:6px;padding:2.5px 7px;
  font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.06em;text-transform:uppercase}
.bar{height:4px;border-radius:3px;background:rgba(255,255,255,.08);overflow:hidden}
.bar i{display:block;height:100%;border-radius:3px}
.btn{background:var(--raised);border:1px solid var(--line2);border-radius:8px;padding:7px 12px;
  font-size:12px;cursor:pointer;color:var(--t1)}
.btn:hover{border-color:rgba(255,255,255,.22)}
.btn.pri{background:var(--work);border-color:var(--work);color:#06121f;font-weight:600}
.btn:disabled{opacity:.4;cursor:default}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:-4px 0 12px;flex-wrap:wrap}
.tabs button{background:none;border:none;border-bottom:2px solid transparent;padding:7px 10px;
  font-size:11.5px;cursor:pointer;color:var(--t3)}
.tabs button.on{color:var(--t1);border-bottom-color:var(--work)}
.muted{color:var(--t2);font-size:12px}
.empty{color:var(--t3);font-size:12px;padding:14px 2px}

/* ---- quick add ---- */
.qa{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:18px}
.qa input[type=text]{flex:1;min-width:220px;background:var(--card);border:1px solid var(--line2);
  border-radius:10px;padding:9px 13px;font-size:13px;color:var(--t1);outline:none;font-family:inherit}
.qa input[type=text]:focus{border-color:var(--work)}
.qa select{background:var(--card);border:1px solid var(--line2);border-radius:10px;padding:9px 10px;
  font-size:12px;color:var(--t1);outline:none;font-family:inherit;cursor:pointer}
.qa .seg button{padding:7px 10px}
.qa-warn{width:100%;font-size:11.5px;color:var(--warn);margin-top:-8px}
.qa-sim{width:100%;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.qa-sim .r{display:flex;align-items:center;gap:10px;padding:5px 0;font-size:12.5px}
.qa-sim .r .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---- hub grid ---- */
.grid{display:grid;grid-template-columns:minmax(340px,2fr) minmax(300px,1fr);gap:16px;align-items:start}
@media (max-width:1100px){.grid{grid-template-columns:1fr}}
.full{grid-column:1/-1}
.now-t{font-size:23px;font-weight:600;line-height:1.25;margin:10px 0 8px;letter-spacing:-.015em}
.now-next{font-size:12.5px;color:var(--t2);margin-bottom:14px;line-height:1.5}
.now-acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.then{margin-top:16px;border-top:1px solid var(--line);padding-top:12px}
.then .r{display:flex;gap:9px;align-items:center;padding:5px 0;font-size:12.5px;color:var(--t2);cursor:pointer}
.then .r:hover{color:var(--t1)}
.ev{display:flex;gap:11px;padding:8px 0;border-bottom:1px solid var(--line);font-size:12.5px}
.ev:last-child{border-bottom:none}
.ev .tm{flex:0 0 74px;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--t2)}
.today{margin-bottom:18px;padding:6px 18px 10px}.td-r{align-items:center;gap:9px}.td-r .tm{flex:0 0 96px}
.td-r .tt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--t1);cursor:pointer}.td-r .tt:hover{text-decoration:underline}
.td-r .btn{padding:3px 8px;font-size:11px;line-height:1.3}.td-r:last-of-type{border-bottom:none}
.dg .td-r:last-of-type{border-bottom:1px solid var(--line)}.dg .row{padding:7px 6px}.dg-note{font-size:12px;color:var(--t2);padding:0 6px 8px 40px;border-bottom:1px solid var(--line);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ---- item rows ---- */
.row{display:flex;align-items:center;gap:11px;padding:9px 6px;border-radius:8px;cursor:pointer;
  border-bottom:1px solid var(--line)}
.row:last-child{border-bottom:none}
.row:hover{background:var(--raised)}
.ck{width:15px;height:15px;flex:0 0 15px;border-radius:50%;border:1.5px solid var(--t3)}
.row .t{flex:1;min-width:0;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .meta{display:flex;align-items:center;gap:9px;flex:0 0 auto}
.row .pb{width:58px}
.row .pc{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--t2);width:30px;text-align:right}
.row .ag{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--t3);width:56px;text-align:right}
.lint-i{color:var(--warn);font-size:11.5px}
.board{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
@media (max-width:1200px){.board{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:640px){.board{grid-template-columns:1fr}}
.bcol{min-width:0;display:flex;flex-direction:column;gap:6px}
.bcol-h{display:flex;justify-content:space-between;font-weight:600;font-size:13px;color:var(--t1)}
.bcol-h .mono{font-family:'JetBrains Mono',monospace;color:var(--t3);font-weight:400}
.bcol-hint{font-size:11px;color:var(--t3);margin-bottom:4px}
.br{position:relative;padding:8px 44px 8px 10px;border:1px solid var(--line);border-radius:8px;cursor:pointer}
.br:hover{border-color:var(--t3)}
.br-t{font-size:13px;display:flex;gap:7px;align-items:baseline;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.br-dot{width:7px;height:7px;border-radius:50%;flex:0 0 7px;transform:translateY(-1px)}
.br-s{font-size:11.5px;color:var(--t2);margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.br-a{position:absolute;right:10px;top:9px;font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--t3)}
.resume{margin:0 0 14px;padding:10px 12px;border-left:3px solid var(--work);background:rgba(90,169,255,.07);border-radius:4px;font-size:13px}
.resume .lbl{display:block;margin-bottom:4px}
.lint-d{margin:-4px 0 12px;color:var(--warn);font-size:12px}

/* ---- branch board ---- */
.bh{display:flex;align-items:center;gap:13px;margin-bottom:6px;flex-wrap:wrap}
.bh h2{margin:0;font-size:19px;font-weight:600;letter-spacing:-.015em}
.bh ha-icon{--mdc-icon-size:22px}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;margin-top:18px;align-items:start}
.col h4{margin:0 0 10px;display:flex;align-items:center;gap:8px}
.col .cnt{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--t3)}
.ic{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:11px 12px;
  margin-bottom:8px;cursor:pointer}
.ic:hover{border-color:var(--line2)}
.ic .t{font-size:13px;line-height:1.35;margin-bottom:6px}
.ic .ns{font-size:11.5px;color:var(--t2);line-height:1.45;margin-bottom:8px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.ic .ft{display:flex;align-items:center;gap:9px}

/* ---- drawer ---- */
.scrim{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:20}
.dw{position:fixed;top:0;right:0;bottom:0;width:470px;max-width:100vw;background:var(--card);
  border-left:1px solid var(--line2);z-index:21;display:flex;flex-direction:column;
  box-shadow:-24px 0 60px rgba(0,0,0,.45)}
.dw-b{flex:1;overflow-y:auto;padding:18px 20px}
.dw-f{border-top:1px solid var(--line);padding:12px 20px;display:flex;gap:8px;flex-wrap:wrap}
.dw h2{font-size:17px;font-weight:600;margin:10px 0 14px;line-height:1.3}
.sec{margin:18px 0}
.sec .lbl{margin-bottom:7px;display:block}
.note{font-size:12px;color:var(--t2);padding:5px 0;border-bottom:1px solid var(--line);line-height:1.5}
.note b{color:var(--t3);font-family:'JetBrains Mono',monospace;font-size:10.5px;font-weight:400;margin-right:8px}
.ta{width:100%;background:var(--raised);border:1px solid var(--line2);border-radius:8px;
  padding:9px 11px;font-size:12.5px;color:var(--t1);font-family:inherit;outline:none;resize:vertical}
.via{font-size:10.5px;color:var(--t3);margin-left:7px;white-space:nowrap;font-weight:400}
.pre{background:var(--raised);border:1px solid var(--line);border-radius:8px;padding:11px;
  font-size:11.5px;line-height:1.55;color:var(--t2);white-space:pre-wrap;max-height:220px;overflow:auto}
.tb-row{display:flex;gap:6px;flex-wrap:wrap}
.tb-chip{display:inline-flex;align-items:center;gap:7px;background:var(--raised);border:1px solid var(--line2);
  border-radius:8px;padding:6px 10px;font-size:12px;cursor:pointer;color:var(--t1)}
.tb-chip ha-icon{--mdc-icon-size:15px;color:var(--c)}
.tb-chip b{font-family:'JetBrains Mono',monospace;font-size:10.5px;font-weight:400;color:var(--t3)}
.tb-chip:hover{border-color:rgba(255,255,255,.22)}
.tb-chip.on{border-color:var(--c);background:color-mix(in srgb,var(--c) 14%,var(--raised))}
.tb-chip.zero{opacity:.45}
.tb-grp{font-family:'JetBrains Mono',monospace;font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;
  color:var(--t2);margin:14px 0 4px}
.tb-grp:first-child{margin-top:0}
.tb-grp .cnt{color:var(--t3);text-transform:none;letter-spacing:0}
.tb-note{text-transform:none;letter-spacing:0;color:var(--warn);font-family:inherit;font-weight:400}
.tb-sel{background:var(--raised);border:1px solid var(--line2);border-radius:8px;padding:7px 9px;font-size:12px;
  color:var(--t1);font-family:inherit;outline:none}
.cf{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:40;display:flex;align-items:center;justify-content:center}
.cf-box{background:var(--card);border:1px solid var(--line2);border-radius:12px;padding:20px;max-width:460px;
  box-shadow:0 24px 60px rgba(0,0,0,.55)}
.cf-t{font-size:15px;font-weight:600;margin-bottom:9px}
.cf-b{font-size:12.5px;color:var(--t2);line-height:1.55;margin-bottom:16px}
.cf-a{display:flex;gap:8px;justify-content:flex-end}
.cmp-box{max-width:min(1080px,96vw);width:100%;max-height:90vh;overflow:auto}
.cmp-g{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media (max-width:760px){.cmp-g{grid-template-columns:1fr}}
.cmp-col{background:var(--raised);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}
.cmp-t{font-size:14.5px;font-weight:600;margin:0 0 10px;line-height:1.35}
.cmp-l{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--t3);margin:10px 0 3px}
.cmp-v{font-size:12.5px;color:var(--t1);line-height:1.5}
.cmp-d{max-height:220px;overflow:auto;font-size:11.5px}
.cmp-a{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.ro{font-size:11px;color:var(--warn);border:1px solid rgba(240,179,90,.3);background:rgba(240,179,90,.07);
  border-radius:8px;padding:8px 11px;margin-bottom:14px;line-height:1.5}
/* ---- calm hub (2026-09-24) ---- */
.sb-nav{margin-bottom:6px}
.sb-nav .sb-hub{margin-bottom:2px}
.sb-dots{display:inline-flex;gap:4px;align-items:center}
.sb-dots i{width:7px;height:7px;border-radius:50%;display:block}
.setup{display:flex;align-items:center;gap:7px;flex-wrap:wrap;flex-basis:100%}
.setup .q{font-size:12.5px;font-weight:600;margin-right:4px}
.pill{background:var(--card);border:1px solid var(--line2);border-radius:999px;padding:5px 12px;font-size:12px;color:var(--t2);cursor:pointer;font-family:inherit}
.pill:hover{color:var(--t1);border-color:rgba(255,255,255,.22)}
.pill.on{background:var(--work);border-color:var(--work);color:#06121f;font-weight:600}
.hero-h{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:2px 0 14px}
.hero-h h1{margin:0;font-size:24px;font-weight:600;letter-spacing:-.015em}
.nm-grid{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px;align-items:start}
.nm-grid.one{grid-template-columns:minmax(0,1fr)}
@media (max-width:1000px){.nm-grid{grid-template-columns:minmax(0,1fr)}}
.hero{padding:20px 22px}
.now-ns{font-size:14.5px;line-height:1.5;color:var(--t1)}
.stop{margin:12px 0 0;padding:8px 11px;border-left:3px solid var(--work);background:rgba(90,169,255,.07);border-radius:4px;font-size:12.5px;color:var(--t2);line-height:1.5}
.strip{display:flex;align-items:center;gap:12px;padding:12px 16px;margin-top:12px;flex-wrap:wrap}
.strip .ic2{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;flex:0 0 30px;--mdc-icon-size:17px}
.strip .tx{flex:1;min-width:200px}
.strip .tx b{font-weight:600;font-size:13.5px}
.strip .tx small{display:block;color:var(--t2);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.board3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
@media (max-width:1000px){.board3{grid-template-columns:minmax(0,1fr)}}
.why{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--t3);white-space:nowrap}
.linkb{background:none;border:none;padding:0;color:var(--work);font-size:12.5px;cursor:pointer;font-family:inherit}


/* ---- quick add v2 (2026-09-25) ---- */
.qa2{background:var(--card);border:1px solid var(--line2);border-radius:14px;padding:10px 12px;margin-bottom:12px}
.qa2:focus-within{border-color:var(--work)}
.qa2-row{display:flex;gap:8px;align-items:center}
.qa2-row input{flex:1;min-width:0;background:none;border:0;outline:0;color:var(--t1);font:inherit;font-size:14.5px;padding:6px 2px}
.qa2-g{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px;min-height:24px}
.qg{position:relative;border:1px dashed var(--line2);background:var(--raised);border-radius:999px;padding:2px 10px;font-size:12px;color:var(--t2);cursor:pointer}
.qg.pin{border-style:solid;border-color:var(--work)}
.qg b{color:var(--t1);font-weight:600}
.qm{position:absolute;top:24px;left:0;z-index:15;background:var(--raised);border:1px solid var(--line2);border-radius:10px;padding:4px;min-width:180px;max-height:280px;overflow:auto;box-shadow:0 12px 30px rgba(0,0,0,.5);display:flex;flex-direction:column}
.qm span{padding:5px 9px;border-radius:6px;font-size:12.5px;color:var(--t1)}
.qm span:hover{background:var(--card)}
.qh{font-size:11.5px;color:var(--t3);margin-left:auto}
.qa2-feed{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
.qf{position:relative;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 36px 10px 14px}
.qf.dup{border-color:rgba(240,179,90,.45)}
.qf-x{position:absolute;right:8px;top:6px;background:none;border:0;color:var(--t3);font-size:17px;cursor:pointer}
.qf-raw{color:var(--t2);font-size:12.5px}
.qf-src{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--t3)}
.qf-t{font-weight:600;font-size:14.5px;margin:2px 0 5px}
.qf-c{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:5px}
.qf-ns{font-size:12.5px}
.qf-a{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
.qf-w{color:var(--warn);font-size:12.5px;margin-top:4px}
.qf-s{display:flex;gap:8px;align-items:center;font-size:12px;color:var(--t3);margin-top:5px}
.ok{color:var(--ok);font-size:12.5px}
.spin{width:12px;height:12px;border-radius:50%;border:2px solid #b07cf0;border-right-color:transparent;animation:mcsp .8s linear infinite;display:inline-block}
@keyframes mcsp{to{transform:rotate(360deg)}}
.qf-sub{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line2)}
.qf-cv{margin-top:8px;font-size:12.5px;color:var(--t2);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sb-badge{background:var(--work);color:#06121f;border-radius:999px;font-size:10.5px;font-weight:700;padding:0 7px;line-height:17px}

/* ---- collapsible sidebar (2026-09-25) ---- */
.sb{transition:width .15s ease,flex-basis .15s ease}
.sb-mark{padding-right:8px}
.sb-tog{margin-left:auto;background:none;border:1px solid transparent;border-radius:8px;color:var(--t3);cursor:pointer;padding:3px;display:grid;place-items:center;--mdc-icon-size:18px}
.sb-tog:hover{color:var(--t1);border-color:var(--line2)}
.sb.collapsed{width:60px;flex-basis:60px}
.sb.collapsed .sb-lbl,.sb.collapsed .sb-nm,.sb.collapsed .sb-foot{display:none !important}
.sb.collapsed{overflow-x:hidden}
.sb.collapsed .sb-mark{padding:14px 0 10px;justify-content:center}
.sb.collapsed .sb-mark .dot{display:none}
.sb.collapsed .sb-tog{margin:0}
.sb.collapsed .sb-hub,.sb.collapsed .sb-row{width:44px;margin-left:8px;margin-right:8px;justify-content:center;padding-left:0;padding-right:0;position:relative}
.sb.collapsed .sb-top{justify-content:center;position:relative}
.sb.collapsed .sb-grp{padding:10px 0 4px;margin:0 12px;border-top:1px solid var(--line)}
.sb.collapsed .sb-dots,.sb.collapsed .sb-badge{position:absolute;top:3px;right:3px}
.sb.collapsed .sb-badge{font-size:9.5px;padding:0 4px;line-height:14px}

`;

/* --------------------------------------------------------------------------
 * 6. The element
 * ------------------------------------------------------------------------ */

/* ---- branch table / spreadsheet view (2026-09-25) ---- */
const BT_COLS = [
  { k: 'status', label: 'Status', def: true },
  { k: 'lane', label: 'Priority', def: true },
  { k: 'due', label: 'Due', def: true }, /* 2026-11-01, docs/CARD_FORMAT.md (Today rank) */
  { k: 'owner', label: 'Who can do it', def: false },
  { k: 'next', label: 'Next action', def: true },
  { k: 'note', label: 'Last note', def: true }, /* 2026-11-24, docs/CARD_FORMAT.md (Reading view) */
  { k: 'toolbox', label: 'Toolbox', def: false },
  { k: 'effort', label: 'Time', def: true },
  { k: 'cost', label: 'Cost', def: true },
  { k: 'created', label: 'Created', def: false },
  { k: 'updated', label: 'Updated', def: true },
];
const BT_STATUS = { in_progress: ['In progress', '#5aa9ff'], next_up: ['Next up', '#9aa0a8'], blocked: ['Blocked', '#ff6b6b'], done: ['Done', '#5cc98b'] };
const CSS_TABLE = `
.nx{font-size:15px;line-height:1.5;color:var(--t1);padding:12px 14px;background:var(--raised);border-left:3px solid var(--work);border-radius:8px}
.dtx{font-size:13px;line-height:1.6;color:var(--t2);white-space:pre-wrap;overflow-wrap:anywhere}
.dw-links{display:flex;gap:14px;margin-top:6px}
.dw-fold{border-top:1px solid var(--line);padding-top:12px}
.dw-fold>.bt-link{font-size:13px}
.bt-seg{display:flex;border:1px solid var(--line2);border-radius:8px;overflow:hidden;margin-left:auto}
.bt-seg button{background:none;border:none;padding:6px 12px;font-size:12px;cursor:pointer;color:var(--t2);display:flex;align-items:center;gap:6px;--mdc-icon-size:15px}
.bt-seg button.on{background:rgba(90,169,255,.14);color:var(--work)}
.bt-top{display:flex;align-items:center;gap:2px;border-bottom:1px solid var(--line);margin:14px 0 12px;flex-wrap:wrap}
.bt-tab{background:none;border:none;border-bottom:2px solid transparent;padding:9px 12px;font-size:13px;color:var(--t2);cursor:pointer;margin-bottom:-1px}
.bt-tab span{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--t3);margin-left:3px}
.bt-tab.on{color:var(--t1);border-bottom-color:var(--work)}
.bt-tab.on span{color:var(--work)}
.bt-fil{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.bt-fil .srch{min-width:200px;flex:0 1 280px}
.bt-sel{background:var(--card);border:1px solid var(--line2);border-radius:9px;color:var(--t1);font:inherit;font-size:12.5px;padding:7px 10px;cursor:pointer}
.bt-sel.on{border-color:var(--work);color:var(--work)}
.bt-link{background:none;border:none;color:var(--work);font-size:12.5px;cursor:pointer;padding:4px}
.bt-chk{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--t2);cursor:pointer;background:none;border:none}
.bt-wrap{border:1px solid var(--line);border-radius:12px;overflow:auto;background:var(--card)}
table.bt{width:100%;border-collapse:collapse;font-size:13px;min-width:720px}
.bt th{position:sticky;top:0;z-index:1;background:var(--raised);text-align:left;font-weight:500;color:var(--t2);font-size:12px;padding:9px 12px;border-bottom:1px solid var(--line2);white-space:nowrap;cursor:pointer;user-select:none}
.bt th.on{color:var(--t1)}
.bt th .ar{color:var(--work);margin-left:5px}
.bt td{padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
.bt tbody tr:last-child td{border-bottom:none}
.bt tr.r{cursor:pointer}
.bt tr.r:hover td{background:rgba(255,255,255,.025)}
.bt tr.cur td{background:rgba(90,169,255,.08)}
.bt tr.sel td{background:rgba(90,169,255,.13)}
.bt td.t{max-width:340px;overflow:hidden;text-overflow:ellipsis}
.bt td.n{color:var(--t2);max-width:380px;overflow:hidden;text-overflow:ellipsis}
.bt td.a,.bt td.c{font-size:12px;color:var(--t2)}
.bt td.a{font-family:'JetBrains Mono',monospace;font-size:11.5px}
.bt td.x,.bt th.x{width:40px;padding:0;text-align:center}
.bt td.nt{max-width:300px}
.bt .ag{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--t3);margin-right:4px}
.bt tr.gh td{background:var(--raised);color:var(--t2);font-size:11.5px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;padding:7px 12px;cursor:default}
.bt tr.gh td span{color:var(--t3);font-weight:400;margin-left:6px}
.bt tr.bn td{background:rgba(199,155,255,.05)}
.bt tr.bm td.t{padding-left:30px}
.bt-od{border:1px solid rgba(255,107,107,.35);background:rgba(255,107,107,.06);border-radius:12px;padding:10px 12px;margin:12px 0 4px}
.bt-od-h{font-size:11.5px;font-weight:600;letter-spacing:.03em;text-transform:uppercase;color:var(--danger);margin-bottom:6px}
.bt-od-h b{color:var(--t2);font-weight:400;margin-left:4px}
.bt-od-r{display:flex;gap:10px;align-items:baseline;padding:5px 0;cursor:pointer;font-size:13px;border-top:1px solid var(--line)}
.bt-od-r:first-of-type{border-top:none}
.bt-od-r:hover .t{color:var(--work)}
.bt-od-r .late{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:var(--danger);min-width:58px}
.bt-od-r .t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46%}
.bt-od-r .ns{color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;font-size:12px}
.bt-od .btn{margin-top:6px}
.box{display:inline-block;width:15px;height:15px;border:1.5px solid var(--t3);border-radius:4px;vertical-align:middle;position:relative;flex:0 0 15px}
.box.on,.box.mid{border-color:var(--work)}
.box.on{background:var(--work)}
.box.on::after{content:'';position:absolute;left:3.5px;top:0.5px;width:4px;height:8px;border:solid #0e1013;border-width:0 2px 2px 0;transform:rotate(45deg)}
.box.mid::after{content:'';position:absolute;left:2px;right:2px;top:5px;height:2px;background:var(--work)}
.pill{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11.5px;border:1px solid transparent;line-height:1.5}
.bt .dots{background:none;border:none;color:var(--t3);cursor:pointer;padding:4px 6px;border-radius:6px;--mdc-icon-size:15px}
.bt .dots:hover{background:var(--raised);color:var(--t1)}
.bt-foot{display:flex;align-items:center;gap:10px;padding:12px 2px;font-size:12.5px;color:var(--t2);flex-wrap:wrap}
.bt-foot .btn[disabled]{opacity:.4;cursor:default}
.bt-bulk{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 12px;margin-bottom:10px;border:1px solid rgba(90,169,255,.35);background:rgba(90,169,255,.08);border-radius:10px;font-size:12.5px}
.bt-colm{position:relative}
.bt-menu{position:absolute;right:0;top:calc(100% + 6px);z-index:8;background:var(--raised);border:1px solid var(--line2);border-radius:10px;padding:6px;min-width:180px;box-shadow:0 10px 30px rgba(0,0,0,.45)}
.bt-menu button{display:flex;align-items:center;gap:9px;width:100%;background:none;border:none;padding:7px 8px;border-radius:7px;font-size:12.5px;cursor:pointer;text-align:left}
.bt-menu button:hover{background:rgba(255,255,255,.05)}
.sb-mode{margin:8px 10px 2px}
.sb-mseg{display:flex;gap:2px;padding:2px;border-radius:9px;background:rgba(255,255,255,.04)}
.sb-mseg button{flex:1;display:flex;align-items:center;justify-content:center;gap:5px;background:none;border:0;border-radius:7px;padding:5px 0;font-size:11.5px;color:var(--t3);cursor:pointer;--mdc-icon-size:14px}
.sb-mseg button:hover{color:var(--t1)}
.sb-mseg button.on{background:var(--raised);color:var(--t1);box-shadow:0 1px 2px rgba(0,0,0,.35)}
.sb-mone{display:none}
.sb.collapsed .sb-mseg{display:none}
.sb.collapsed .sb-mode{margin:6px 8px}
.sb.collapsed .sb-mone{display:grid;place-items:center;width:44px;height:34px;margin:0 auto;background:none;border:1px solid transparent;border-radius:8px;color:var(--t2);cursor:pointer;--mdc-icon-size:17px}
.sb.collapsed .sb-mone:hover{border-color:var(--line2);color:var(--t1)}
`;

class MissionControlV2App extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hass = null;
    this._ready = false;
    this._loading = true;
    this._err = null;

    this.data = {};          // key -> { open: [], done: [] }
    this.events = [];
    this.view = { name: 'hub', branch: null };
    /* Deep link (2026-11-01, docs/CARD_FORMAT.md (Daily pushes)): /mission-control?page=digest opens a page straight away. */
    try { const pg = new URLSearchParams(location.search).get('page'); if (pg && ['digest', 'claude', 'inbox', 'someday', 'house', 'past'].includes(pg)) this.view = { name: pg, branch: null }; } catch (e) {}
    this.drawer = null;      // { branchKey, uid }
    this.blocksCache = {};   // uid → blocks from mission_control/items/get 
    this.historyCache = {};  // uid → last field changes from items/get 
    this.skipped = new Set();

    this.modeOverride = this._readMode();
    this.qa = { text: '', branch: null, branchTouched: false, lane: null, laneTouched: false };
    this.qa.pins = {}; this.qa.menu = null; this.feed = []; this.inbox = [];
    this.tabs = { attention: 'overlap', lanes: null, toolbox: null };
    this.tbMap = {};
    this.showMore = {};
    this._lastFetch = 0;
    this.search = '';
    this.searchDone = false;
    this.sort = 'default';
    this.setup = 'all'; this.setupMore = false; this.qaOpen = false; this.boardOpen = false;
    this.sbCollapsed = false;
    try { this.sbCollapsed = localStorage.getItem('mc.sbCollapsed') === '1'; } catch (e) {}
    try { this.boardOpen = localStorage.getItem('mc.boardOpen') === '1'; } catch (e) {}
    try { this.sort = localStorage.getItem('mc.sort') || 'default'; } catch (e) {}
    this.boardSort = 'default'; /* Your board's own sort (2026-09-28) — localStorage mc.boardSort */
    try { this.boardSort = localStorage.getItem('mc.boardSort') || 'default'; } catch (e) {}
    this.branchView = 'table';
    try { this.branchView = localStorage.getItem('mc.branchView') || 'table'; } catch (e) {}
    this.bt = { tab: 'all', q: '', status: '', lane: '', owner: '', sort: 'updated', dir: 'desc', page: 0, per: 25, done: false, sel: new Set(), cols: null, menu: false };
    try { this.bt.per = +localStorage.getItem('mc.btPer') || 25; } catch (e) {}
    this.bt.group = 'who'; this.bt.openB = new Set(); this.provOpen = false;
    this.dw = {};
    try { this.bt.group = localStorage.getItem('mc.btGroup') || 'who'; } catch (e) {}
    try { this.bt.cols = JSON.parse(localStorage.getItem('mc.btCols') || 'null'); } catch (e) {}
    /* 2026-10-11: a saved column list predates Time/Cost — add them once. */
    try {
      if (Array.isArray(this.bt.cols) && !localStorage.getItem('mc.btColsEffort')) {
        for (const c of ['effort', 'cost']) if (!this.bt.cols.includes(c)) this.bt.cols.push(c);
        localStorage.setItem('mc.btCols', JSON.stringify(this.bt.cols));
      }
      localStorage.setItem('mc.btColsEffort', '1');
      /* 2026-11-01: a saved column list predates Due — add it once (docs/CARD_FORMAT.md (Today rank)). */
      if (Array.isArray(this.bt.cols) && !localStorage.getItem('mc.btColsDue')) {
        if (!this.bt.cols.includes('due')) this.bt.cols.splice(Math.max(0, this.bt.cols.indexOf('lane') + 1), 0, 'due');
        localStorage.setItem('mc.btCols', JSON.stringify(this.bt.cols));
      }
      localStorage.setItem('mc.btColsDue', '1');
      if (Array.isArray(this.bt.cols) && !localStorage.getItem('mc.btColsNote')) {
        if (!this.bt.cols.includes('note')) this.bt.cols = BT_COLS.map((c) => c.k).filter((x) => this.bt.cols.includes(x) || x === 'note');
        localStorage.setItem('mc.btCols', JSON.stringify(this.bt.cols));
      }
      localStorage.setItem('mc.btColsNote', '1');
    } catch (e) {}
  }

  /* ---- lifecycle ---- */

  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (first) this._boot();
    else if (this._ready) this._renderLight();
  }
  get hass() { return this._hass; }

  set narrow(v) { this._narrow = v; }
  set panel(v) { this._panel = v; }
  set route(v) { this._route = v; }

  async _boot() {
    this.shadowRoot.innerHTML = `<style>${CSS}${CSS_TABLE}</style><div id="root"></div>`;
    this._root = this.shadowRoot.getElementById('root');
    this._onKey = (e) => {
      if (e.key === 'Escape' && this.drawer) { this.drawer = null; this.render(); return; }
      const t = (e.composedPath && e.composedPath()[0]) || e.target;
      const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
      if (e.key === '/' && !typing && this.isConnected) {
        const el = this.shadowRoot.getElementById('mcSearch');
        if (el) { e.preventDefault(); el.focus(); }
      }
    };
    document.addEventListener('keydown', this._onKey);
    if (window.ResizeObserver) { this._ro = new ResizeObserver(() => this._fitSetup()); this._ro.observe(this); }
    try { applyConfig(await this._hass.callWS({ type: 'mission_control/config' })); }
    catch (e) { this._err = 'Mission Control is not loaded: ' + ((e && e.message) || e); }
    this.render();
    await this.refresh();
    /* Live updates (2026-10-07): one todo/item/subscribe per tracker (+ the inbox)
       pushes the whole list on every change, so edits from the phone, voice, Claude sessions or
       another tab land in seconds. The poll stays as a slow safety net (10 min; 60 s only while
       the subscriptions are down). */
    this._subs = {};
    await this._subscribeAll();
    this._timer = setInterval(() => this.refresh(), this._liveOk() ? LIVE_POLL_MS : 60000);
  }

  disconnectedCallback() {
    if (this._timer) clearInterval(this._timer);
    if (this._liveRetry) clearTimeout(this._liveRetry);
    document.removeEventListener('keydown', this._onKey);
    if (this._ro) this._ro.disconnect();
    this._unsubscribeAll();
  }

  /* ---- live subscriptions ---- */

  _liveOk() { return this._subs && Object.keys(this._subs).length >= BRANCHES.length; }

  async _subscribeAll() {
    if (!this._hass || !this._hass.connection || !this._hass.connection.subscribeMessage) return;
    const targets = BRANCHES.map((b) => ent(b.key)).concat(this._hass.states['todo.mission_inbox'] ? ['todo.mission_inbox'] : []);
    for (const entityId of targets) {
      if (this._subs[entityId]) continue;
      try {
        this._subs[entityId] = await this._hass.connection.subscribeMessage(
          (msg) => this._onLive(entityId, msg), { type: 'todo/item/subscribe', entity_id: entityId });
      } catch (e) {
        console.warn('[mission-control] live subscribe failed for', entityId, e);
      }
    }
    this._live = this._liveOk();
    if (!this._live && !this._liveRetry) {
      this._liveRetry = setTimeout(() => { this._liveRetry = null; this._subscribeAll(); }, 120000);
    }
  }

  _unsubscribeAll() {
    for (const [k, un] of Object.entries(this._subs || {})) { try { un && un(); } catch (e) {} delete this._subs[k]; }
  }

  _onLive(entityId, msg) {
    const items = msg && msg.items;
    if (!Array.isArray(items)) return;
    this._lastLive = Date.now();
    if (entityId === 'todo.mission_inbox') {
      this.inbox = items.filter((it) => it.status === 'needs_action');
    } else {
      const b = BRANCHES.find((x) => ent(x.key) === entityId);
      if (!b) return;
      const dec = (arr) => arr.map((it) => ({
        uid: it.uid,
        summary: cleanSummary(it.summary),
        rawSummary: it.summary || '',
        description: it.description || '',
        status: it.status,
        due: it.due || '',
        branch: b.key,
        p: parseDesc(it.description),
      }));
      this.data = Object.assign({}, this.data, {
        [b.key]: { open: dec(items.filter((it) => it.status === 'needs_action')), done: dec(items.filter((it) => it.status === 'completed')) },
      });
      this._err = null;
    }
    this._renderWhenIdle();
  }

  /* A push mid-typing would wipe the note or next-step being written; wait for the field to blur. */
  _renderWhenIdle() {
    const el = this.shadowRoot && this.shadowRoot.activeElement;
    const typing = el && ((el.tagName === 'TEXTAREA' && el.value) || (el.tagName === 'INPUT' && el.value));
    if (typing) {
      if (!this._idleTimer) this._idleTimer = setTimeout(() => { this._idleTimer = null; this._renderWhenIdle(); }, 4000);
      return;
    }
    this.render();
  }

  /* ---- data ---- */

  /* Blocks: the store splits BLOCKED BY into uid / date / text blocks and
     clears uid and date blocks by itself; a text block is cleared here by a person. */
  async _loadBlocks(uid) {
    if (!this._hass || !uid) return;
    try {
      const r = await this._hass.callWS({ type: 'mission_control/items/get', uid });
      this.blocksCache[uid] = (r && r.blocks) || [];
      this.historyCache[uid] = (r && r.history) || [];
    } catch (e) {
      this.blocksCache[uid] = null;
      this.historyCache[uid] = null;
    }
    if (this.drawer && this.drawer.uid === uid) this.render();
  }

  async _revert(uid, id) {
    const r = await this._hass.callWS({ type: 'mission_control/items/revert', history_id: id, source: 'panel' });
    this.historyCache[uid] = (r && r.history) || [];
    await this.refresh();
  }

  /* History: one row per field change, newest first, with Undo. */
  _historySection(i) {
    const list = this.historyCache[i.uid];
    if (!Array.isArray(list) || !list.length) return '';
    const SRC = { todo: 'phone / panel', ws: 'Claude', panel: 'panel', revert: 'undo', 'system:unblock': 'auto-unblock', 'system:repeat': 'repeat' };
    const when = (ts) => { const d = new Date(ts); return isNaN(d) ? ts : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }); };
    const cut = (v) => { const t = (v == null ? '' : String(v)).replace(/\s+/g, ' '); return t.length > 60 ? t.slice(0, 57) + '…' : t; };
    const undoable = (h) => !['note', 'created', 'deleted'].includes(h.field);
    const rows = list.slice(0, 12).map((h) => `<div class="row">
      <span class="t"><b>${esc(h.field.replace('_', ' '))}</b> ${h.field === 'note' ? esc(cut(h.new)) : `${esc(cut(h.old)) || '<span class="muted">—</span>'} → ${esc(cut(h.new)) || '<span class="muted">—</span>'}`}
        <small class="muted" style="display:block;font-size:11px">${esc(when(h.ts))} · ${esc(SRC[h.source] || h.source)}</small></span>
      ${undoable(h) ? `<span class="meta"><button class="btn" data-act="revert" data-u="${i.uid}" data-id="${h.id}" title="Write the old value back">Undo</button></span>` : ''}
    </div>`).join('');
    return `<div class="sec"><span class="lbl">History</span>${rows}</div>`;
  }

  async _clearBlock(k, uid, seq) {
    const r = await this._hass.callWS({ type: 'mission_control/blocks/clear', uid, seq, by: 'me' });
    this.blocksCache[uid] = (r && r.blocks) || [];
    await this.refresh();
  }

  _blocksSection(i) {
    if (!i.p.hasBlocker) return '';
    const list = this.blocksCache[i.uid];
    const glyph = { uid: 'mdi:card-text-outline', date: 'mdi:calendar-clock', text: 'mdi:account-clock' };
    const rows = Array.isArray(list) && list.length ? list.map((bk) => {
      const done = !!bk.cleared_at;
      const label = bk.kind === 'uid' ? `card ${bk.value}${(() => { const t = bk.target_uid && this.findAny(bk.target_uid); return t ? ' · ' + esc(t.summary) : ''; })()}`
        : bk.kind === 'date' ? bk.value : esc(bk.value);
      const how = bk.kind === 'uid' ? 'clears when that card closes' : bk.kind === 'date' ? 'clears the day it passes' : 'only you can clear this';
      return `<div class="row" style="${done ? 'opacity:.55' : ''}">
        <ha-icon icon="${done ? 'mdi:check-circle-outline' : glyph[bk.kind]}" style="--mdc-icon-size:16px;color:${done ? 'var(--ok)' : 'var(--warn)'}"></ha-icon>
        <span class="t">${label}<small class="muted" style="display:block;font-size:11px">${done ? esc(bk.cleared_by || 'cleared') : how}</small></span>
        ${!done ? `<span class="meta"><button class="btn" data-act="clear-block" data-k="${i.branch}" data-u="${i.uid}" data-seq="${bk.seq}">Clear</button></span>` : ''}
      </div>`; }).join('')
      : `<div class="muted">${esc(i.p.blockedBy)}</div>`;
    return `<div class="sec"><span class="lbl">Waiting on</span>${rows}
      <div class="muted" style="margin-top:6px;font-size:11px">Card and date blocks clear themselves; when the last one clears the card gets an "unblocked" note and leaves this column.</div></div>`;
  }

  async _items(entityId, status) {
    const r = await this._hass.callWS({
      type: 'call_service', domain: 'todo', service: 'get_items',
      target: { entity_id: [entityId] },
      service_data: { status: [status] },
      return_response: true,
    });
    return (r && r.response && r.response[entityId] && r.response[entityId].items) || [];
  }

  async refresh() {
    if (!this._hass) return;
    this._lastFetch = Date.now();
    try {
      const out = {};
      await Promise.all(BRANCHES.map(async (b) => {
        const [open, done] = await Promise.all([
          this._items(ent(b.key), 'needs_action'),
          this._items(ent(b.key), 'completed'),
        ]);
        const dec = (arr) => arr.map((it) => ({
          uid: it.uid,
          summary: cleanSummary(it.summary),
          rawSummary: it.summary || '',
          description: it.description || '',
          status: it.status,
          due: it.due || '',
          branch: b.key,
          p: parseDesc(it.description),
        }));
        out[b.key] = { open: dec(open), done: dec(done) };
      }));
      this.data = out;
      this._err = null;
    } catch (e) {
      this._err = e && e.message ? e.message : String(e);
      console.error('[mission-control] tracker fetch failed', e);
    }
    await this._loadToolboxMap();
    try { this.inbox = this._hass.states['todo.mission_inbox'] ? await this._items('todo.mission_inbox', 'needs_action') : []; } catch (e) { this.inbox = []; }
    await this._loadEvents();
    this._loading = false;
    this._ready = true;
    this.render();
  }

  async _loadEvents() {
    if (!CALENDAR) { this.events = []; return; }
    try {
      const now = new Date();
      const end = new Date(now.getTime() + 7 * 86400000);
      const r = await this._hass.callApi(
        'GET',
        `calendars/${CALENDAR}?start=${encodeURIComponent(now.toISOString())}&end=${encodeURIComponent(end.toISOString())}`
      );
      /* Events the mission_control calendar sync mirrored from cards  are
         already listed as due rows — skip them so a dated card is not shown twice. */
      this.events = (Array.isArray(r) ? r : [])
        .filter((e) => !String(e.description || '').startsWith('Mission Control card'))
        .slice(0, 12);
    } catch (e) {
      this.events = [];
    }
  }

  allOpen() {
    const out = [];
    for (const b of BRANCHES) {
      const d = this.data[b.key];
      if (d) out.push(...d.open);
    }
    return out;
  }

  find(branchKey, uid) {
    const d = this.data[branchKey];
    if (!d) return this.findAny(uid);
    return d.open.find((i) => i.uid === uid) || d.done.find((i) => i.uid === uid) || this.findAny(uid);
  }

  /* A card shown on a tagged branch page lives in another tracker (2026-10-07). */
  findAny(uid) {
    for (const b of BRANCHES) {
      const d = this.data[b.key];
      if (!d) continue;
      const hit = d.open.find((i) => i.uid === uid) || d.done.find((i) => i.uid === uid);
      if (hit) return hit;
    }
    return null;
  }

  /* Open (or done) cards from OTHER branches that carry `TAGS: <k>` (docs/CARD_FORMAT.md (Tags)). */
  taggedInto(k, done) {
    const out = [];
    for (const b of BRANCHES) {
      if (b.key === k) continue;
      const d = this.data[b.key];
      if (!d) continue;
      for (const i of (done ? d.done : d.open)) if ((i.p.tags || []).includes(k)) out.push(i);
    }
    return out;
  }
  tagsOf(i) { return (i.p.tags || []).filter((t) => t !== i.branch); }

  /* ---- mode ---- */

  _readMode() {
    try { return localStorage.getItem('mc.modeOverride') || 'auto'; } catch (e) { return 'auto'; }
  }
  _writeMode(v) {
    this.modeOverride = v;
    try { localStorage.setItem('mc.modeOverride', v); } catch (e) { /* private mode */ }
  }

  /* Work-hours window, browser-local (matches hero-task-card). */
  _inHours(d) {
    const h = d.getHours(), dow = d.getDay();
    return h >= 8 && h < 17 && dow >= 1 && dow <= 5;
  }

  mode() {
    if (this.modeOverride === 'work') return { work: true, forced: true };
    if (this.modeOverride === 'home') return { work: false, forced: true };
    /* Auto also honours the legacy kill switch — see docs/CARD_FORMAT.md (Work hours). */
    const st = this._hass.states[WORK_BOOST_TOGGLE];
    const boostOn = st ? st.state === 'on' : true;
    return { work: boostOn && this._inHours(new Date()), forced: false, boostOn };
  }

  _nextBoundary() {
    const now = new Date();
    const d = new Date(now);
    if (this._inHours(now)) { d.setHours(17, 0, 0, 0); return { until: d, label: 'until' }; }
    d.setHours(8, 0, 0, 0);
    if (d <= now) d.setDate(d.getDate() + 1);
    while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() + 1);
    return { until: d, label: 'work resumes' };
  }

  /* ---- focus ordering ---- */

  focusPool() {
    let pool = this.modePool(this.allOpen().filter((i) => !i.p.hasBlocker && !this.isSomeday(i)));
    pool = pool.filter((i) => ownerOf(i) === 'me' && !this.skipped.has(i.branch + '|' + i.uid) && this.setupMatch(i));
    const score = (i) => {
      const stale = i.p.lastNote == null ? 0 : (daysSince(i.p.lastNote) || 0);
      const dd = this.dueIn(i);
      return (i.p.priority === 'high' ? 1000 : 0)
        + (laneOf(i) === 'urgent' ? 800 : 0)
        + (dd != null && dd >= 0 && dd <= 7 ? 300 : 0)
        + (this.isInFlight(i) ? 200 : 0)
        - Math.min(stale, 90) / 2;
    };
    /* PIN: is an absolute override above the score — it exists in live data. */
    return pool.sort((a, b) => {
      if (a.p.pinned !== b.p.pinned) return a.p.pinned ? -1 : 1;
      return score(b) - score(a);
    });
  }

  /* Active (2026-09-28): the hub hero shows the most recently modified card — newest dated
     note, ties broken by newest created (UUIDv1). Any owner; still honours mode (work-hours filter),
     setup, Not now, blocked and the shelf. */
  activePool() {
    let pool = this.modePool(this.allOpen().filter((i) => !i.p.hasBlocker && !this.isSomeday(i)));
    pool = pool.filter((i) => !this.skipped.has(i.branch + '|' + i.uid) && this.setupMatch(i));
    return sortItems(pool, 'updated');
  }

  /* ---- attention buckets ---- */

  /* Stale = HAS a dated PROGRESS note and the newest is 30+ days old.
     Items with no dated note at all are a different (much larger) problem —
     they're undocumented, not stale — so they get their own bucket below.
     Live data 2026-09-15: 239 open, 45 with notes (38 stale), 194 with none. */
  stale() {
    return this.allOpen()
      .map((i) => ({ i, d: i.p.lastNote == null ? null : daysSince(i.p.lastNote) }))
      .filter((x) => x.d != null && x.d >= 30)
      .sort((a, b) => b.d - a.d);
  }

  undocumented() {
    return this.allOpen().filter((i) => i.p.lastNote == null);
  }

  lintList() {
    return this.allOpen()
      .map((i) => ({ i, issues: lintCard(i) }))
      .filter((x) => x.issues.length)
      .sort((a, b) => b.issues.length - a.issues.length);
  }

  overlaps() {
    const items = this.allOpen().map((i) => ({ i, t: tokenize(i.summary) }));
    const pairs = [];
    const seen = new Set();
    for (let a = 0; a < items.length; a++) {
      for (let b = a + 1; b < items.length; b++) {
        const c = overlapCoefficient(items[a].t, items[b].t);
        if (c >= 0.34) {
          const k = items[a].i.uid + items[b].i.uid;
          if (seen.has(k)) continue;
          seen.add(k);
          pairs.push({ a: items[a].i, b: items[b].i, c });
        }
      }
    }
    return pairs.sort((x, y) => y.c - x.c).slice(0, 40);
  }

  similarTo(text, branchKey) {
    const t = tokenize(text);
    if (t.size === 0) return [];
    const pool = branchKey ? (this.data[branchKey] ? this.data[branchKey].open : []) : this.allOpen();
    return pool
      .map((i) => ({ i, c: overlapCoefficient(t, tokenize(i.summary)) }))
      .filter((x) => x.c >= 0.34)
      .sort((a, b) => b.c - a.c)
      .slice(0, 3);
  }

  /* ---- writes (gated) ---- */

  async write(fn, label) {
    if (!WRITES_ENABLED) {
      this._toast(`Writes are switched off in this browser — "${label}" not performed.`);
      return false;
    }
    try { await fn(); await this.refresh(); return true; }
    catch (e) { this._toast('Write failed: ' + (e.message || e)); return false; }
  }

  /* Custom modal — never window.confirm(), which blocks the page. */
  _confirm(title, body, okLabel, onOk) {
    const host = document.createElement('div');
    host.className = 'cf';
    host.innerHTML = `<div class="cf-box">
      <div class="cf-t">${esc(title)}</div>
      <div class="cf-b">${esc(body)}</div>
      <div class="cf-a">
        <button class="btn" data-cf="no">Cancel</button>
        <button class="btn pri" data-cf="yes">${esc(okLabel)}</button>
      </div></div>`;
    host.addEventListener('click', (e) => {
      const v = e.target && e.target.dataset && e.target.dataset.cf;
      if (!v) return;
      host.remove();
      if (v === 'yes') onOk();
    });
    this.shadowRoot.appendChild(host);
  }

  _toast(msg) {
    const d = document.createElement('div');
    d.textContent = msg;
    d.style.cssText = 'position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:50;' +
      'background:#1a1e23;border:1px solid rgba(255,255,255,.14);color:#ebe9e4;padding:11px 16px;' +
      "border-radius:10px;font:12.5px/1.4 system-ui;max-width:min(560px,90vw);box-shadow:0 12px 34px rgba(0,0,0,.5)";
    this.shadowRoot.appendChild(d);
    setTimeout(() => d.remove(), 4200);
  }

  /* =======================================================================
   * Rendering
   * ===================================================================== */

  _renderLight() { /* hass ticks shouldn't re-render the whole tree mid-typing */ }

  render() {
    if (!this._root) return;
    const focused = this.shadowRoot.activeElement;
    const focusId = focused && focused.id;
    const caret = focused && focused.selectionStart;

    this._root.innerHTML = `<div class="wrap">${this._sidebar()}${this._main()}</div>${this._drawer()}${this._compare()}`;
    this._bind();
    this._fitSetup();

    if (focusId) {
      const el = this.shadowRoot.getElementById(focusId);
      if (el) { el.focus(); try { el.setSelectionRange(caret, caret); } catch (e) {} }
    }
  }

  /* ---- sidebar ---- */

  _reviewColor(days) {
    if (days == null) return 'var(--danger)';
    if (days < 7) return 'var(--ok)';
    if (days < 14) return 'var(--warn)';
    return 'var(--danger)';
  }

  /* ---- calm hub helpers (2026-09-24 remodel) ---- */

  isInFlight(i) {
    const n = i.p.notes[0];
    return !i.p.hasBlocker && !!n && daysSince(n.date) <= 14 && !CREATED_ONLY_RE.test(n.text);
  }

  dueIn(i) {
    if (!i.due) return null;
    const d = new Date(i.due.length <= 10 ? i.due + 'T12:00:00' : i.due);
    if (isNaN(d)) return null;
    const t = new Date(); t.setHours(12, 0, 0, 0); d.setHours(12, 0, 0, 0);
    return Math.round((d - t) / 86400000);
  }

  /* Someday shelf: parked out of sight, never closed. Not urgent/work/blocked/in progress/due soon;
     a want card quiet 30+ days, or anything untouched (last note, else created) for SOMEDAY_DAYS. */
  isSomeday(i) {
    const lane = laneOf(i);
    if (lane === 'urgent' || lane === 'work' || i.p.hasBlocker || this.isInFlight(i)) return false;
    const dd = this.dueIn(i);
    if (dd != null) return false; /* 2026-11-01: a dated card never shelves — overdue is a Today item, not a Someday one (§25) */
    let touched = i.p.lastNote ? daysSince(i.p.lastNote) : null;
    if (touched == null) { const c = uuidCreated(i.uid); touched = c == null ? null : Math.floor((Date.now() - c) / 86400000); }
    if (touched == null) return false;
    return touched > (lane === 'want' ? 30 : SOMEDAY_DAYS);
  }

  modePool(list) {
    const m = this.mode();
    /* Work hours = work items only (hard filter, fail-open): the work lane plus urgent Ticketing/Business cards. */
    if (m.work) { const w = list.filter((i) => laneOf(i) === 'work' || (WORK_TRACKERS.has(i.branch) && laneOf(i) === 'urgent')); return w.length ? w : list; }
    const h = list.filter((i) => !WORK_TRACKERS.has(i.branch) || laneOf(i) === 'urgent');
    return h.length ? h : list;
  }

  setupMatch(i) {
    if (!this.setup || this.setup === 'all') return true;
    const s = SETUPS.find((x) => x.k === this.setup);
    return s ? s.m(i, this) : true;
  }

  claudeList() {
    const age = (i) => (i.p.lastNote ? daysSince(i.p.lastNote) : 9999);
    return this.allOpen()
      .filter((i) => !i.p.hasBlocker && !this.isInFlight(i) && ownerOf(i) === 'claude' && !this.isSomeday(i))
      .sort((a, b) => (LANE_RANK[laneOf(a)] - LANE_RANK[laneOf(b)]) || (age(a) - age(b)));
  }

  somedayList() {
    const t = (i) => (i.p.lastNote ? daysSince(i.p.lastNote) : Math.floor((Date.now() - (uuidCreated(i.uid) || Date.now())) / 86400000));
    return this.allOpen().filter((i) => this.isSomeday(i)).map((i) => ({ i, d: t(i) })).sort((a, b) => a.d - b.d);
  }

  /* ---- sidebar ---- */

  _sidebar() {
    const m = this.mode();
    const groups = m.work ? ['work', 'home'] : ['home', 'work'];
    const rows = (g) => BRANCHES.filter((b) => b.group === g).map((b) => {
      const open = this.data[b.key] ? this.data[b.key].open : [];
      const urg = open.some((i) => laneOf(i) === 'urgent');
      const fl = open.some((i) => this.isInFlight(i));
      const on = this.view.name === 'branch' && this.view.branch === b.key;
      return `<button class="sb-row${on ? ' on' : ''}" data-act="branch" data-k="${b.key}" title="${esc(b.label)}">
        <div class="sb-top">
          <ha-icon icon="${b.icon}" style="color:${b.color}"></ha-icon>
          <span class="sb-nm">${esc(b.label)}</span>
          <span class="sb-dots">${urg ? '<i style="background:var(--warn)" title="Has urgent items"></i>' : ''}${fl ? '<i style="background:var(--work)" title="Something in progress"></i>' : ''}</span>
        </div></button>`;
    }).join('');
    const lint = this.lintList().length;
    const nav = (name, icon, label, dot) => `<button class="sb-hub${this.view.name === name ? ' on' : ''}" data-act="view" data-v="${name}" title="${esc(label)}">
      <ha-icon icon="${icon}" style="--mdc-icon-size:17px"></ha-icon><span class="sb-lbl" style="flex:1">${label}</span>${dot || ''}</button>`;
    const col = this.sbCollapsed;
    return `<nav class="sb${col ? ' collapsed' : ''}">
      <div class="sb-mark"><span class="dot"></span><span class="sb-lbl" style="flex:1">Mission Control</span>
        <button class="sb-tog" data-act="sb-toggle" title="${col ? 'Expand sidebar' : 'Collapse sidebar'}"><ha-icon icon="${col ? 'mdi:chevron-double-right' : 'mdi:chevron-double-left'}"></ha-icon></button></div>
      <div class="sb-nav">
        ${nav('hub', 'mdi:view-dashboard-outline', 'Hub')}
        ${nav('digest', 'mdi:weather-sunset-up', 'Digest')}
        ${nav('claude', 'mdi:creation', 'AI can handle')}
        ${this._hass && this._hass.states['todo.mission_inbox'] ? nav('inbox', 'mdi:inbox-arrow-down', 'Inbox', (this.inbox || []).length ? `<span class="sb-badge">${this.inbox.length}</span>` : '') : ''}
        ${nav('someday', 'mdi:weather-night', 'Someday shelf')}
        ${nav('house', 'mdi:broom', 'Housekeeping', lint ? '<span class="sb-dots"><i style="background:var(--warn)" title="Cards drifting from the lean card"></i></span>' : '')}
      </div>
      ${this._modeBox()}
      ${groups.map((g) => `<div class="sb-grp lbl"><span class="sb-lbl">${g === 'work' ? 'Work' : 'Home'}</span></div>${rows(g)}`).join('')}
      <div class="sb-foot muted" style="font-size:11px;display:flex;gap:12px;align-items:center">
        <span class="sb-dots"><i style="background:var(--warn)"></i></span>urgent
        <span class="sb-dots"><i style="background:var(--work)"></i></span>in progress</div>
    </nav>`;
  }

  /* ---- mode box in the sidebar (2026-09-25, moved from the header) ---- */
  _modeBox() {
    const m = this.mode();
    const opts = [['auto', 'mdi:autorenew', 'Auto'], ['work', 'mdi:briefcase-outline', 'Work'], ['home', 'mdi:home-outline', 'Home']];
    const cur = opts.find((o) => o[0] === this.modeOverride) || opts[0];
    const tip = (v, l) => v === 'auto' ? `Auto: follows work hours (now ${m.work ? 'work' : 'home'})` : `Always ${l.toLowerCase()}`;
    const btns = opts.map(([v, ic, l]) => `<button class="${this.modeOverride === v ? 'on' : ''}" data-act="mode" data-v="${v}" title="${esc(tip(v, l))}"><ha-icon icon="${ic}"></ha-icon><span>${l}</span></button>`).join('');
    const next = opts[(opts.indexOf(cur) + 1) % 3];
    return `<div class="sb-mode"><div class="sb-mseg">${btns}</div>
      <button class="sb-mone" data-act="mode" data-v="${next[0]}" title="${esc('Mode: ' + cur[2] + ' (click for ' + next[2] + ')')}"><ha-icon icon="${cur[1]}"></ha-icon></button></div>`;
  }

  /* ---- header ---- */

  _header() {
    const m = this.mode();
    const now = new Date();
    const b = this._nextBoundary();
    const time = now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    const until = b.until.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    const color = m.work ? 'var(--work)' : 'var(--warn)';
    const head = m.work ? 'Work hours' : 'Off hours';
    const tail = m.work ? `${b.label} ${until} · Work lane only` : `${b.label} ${until}`;
    const seg = ['auto', 'work', 'home'].map((v) =>
      `<button data-act="mode" data-v="${v}" class="${this.modeOverride === v ? 'on' : ''}">${v[0].toUpperCase() + v.slice(1)}</button>`
    ).join('');
    const onHub = this.view.name === 'hub' && !this.search.trim();
    return `<header class="hdr">
      <div class="srch" style="margin-left:auto"><ha-icon icon="mdi:magnify"></ha-icon>
        <input id="mcSearch" type="text" placeholder="Search all trackers  ( / )" value="${esc(this.search)}" autocomplete="off">
        ${this.search ? `<button class="x" data-act="search-clear" title="Clear">×</button>` : ''}</div>
      ${(this.view.name === 'branch' || this.view.name === 'hub') && !this.search.trim() ? '' : `<select id="mcSort" title="Sort lists">${SORTS.map(([k, l]) =>
        `<option value="${k}"${this.sort === k ? ' selected' : ''}>${k === 'default' ? 'Sort: default' : 'Sort: ' + esc(l)}</option>`).join('')}</select>`}
      <button class="btn${this.qaOpen ? ' pri' : ''}" data-act="qa-toggle">+ Add</button>
      ${onHub ? this._setupBar() : ''}
    </header>`;
  }

  /* "What are you set up for?" — filters the next move, up next and the board (2026-09-24). */
  _setupBar() {
    const pool = this.modePool(this.allOpen().filter((i) => !i.p.hasBlocker && !this.isSomeday(i)))
      .filter((i) => ownerOf(i) === 'me');
    const avail = SETUPS.filter((s) => s.k === 'all' || s.k === this.setup || pool.some((i) => s.m(i, this)));
    const pill = (s) => `<button class="pill${this.setup === s.k ? ' on' : ''}" data-setup-pill data-act="setup" data-v="${s.k}">${esc(s.label)}</button>`;
    return `<div class="setup"><span class="q">What are you set up for?</span>
      ${avail.map(pill).join('')}
      ${this.setupMore ? '' : `<button class="pill" id="mcSetupMore" data-act="setup-more" style="display:none">More ▾</button>`}</div>`;
  }

  /* Show every setup that fits on one line; only fold the rest behind "More" when they don't (2026-09-24). */
  _fitSetup() {
    const bar = this.shadowRoot && this.shadowRoot.querySelector('.setup');
    if (!bar || this.setupMore) return;
    const pills = [...bar.querySelectorAll('[data-setup-pill]')];
    const more = bar.querySelector('#mcSetupMore');
    if (!pills.length || !more) return;
    pills.forEach((p) => { p.style.display = ''; });
    more.style.display = 'none';
    const top = pills[0].offsetTop;
    const fits = () => pills.every((p) => p.style.display === 'none' || p.offsetTop === top) && (more.style.display === 'none' || more.offsetTop === top);
    if (fits()) return;
    more.style.display = '';
    for (let k = pills.length - 1; k > 0 && !fits(); k--) {
      if (pills[k].classList.contains('on')) continue;
      pills[k].style.display = 'none';
    }
  }

  _main() {
    if (this._loading) {
      return `<div class="main">${this._header()}<div class="body"><div class="empty">Loading trackers…</div></div></div>`;
    }
    const err = this._err
      ? `<div class="ro">Tracker fetch failed: ${esc(this._err)} — showing the last good data.</div>` : '';
    const v = this.view.name;
    const inner = this.search.trim() ? this._searchResults()
      : v === 'branch' ? this._branchBoard()
      : v === 'claude' ? this._claudeView()
      : v === 'someday' ? this._somedayView()
      : v === 'house' ? this._houseView()
      : v === 'past' ? this._pastView()
      : v === 'inbox' ? this._inboxView()
      : v === 'digest' ? this._digestView()
      : this._hub();
    const qa = this.qaOpen && v !== 'branch' ? this._quickAdd(null) : '';
    return `<div class="main">${this._header()}<div class="body">${err}${qa}${inner}</div></div>`;
  }

  /* ---- quick add ---- */

  /* ---- quick add: type → instant guesses → Enter → mission_control.file_note files it (with the
     AI Task entity set in the options; without one the note becomes a plain card). ---- */

  _guess(text) {
    const t = (text || '').toLowerCase();
    const g = {};
    g.branch = this._autoDetectBranch(text) || '';
    g.lane = /\b(asap|urgent|today|leak(ing)?|right away)\b/.test(t) ? 'urgent'
      : WORK_TRACKERS.has(g.branch) ? 'work'
      : /\b(maybe|someday|idea|nice to have|would be cool)\b/.test(t) ? 'want' : 'need';
    const tm = [['Plumber visit', /\bplumber\b/], ['Electrician visit', /\belectrician\b/], ['Electrical tools out', /\b(switch|fixture|outlet|breaker|wiring)\b/],
      ['Plumbing tools out', /\b(faucet|toilet|sink|drain|pipe)\b/], ['Physical checks', /\b(battery|check on|walk)\b/],
      ['Portals & follow-ups', /\b(call|email|renew|book|schedule|order)\b/], ['Car', /\b(car|tires?|oil change|brakes?)\b/], ['Session', /\b(dashboard|automation|card|script)\b/]];
    for (const [k, re] of tm) if (re.test(t)) { g.toolbox = k; break; }
    const d = new Date();
    if (/\b(asap|today)\b/.test(t)) g.due = ymd(d);
    else if (/\btomorrow\b/.test(t)) { d.setDate(d.getDate() + 1); g.due = ymd(d); }
    return g;
  }

  _quickAdd(pinnedBranch) {
    if (pinnedBranch && !this.qa.pins.branch) this.qa.pins.branch = pinnedBranch;
    const t = this.qa.text.trim();
    const g = { ...(t ? this._guess(t) : {}), ...this.qa.pins };
    const LBL = { branch: 'Branch', lane: 'Lane', toolbox: 'Setup', due: 'Due' };
    const shown = (k) => k === 'branch' ? (BY_KEY[g.branch] ? BY_KEY[g.branch].label : '?') : (g[k] || '—');
    const opts = (k) => k === 'branch' ? BRANCHES.map((b) => [b.key, b.label])
      : k === 'lane' ? ['urgent', 'work', 'need', 'want'].map((l) => [l, l])
      : k === 'toolbox' ? ['none', ...TOOLBOX_VALUES, 'Electrician visit', 'Plumber visit'].map((v) => [v, v])
      : [['', 'none'], [ymd(), 'today'], [ymd(new Date(Date.now() + 864e5)), 'tomorrow'], [ymd(new Date(Date.now() + 7 * 864e5)), 'in a week']];
    const chip = (k) => `<span class="qg${this.qa.pins[k] !== undefined ? ' pin' : ''}" data-act="qa-chip" data-k="${k}">${LBL[k]} <b>${esc(shown(k))}</b>
      ${this.qa.menu === k ? `<span class="qm">${opts(k).map(([v, l]) => `<span data-act="qa-pick" data-k="${k}" data-v="${esc(v)}">${esc(l)}</span>`).join('')}</span>` : ''}</span>`;
    const guesses = t ? ['branch', 'lane', 'toolbox', 'due'].map(chip).join('') +
      `<span class="qh">Dashed = guessed · click to change · Enter adds, Claude refines it</span>`
      : MC_CONFIG.ai_task
        ? `<span class="qh">Type anything — the AI picks the branch, lane, setup and next step, and checks for duplicates.</span>`
        : `<span class="qh">Type anything — it becomes a card on the branch shown. Set an AI Task entity under Settings → Mission Control → Configure to have notes filed for you.</span>`;
    return `<div class="qa2">
        <div class="qa2-row"><input id="qaText" type="text" placeholder="Add anything… press Enter" value="${esc(this.qa.text)}" autocomplete="off">
        <button class="btn pri" data-act="qa-file">Add ↵</button></div>
        <div class="qa2-g">${guesses}</div>
      </div>${this._qaFeed()}`;
  }

  _qaFeed() {
    if (!this.feed.length) return '';
    const chipsFor = (c) => c.title ? `<div class="qf-c"><span class="chip" style="background:${(BY_KEY[c.branch] || {}).color || '#888'}22;color:${(BY_KEY[c.branch] || {}).color || '#888'}">${esc((BY_KEY[c.branch] || {}).label || c.branch)}</span>
        <span class="chip" style="background:${LANE_COLORS[c.lane] || '#8b9098'}22;color:${LANE_COLORS[c.lane] || '#8b9098'}">${esc(LANE_LABELS[c.lane] || c.lane)}</span>
        ${c.toolbox && c.toolbox !== 'none' ? `<span class="chip" style="background:rgba(255,255,255,.06);color:var(--t2)">${esc(c.toolbox)}</span>` : ''}
        ${c.buy_first ? `<span class="chip" style="background:#f0b35a22;color:var(--warn)">Buy first: ${esc(c.buy_first)}</span>` : ''}
        ${c.due ? `<span class="chip" style="background:#f0b35a22;color:var(--warn)">Due ${esc(c.due)}</span>` : ''}
        ${c.waits ? '<span class="chip" style="background:#f0b35a22;color:var(--warn)">Blocked by the card above</span>' : ''}
        ${c.owner === 'claude' ? '<span class="chip" style="background:#b07cf022;color:#b07cf0">AI can handle</span>' : ''}</div>` : '';
    const row = (e) => {
      const r = e.res || {};
      const c = r.card || {};
      const k = (r.entity_id || '').replace(/^todo\.|_tracker$/g, '');
      const chips = c.title ? `<div class="qf-c"><span class="chip" style="background:${(BY_KEY[c.branch] || {}).color || '#888'}22;color:${(BY_KEY[c.branch] || {}).color || '#888'}">${esc((BY_KEY[c.branch] || {}).label || c.branch)}</span>
        <span class="chip" style="background:${LANE_COLORS[c.lane] || '#8b9098'}22;color:${LANE_COLORS[c.lane] || '#8b9098'}">${esc(LANE_LABELS[c.lane] || c.lane)}</span>
        ${c.toolbox && c.toolbox !== 'none' ? `<span class="chip" style="background:rgba(255,255,255,.06);color:var(--t2)">${esc(c.toolbox)}</span>` : ''}
        ${c.buy_first ? `<span class="chip" style="background:#f0b35a22;color:var(--warn)">Buy first: ${esc(c.buy_first)}</span>` : ''}
        ${c.due ? `<span class="chip" style="background:#f0b35a22;color:var(--warn)">Due ${esc(c.due)}</span>` : ''}
        ${c.owner === 'claude' ? '<span class="chip" style="background:#b07cf022;color:#b07cf0">AI can handle</span>' : ''}</div>` : '';
      const x = `<button class="qf-x" data-act="qa-dismiss" data-id="${e.id}" title="Dismiss">×</button>`;
      if (e.status === 'pending') return `<div class="qf">${x}<div class="qf-raw">“${esc(e.note)}”</div>
        <div class="qf-s"><span class="spin"></span>${MC_CONFIG.ai_task ? 'Filing it…' : 'Saving…'}</div></div>`;
      if (e.status === 'filed') {
        const list = (r.cards && r.cards.length) ? r.cards : [{ card: c, entity_id: r.entity_id, uid: r.uid }];
        const one = (f, idx) => {
          const cc = f.card || {}; const kk = (f.entity_id || '').replace(/^todo\.|_tracker$/g, '');
          const bl = BY_KEY[kk] || {};
          return `<div class="${idx ? 'qf-sub' : ''}">${idx ? `<div class="qf-src">split-off card · linked to the one above${cc.waits ? ' · waits for it' : ''}</div>` : ''}
            <div class="qf-t">${esc(cc.title)}</div>${chipsFor(cc)}<div class="qf-ns">${esc(cc.next_step || '')}</div>
            <div class="qf-a"><span class="ok">✓ Filed to ${esc(bl.label || kk)}</span>
              ${f.uid ? `<button class="btn" data-act="open" data-k="${kk}" data-u="${f.uid}">Open</button>` : ''}</div></div>`;
        };
        const covers = (r.covers || []).map((cv, ci) => {
          const done = (e.merged || []).includes(cv.uid);
          return `<div class="qf-cv">${done ? `<span class="ok">✓ Merged “${esc(cv.title)}” into this card</span>`
            : `Covers <b>“${esc(cv.title)}”</b> <button class="btn" data-act="qa-merge" data-id="${e.id}" data-i="${ci}">Merge it in</button>`}</div>`;
        }).join('');
        return `<div class="qf">${x}<div class="qf-src">${e.res && e.res.ai ? 'filed by the AI' : 'filed'} in ${e.secs}s${list.length > 1 ? ` · split into ${list.length} cards` : ''}</div>
          ${list.map(one).join('')}${covers}
          <div class="qf-a"><button class="btn" data-act="qa-undo" data-id="${e.id}">Undo${list.length > 1 ? ' all' : ''}</button></div></div>`;
      }
      if (e.status === 'duplicate') {
        const dk = (r.duplicate_entity || '').replace(/^todo\.|_tracker$/g, '');
        return `<div class="qf dup">${x}<div class="qf-raw">“${esc(e.note)}”</div>
        <div class="qf-w">Looks like the same task as <b>${esc(r.duplicate_title || r.duplicate_uid)}</b>${dk ? ` (${esc((BY_KEY[dk] || {}).label || dk)})` : ''}. It's held in the Inbox.</div>
        <div class="qf-a"><button class="btn pri" data-act="qa-dup-note" data-id="${e.id}">Add as a note there</button>
          <button class="btn" data-act="qa-dup-force" data-id="${e.id}">File as a new card</button>
          ${dk ? `<button class="btn" data-act="open" data-k="${dk}" data-u="${r.duplicate_uid}">Open it</button>` : ''}</div></div>`;
      }
      if (e.status === 'noted') return `<div class="qf">${x}<div class="ok">✓ Added as a dated note on “${esc(r.duplicate_title)}”</div></div>`;
      if (e.status === 'undone') return `<div class="qf">${x}<div class="muted">Undone — “${esc(c.title || e.note)}” removed.</div></div>`;
      return `<div class="qf dup">${x}<div class="qf-raw">“${esc(e.note)}”</div><div class="qf-w">Couldn't file it (${esc(e.err || 'error')}). Nothing was saved — try again.</div></div>`;
    };
    return `<div class="qa2-feed">${this.feed.slice(0, 6).map(row).join('')}</div>`;
  }

  async _fileNote(note, pins, opts = {}) {
    note = (note || '').trim();
    if (!note) return;
    const entry = opts.entry || { id: 'q' + Date.now() + Math.floor(Math.random() * 1000), note };
    entry.status = 'pending'; entry.t0 = Date.now();
    if (!opts.entry) this.feed.unshift(entry);
    this.render();
    if (!WRITES_ENABLED) { entry.status = 'error'; entry.err = 'writes are switched off in this browser'; return this.render(); }
    const clean = {};
    for (const [k, v] of Object.entries(pins || {})) if (v !== undefined && v !== null) clean[k] = v;
    try {
      const r = await this._hass.callWS({
        type: 'call_service', domain: 'mission_control', service: 'file_note',
        service_data: { note, source: opts.source || 'panel', pins: clean, force: !!opts.force },
        return_response: true,
      });
      entry.res = (r && r.response) || {};
      entry.status = entry.res.status === 'duplicate' ? 'duplicate' : entry.res.status === 'filed' ? 'filed' : 'error';
      if (entry.status === 'error') entry.err = 'unexpected reply';
    } catch (e) {
      entry.status = 'error'; entry.err = (e && e.message) || String(e);
    }
    entry.secs = Math.round((Date.now() - entry.t0) / 100) / 10;
    await this.refresh();
  }

  _inboxView() {
    const items = this.inbox || [];
    const rows = items.map((i) => {
      const d = i.description || '';
      const src = (d.match(/^source:\s*(.+)$/m) || [])[1] || '';
      const held = (d.match(/^held:\s*(.+)$/m) || [])[1] || '';
      const busy = this.feed.some((e) => e.note === i.summary && e.status === 'pending');
      return `<div class="row" style="cursor:default">
        <span class="t" style="white-space:normal">“${esc(i.summary)}”<br><span class="muted">${esc(src || 'unknown source')}${held ? ' · <span style="color:var(--warn)">' + esc(held) + '</span>' : ''}</span></span>
        <span class="meta">${busy ? '<span class="muted">filing…</span>' : held
          ? `<button class="btn" data-act="inbox-file" data-u="${esc(i.uid)}" data-force="1">File as new card</button>`
          : `<button class="btn" data-act="inbox-file" data-u="${esc(i.uid)}">File now</button>`}</span></div>`;
    }).join('');
    return this._pageHead('mdi:inbox-arrow-down', 'var(--work)', 'Inbox',
        'Captured notes waiting to be filed — from voice, your phone, or any add Claude couldn\'t finish. Held items look like duplicates; open the matching card or file them anyway.',
        items.length ? `<button class="btn pri" data-act="inbox-all">Let Claude file all</button>` : '')
      + `<div class="card">${rows || '<div class="empty">Inbox is empty — everything is filed.</div>'}</div>${this.qaOpen ? '' : this._qaFeed()}`;
  }

  _autoDetectBranch(text) {
    if (this.qa.branchTouched) return this.qa.branch;
    const t = (text || '').toLowerCase();
    if (!t) return null;
    const map = BRANCH_GUESS;
    for (const [k, re] of map) if (re.test(t)) return k;
    return null;
  }

  /* ---- hub ---- */

  /* ---- Today rank (docs/CARD_FORMAT.md (Today rank), 2026-11-01): deadlines beat recency ---- */

  /* Tier of an open, unblocked card: 0 overdue · 1 due today · 2 urgent lane with no date ·
     3 due tomorrow · 4 due within 7 days · null = not a Today item. A dated card rises on its
     own as the date nears; `urgent` is for the undated safety/money risks that must stay in view. */
  todayTier(i) {
    const d = this.dueIn(i);
    if (d != null && d < 0) return 0;
    if (d === 0) return 1;
    if (d == null && laneOf(i) === 'urgent') return 2;
    if (d === 1) return 3;
    if (d != null && d <= 7) return 4;
    return null;
  }

  /* Same pool as Active (mode, setup, Not now, blocked, shelf), ranked by tier; inside a tier the
     older overdue / sooner due first, then lane rank, then the smaller effort (quick wins), then title. */
  todayList(opts) {
    const all = !!(opts && opts.all); /* the Digest page ranks every tracker, like the push (§26) */
    let base = this.allOpen().filter((i) => !i.p.hasBlocker && !this.isSomeday(i));
    if (!all) base = this.modePool(base).filter((i) => !this.skipped.has(i.branch + '|' + i.uid) && this.setupMatch(i));
    const eff = (i) => (i.p.effortMin == null ? 1e9 : i.p.effortMin);
    return base.map((i) => ({ i, t: this.todayTier(i), d: this.dueIn(i) })).filter((x) => x.t != null)
      .sort((a, b) => (a.t - b.t)
        || ((a.t === 0 || a.t === 4) ? (a.d - b.d) : 0)
        || (LANE_RANK[laneOf(a.i)] - LANE_RANK[laneOf(b.i)])
        || (eff(a.i) - eff(b.i))
        || a.i.summary.localeCompare(b.i.summary, undefined, { sensitivity: 'base' }));
  }

  /* Today rows (shared by the hub block and the Digest page). skip=false hides the Not now button. */
  _todayRows(list, skip) {
    const TIER = [['Overdue', 'var(--danger)'], ['Due today', 'var(--warn)'], ['Urgent', LANE_COLORS.urgent], ['Tomorrow', 'var(--work)'], ['This week', 'var(--t2)']];
    const label = (x) => (x.t === 0 ? `Overdue ${-x.d}d` : x.t === 4 ? this._dayLabel(x.d) : TIER[x.t][0]);
    return list.map((x) => {
      const i = x.i; const b = BY_KEY[i.branch]; const lc = LANE_COLORS[laneOf(i)] || '#8b9098';
      return `<div class="ev td-r">
        <span class="tm" style="color:${TIER[x.t][1]}">${esc(label(x))}</span>
        <span class="br-dot" style="background:${lc}"></span>
        <span class="tt" data-act="open" data-k="${i.branch}" data-u="${i.uid}" title="${esc(i.p.nextStep || '')}">${esc(i.summary)}</span>
        <span class="why">${esc(b.label)}${i.p.effort ? ' · ' + esc(i.p.effort) : ''}</span>
        <button class="btn" data-act="done" data-k="${i.branch}" data-u="${i.uid}" title="Mark done">✓</button>
        ${skip === false ? '' : `<button class="btn" data-act="skip" data-k="${i.branch}" data-u="${i.uid}" title="Hide for this visit">Not now</button>`}
      </div>`;
    }).join('');
  }

  _todayCounts(list) {
    const n = (t) => list.filter((x) => x.t === t).length;
    return [[n(0), 'overdue'], [n(1), 'due today'], [n(2), 'urgent'], [n(3), 'tomorrow'], [n(4), 'this week']].filter((p) => p[0]).map((p) => `${p[0]} ${p[1]}`).join(' · ');
  }

  _todayRank() { /* note: a legacy _today() calendar card exists further down (unused) — keep the names apart */
    const list = this.todayList();
    if (!list.length) return '';
    const rows = this._todayRows(list.slice(0, 5));
    const more = list.length > 5 ? `<div class="muted" style="padding-top:8px">+${list.length - 5} more — <button class="linkb" data-act="view" data-v="digest">open the Digest</button> for the full list.</div>` : '';
    return `<div class="hero-h"><h1>Today</h1><span class="muted">${esc(this._todayCounts(list))}</span></div>
      <div class="card today">${rows}${more}</div>`;
  }

  /* ---- Digest page (docs/CARD_FORMAT.md (Daily pushes), 2026-11-01): the 08:00 push as a live page ----
     Same sections and rules as script.mission_control_digest: Today (the §25 rank over every tracker, no mode/setup
     filter), Came free (an `unblocked:` note today or yesterday), In flight (the panel's In progress), Urgent (the lane,
     dated or blocked ones marked), Still want this? (unblocked cards untouched 31+ days, oldest first), Closed this week
     (done cards whose newest note is within 7 days). Copy digest text reproduces the push wording. */
  digestData() {
    const today = ymd(), yday = ymd(new Date(Date.now() - 864e5));
    const open = this.allOpen();
    const age = (i) => (i.p.lastNote ? daysSince(i.p.lastNote) : 9999);
    const freed = open.filter((i) => (i.p.notes || []).some((n) => (n.date === today || n.date === yday) && /^unblocked:/i.test(n.text)));
    const flight = open.filter((i) => this.isInFlight(i)).sort((a, b) => age(a) - age(b));
    const urgent = open.filter((i) => laneOf(i) === 'urgent').sort((a, b) => (a.p.hasBlocker ? 1 : 0) - (b.p.hasBlocker ? 1 : 0) || age(a) - age(b));
    const stale = open.filter((i) => !i.p.hasBlocker && i.p.lastNote && age(i) > 30).sort((a, b) => age(b) - age(a));
    const done = [];
    for (const b of BRANCHES) { const d = this.data[b.key]; if (d) done.push(...d.done.filter((i) => i.p.lastNote && daysSince(i.p.lastNote) <= 7)); }
    done.sort((a, b) => (b.p.lastNote || '').localeCompare(a.p.lastNote || ''));
    return { today: this.todayList({ all: true }), freed, flight, urgent, stale, done, open: open.length };
  }

  _digestText() {
    const d = this.digestData();
    const cut = (t) => (t.length > 40 ? t.slice(0, 39) + '…' : t);
    const top = (arr) => arr.slice(0, 3).map((i) => cut(i.summary)).join(' · ');
    const tn = d.today.filter((x) => x.t <= 2);
    const c = (t) => tn.filter((x) => x.t === t).length;
    const out = [`Today ${tn.length}${tn.length ? ` (${c(0)} overdue · ${c(1)} due today · ${c(2)} urgent): ${top(tn.map((x) => x.i))}` : ': nothing overdue, due or urgent'}`];
    if (d.freed.length) out.push(`Came free ${d.freed.length}: ${top(d.freed)}`);
    out.push(`In flight ${d.flight.length}: ${top(d.flight)}`);
    out.push(`Urgent ${d.urgent.length}: ${top(d.urgent)}`);
    if (d.stale.length) out.push(`Still want this? ${d.stale[0].summary.length > 60 ? d.stale[0].summary.slice(0, 59) + '…' : d.stale[0].summary} (${daysSince(d.stale[0].p.lastNote)}d untouched)`);
    out.push(`Closed this week: ${d.done.length} · Open: ${d.open}`);
    return out.join('\n');
  }

  /* Today's activity (2026-11-02): sensor.mission_control_activity reads item_history, so phone check-offs
     count; until the sensor exists (needs the restart) fall back to cards whose newest note is dated today. */
  activityToday() {
    const st = this._hass && this._hass.states && this._hass.states['sensor.mission_control_activity'];
    if (st && !['unknown', 'unavailable'].includes(st.state)) {
      const a = st.attributes || {};
      const pick = (arr) => (arr || []).map((x) => this.findAny(x.uid) || { uid: x.uid, branch: x.branch, summary: x.title, p: { notes: [], lastNote: null } });
      return { live: true, closed: pick(a.closed), moved: pick(a.moved), freed: pick(a.freed), created: pick(a.created) };
    }
    const today = ymd();
    const closed = [], moved = [];
    for (const b of BRANCHES) { const d = this.data[b.key]; if (!d) continue;
      for (const i of d.done) if (i.p.lastNote === today) closed.push(i);
      for (const i of d.open) if (i.p.lastNote === today) moved.push(i);
    }
    return { live: false, closed, moved, freed: [], created: [] };
  }

  _digestView() {
    const d = this.digestData();
    const act = this.activityToday();
    const age = (i) => (i.p.lastNote ? daysSince(i.p.lastNote) + 'd' : '—');
    const lim = (k, n) => (this.showMore['dg_' + k] ? Infinity : n);
    const moreBtn = (k, total, n) => (total > n && !this.showMore['dg_' + k] ? `<button class="btn" style="margin-top:8px" data-act="more" data-v="dg_${k}">Show all ${total}</button>` : '');
    const sec = (key, icon, color, title, sub, body, total, n) => `<div class="card dg" style="margin-bottom:12px">
      <h3 class="lbl" style="color:${color};display:flex;align-items:center;gap:7px"><ha-icon icon="${icon}" style="--mdc-icon-size:15px"></ha-icon>${esc(title)} · ${total}${sub ? `<span class="muted" style="font-weight:400;margin-left:6px">${esc(sub)}</span>` : ''}</h3>
      ${body || '<div class="empty" style="padding:6px 0">Nothing here.</div>'}${moreBtn(key, total, n)}</div>`;
    const note = (i) => { const n = realNote(i.p); return n && !CREATED_ONLY_RE.test(n.text) ? n.text : (i.p.nextStep ? 'Next: ' + i.p.nextStep : ''); };
    const todayN = d.today.filter((x) => x.t <= 2).length;
    const todayHtml = d.today.length ? this._todayRows(d.today.slice(0, lim('today', 12)), false) : '';
    const freedHtml = d.freed.map((i) => this._row(i, `<span class="ag" title="${esc((i.p.notes[0] || {}).text || '')}">${esc((i.p.notes[0] || {}).date || '')}</span>`)).join('');
    const flightHtml = d.flight.slice(0, lim('flight', 8)).map((i) => `${this._row(i, `<span class="ag">${age(i)}</span>`)}
      <div class="dg-note">${esc(note(i).slice(0, 160))}</div>`).join('');
    const urgentHtml = d.urgent.map((i) => this._row(i, `${i.p.hasBlocker ? '<span class="chip" style="background:#ff6b6b22;color:var(--danger)">Blocked</span>' : ''}${this.dueIn(i) != null ? `<span class="chip" style="background:#f0b35a22;color:var(--warn)">Due ${esc(this._dayLabel(this.dueIn(i)))}</span>` : ''}<span class="ag">${age(i)}</span>`)).join('');
    const staleHtml = d.stale.slice(0, lim('stale', 5)).map((i) => this._row(i, `<span class="ag">${age(i)} quiet</span>
      <button class="btn" data-act="bring-back" data-k="${i.branch}" data-u="${i.uid}">Bring back</button>
      <button class="btn" data-act="done" data-k="${i.branch}" data-u="${i.uid}">Done</button>`)).join('');
    const doneHtml = d.done.slice(0, lim('done', 10)).map((i) => this._row(i, `<span class="ag">${esc(i.p.lastNote || '')}</span>`)).join('');
    const date = new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
    const actHtml = [...act.closed.map((i) => this._row(i, '<span class="chip" style="background:#5cc98b22;color:var(--ok)">Done</span>')),
      ...act.moved.map((i) => this._row(i, '<span class="chip" style="background:#5aa9ff22;color:var(--work)">Moved</span>'))].join('');
    return this._pageHead('mdi:weather-sunset-up', 'var(--warn)', 'Digest', `${esc(date)} · live — the 08:00 plan and the 17:00 wrap are extracts of this page`,
        `<button class="btn" data-act="copy-morning">Copy morning plan</button><button class="btn" data-act="copy-wrap">Copy evening wrap</button>`)
      + sec('act', 'mdi:history', 'var(--ok)', 'Today so far', act.live ? 'closed and moved, from the activity sensor' : 'closed and moved — by note date until the activity sensor is live', actHtml, act.closed.length + act.moved.length, 99)
      + sec('today', 'mdi:calendar-today', 'var(--danger)', 'Today', this._todayCounts(d.today) || 'nothing overdue, due or urgent', todayHtml, d.today.length, 12)
      + sec('freed', 'mdi:lock-open-variant-outline', 'var(--ok)', 'Came free', 'blocks that cleared today or yesterday', freedHtml, d.freed.length, 99)
      + sec('flight', 'mdi:progress-clock', 'var(--work)', 'In flight', 'a note in the last 14 days, newest first', flightHtml, d.flight.length, 8)
      + sec('urgent', 'mdi:fire', LANE_COLORS.urgent, 'Urgent', 'the lane, whatever the date', urgentHtml, d.urgent.length, 99)
      + sec('stale', 'mdi:sleep', 'var(--t2)', 'Still want this?', 'untouched 31+ days, oldest first', staleHtml, d.stale.length, 5)
      + sec('done', 'mdi:check-circle-outline', 'var(--ok)', 'Closed this week', `${d.open} still open`, doneHtml, d.done.length, 10);
  }

  _hub() {
    const pool = this.activePool();
    const now = pool[0] || null;
    const m = this.mode();
    let up = [], same = new Set();
    if (now) {
      const rest = pool.slice(1);
      const tb = this.tbOf(now);
      const s = tb && tb !== 'Session' && tb !== 'none' ? rest.filter((i) => this.tbOf(i) === tb) : [];
      same = new Set(s.map((i) => i.uid));
      up = rest.slice(0, 5); /* Also recent: pure recency, 5 rows (2026-09-28) */
    }
    const setupLbl = this.setup !== 'all' ? (SETUPS.find((s) => s.k === this.setup) || {}).label : '';

    const hero = now ? (() => {
      const b = BY_KEY[now.branch];
      const lane = laneOf(now);
      const lc = LANE_COLORS[lane] || '#8b9098';
      const n = realNote(now.p);
      const stop = n && !CREATED_ONLY_RE.test(n.text)
        ? `<div class="stop"><span class="lbl">Where it stopped · ${esc(n.date)}</span><br>${esc(n.text.length > 240 ? n.text.slice(0, 240) + '…' : n.text)}</div>` : '';
      return `<div class="card hero">
        <div style="display:flex;gap:7px;flex-wrap:wrap">
          <span class="chip" style="background:${b.color}22;color:${b.color}">${esc(b.label)}</span>
          ${lane === 'urgent' || lane === 'work' ? `<span class="chip" style="background:${lc}22;color:${lc}">${LANE_LABELS[lane]}</span>` : ''}
          ${this.dueIn(now) != null && this.dueIn(now) >= 0 && this.dueIn(now) <= 7 ? `<span class="chip" style="background:#f0b35a22;color:var(--warn)">Due ${this._dayLabel(this.dueIn(now))}</span>` : ''}
          ${now.p.pinned ? `<span class="chip" style="background:#3fc9d622;color:var(--goals)">Pinned</span>` : ''}
          ${ownerOf(now) === 'claude' ? `<span class="chip" style="background:#b07cf022;color:#b07cf0">AI can handle</span>` : ''}
          <span class="chip" style="background:rgba(255,255,255,.06);color:var(--t2)">${now.p.lastNote ? 'Updated ' + esc(now.p.lastNote) : 'Created ' + esc(fmtCreated(uuidCreated(now.uid)))}</span>
        </div>
        <div class="now-t">${esc(now.summary)}</div>
        <div class="now-ns">${esc(now.p.nextStep || 'No next step written yet — open it and add one.')}</div>
        ${stop}
        <div class="now-acts">
          <button class="btn pri" data-act="copy-init" data-k="${now.branch}" data-u="${now.uid}">▶ Start (copy prompt)</button>
          <button class="btn" data-act="done" data-k="${now.branch}" data-u="${now.uid}">✓ Done</button>
          <button class="btn" data-act="skip" data-k="${now.branch}" data-u="${now.uid}">Not now →</button>
          <button class="btn" data-act="open" data-k="${now.branch}" data-u="${now.uid}">Details</button>
        </div>
        <div class="then"><div class="lbl" style="margin-bottom:6px">Also recent</div>
          ${up.map((i) => `<div class="r" data-act="open" data-k="${i.branch}" data-u="${i.uid}">
            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--t1)">${esc(i.summary)}</span>
            <span class="why">${same.has(i.uid) ? (/ visit$/i.test(this.tbOf(i)) ? 'Same visit' : 'Same setup') + ' · ' + esc(this.tbOf(i)) : esc(BY_KEY[i.branch].label)}</span>
          </div>`).join('') || '<div class="empty" style="padding:4px 0">Nothing else recent for this setup.</div>'}
        </div>
      </div>`;
    })() : `<div class="card hero"><div class="now-t">Nothing active${setupLbl ? ' for ' + esc(setupLbl.toLowerCase()) : ''}.</div>
        <div class="now-ns">${this.skipped.size ? `You skipped ${this.skipped.size}. ` : ''}Try another setup, or switch mode.</div>
        ${this.skipped.size ? '<div class="now-acts"><button class="btn" data-act="unskip">Bring skipped back</button></div>' : ''}</div>`;

    const coming = this._coming();
    const date = new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
    return `${this._todayRank()}<div class="hero-h"><h1>Active</h1><span class="muted">${esc(date)}${setupLbl ? ' · ' + esc(setupLbl) : ''} · ${m.work ? 'work lane' : 'home'}</span></div>
      <div class="nm-grid${coming ? '' : ' one'}">${hero}${coming}</div>
      ${this._boardStrip()}${this._reviewStrip()}`;
  }

  _dayLabel(d) {
    if (d === 0) return 'today';
    if (d === 1) return 'tomorrow';
    const x = new Date(); x.setDate(x.getDate() + d);
    return x.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  }

  _evRow(e) {
    const now = Date.now();
    const startRaw = (e.start && (e.start.dateTime || e.start.date)) || e.start;
    const endRaw = (e.end && (e.end.dateTime || e.end.date)) || e.end;
    const s = Date.parse(startRaw), en = Date.parse(endRaw);
    const allDay = typeof startRaw === 'string' && startRaw.length <= 10;
    let color = 'var(--t2)';
    if (!allDay && s <= now && en > now) color = 'var(--danger)';
    else if (!allDay && s - now < 12 * 3600000) color = 'var(--warn)';
    const same = new Date(s).toDateString() === new Date().toDateString();
    const day = same ? '' : new Date(s).toLocaleDateString([], { weekday: 'short' }) + ' ';
    const tm = allDay ? 'all day' : new Date(s).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return `<div class="ev"><span class="tm" style="color:${color}">${esc(day + tm)}</span><span style="flex:1">${esc(e.summary || '(no title)')}</span></div>`;
  }

  /* Coming up: calendar (7 days) + tracker items due within 7 days. Hidden entirely when empty. */
  _coming() {
    const evs = this.events.slice(0, 4);
    const withDue = this.allOpen().map((i) => ({ i, d: this.dueIn(i) })).filter((x) => x.d != null);
    const due = withDue.filter((x) => x.d >= 0 && x.d <= 7).sort((a, b) => a.d - b.d);
    const past = withDue.filter((x) => x.d < 0);
    if (!evs.length && !due.length && !past.length) return '';
    const dueRows = due.map((x) => `<div class="ev" style="cursor:pointer" data-act="open" data-k="${x.i.branch}" data-u="${x.i.uid}">
      <span class="tm" style="color:var(--work)">${esc(this._dayLabel(x.d))}</span><span style="flex:1">${esc(x.i.summary)}</span></div>`).join('');
    return `<div class="card"><h3 class="lbl">Coming up</h3>
      ${evs.map((e) => this._evRow(e)).join('')}${dueRows}
      ${!evs.length && !due.length ? '<div class="empty" style="padding:4px 0">Nothing in the next 7 days.</div>' : ''}
      ${past.length ? `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">
        <button class="linkb" data-act="view" data-v="past">${past.length} date${past.length === 1 ? ' has' : 's have'} passed — close or reschedule</button></div>` : ''}
    </div>`;
  }

  _claudeStrip() {
    const list = this.claudeList();
    if (!list.length) return '';
    return `<div class="card strip">
      <span class="ic2" style="background:rgba(176,124,240,.14);color:#b07cf0"><ha-icon icon="mdi:creation"></ha-icon></span>
      <div class="tx"><b>AI can handle ${list.length}</b><small>Top: ${esc(list[0].summary)}</small></div>
      <button class="btn" data-act="copy-brief">Copy session brief</button>
      <button class="btn" data-act="view" data-v="claude">Open</button>
    </div>`;
  }

  _boardStrip() {
    const { cols } = this.boardCols();
    const sum = `${cols.flight.length} in progress · ${cols.you.length} ready for you · ${cols.waiting.length} blocked`;
    return `<div class="card strip">
      <span class="ic2" style="background:rgba(90,169,255,.14);color:var(--work)"><ha-icon icon="mdi:view-column-outline"></ha-icon></span>
      <div class="tx"><b>Your board</b><small>${esc(sum)}</small></div>
      <button class="btn" data-act="board-toggle">${this.boardOpen ? 'Hide board' : 'Show board'}</button>
    </div>${this.boardOpen ? this._board() : ''}`;
  }

  _somedayStrip() {
    const n = this.somedayList().length;
    if (!n) return '';
    return `<div class="card strip">
      <span class="ic2" style="background:rgba(255,255,255,.06);color:var(--t2)"><ha-icon icon="mdi:weather-night"></ha-icon></span>
      <div class="tx"><b>Someday shelf · ${n} parked</b><small>Want cards quiet 30+ days and anything untouched ${SOMEDAY_DAYS}+ days. Out of sight, not closed.</small></div>
      <button class="btn" data-act="view" data-v="someday">Open shelf</button>
    </div>`;
  }

  /* What changed in a branch since its last review: open cards created after the stamp or with a newer
     dated note, plus cards closed with a newer note. Never reviewed = every open card counts once. */
  reviewDelta(bk) {
    const d = this.data[bk] || { open: [], done: [] };
    const st = this._hass && this._hass.states[`input_datetime.${bk}_last_review`];
    let since = null;
    if (st && st.state && st.state !== 'unknown' && st.state !== 'unavailable') {
      const t = Date.parse(st.state.replace(' ', 'T'));
      if (!isNaN(t) && t >= NEVER_BEFORE) since = t;
    }
    if (since == null) return { n: d.open.length, open: d.open, since: null, sinceDate: null };
    const sd = new Date(since);
    const sinceDate = `${sd.getFullYear()}-${String(sd.getMonth() + 1).padStart(2, '0')}-${String(sd.getDate()).padStart(2, '0')}`;
    const changed = (i) => {
      const c = uuidCreated(i.uid);
      if (c != null && c > since) return true;
      const n = i.p.notes[0];
      return !!n && n.date > sinceDate;
    };
    const open = d.open.filter(changed);
    const done = d.done.filter(changed);
    return { n: open.length + done.length, open, since, sinceDate };
  }

  _reviewStrip() {
    const due = BRANCHES.map((b) => ({ b, r: this.reviewDelta(b.key) }))
      .filter((x) => x.r.n >= REVIEW_MIN_CHANGES).sort((a, b) => b.r.n - a.r.n);
    if (!due.length) return '';
    const x = due[0];
    const when = x.r.since == null ? 'never reviewed' : 'since the last review ' + fmtCreated(x.r.since);
    return `<div class="card strip">
      <span class="ic2" style="background:rgba(92,201,139,.14);color:var(--ok)"><ha-icon icon="mdi:refresh"></ha-icon></span>
      <div class="tx"><b>Review ${esc(x.b.label)}: ${x.r.n} card${x.r.n === 1 ? '' : 's'} changed</b><small>${esc(when)}${due.length > 1 ? ` · ${due.length - 1} other branch${due.length > 2 ? 'es' : ''} past ${REVIEW_MIN_CHANGES} changes` : ''}</small></div>
      <button class="btn" data-act="copy-review" data-k="${x.b.key}">Copy review prompt</button>
      <button class="btn" data-act="branch" data-k="${x.b.key}">Open ${esc(x.b.label)}</button>
    </div>`;
  }

  /* ---- board: in progress / ready for you / blocked — Claude's queue lives on its own page ---- */

  boardCols() {
    const m = this.mode();
    const filt = this.tabs.board || 'all';
    let pool = this.allOpen().filter((i) => !this.isSomeday(i) && this.setupMatch(i));
    pool = filt === 'all' ? this.modePool(pool) : pool.filter((i) => laneOf(i) === filt);
    const age = (i) => (i.p.lastNote ? daysSince(i.p.lastNote) : 9999);
    const byLane = (a, b) => (LANE_RANK[laneOf(a)] - LANE_RANK[laneOf(b)]) || (age(a) - age(b));
    const cols = { flight: [], you: [], waiting: [] };
    for (const i of pool) {
      if (i.p.hasBlocker) cols.waiting.push(i);
      else if (this.isInFlight(i)) cols.flight.push(i);
      else if (ownerOf(i) === 'me') cols.you.push(i);
    }
    cols.flight.sort((a, b) => age(a) - age(b));
    cols.you.sort(byLane); cols.waiting.sort(byLane);
    if (this.boardSort && this.boardSort !== 'default') for (const k of Object.keys(cols)) cols[k] = sortItems(cols[k], this.boardSort);
    return { filt, cols };
  }

  _bRow(i) {
    const b = BY_KEY[i.branch];
    const lane = laneOf(i);
    const n = realNote(i.p);
    const line = i.p.hasBlocker ? 'Waiting on: ' + this._waitLabel(i.p.blockedBy)
      : (n && !CREATED_ONLY_RE.test(n.text)) ? n.text : (i.p.nextStep ? 'Next: ' + i.p.nextStep : 'No next step yet');
    const d = n ? daysSince(n.date) : null;
    return `<div class="br" data-act="open" data-k="${i.branch}" data-u="${i.uid}">
      <div class="br-t"><span class="br-dot" style="background:${LANE_COLORS[lane] || '#8b9098'}"></span>${esc(i.summary)}</div>
      <div class="br-s"><span style="color:${b.color}">${esc(b.label)}</span> · ${esc(line)}</div>
      ${d != null ? `<span class="br-a">${d === 0 ? 'today' : d + 'd'}</span>` : ''}
    </div>`;
  }

  _board() {
    const { filt, cols } = this.boardCols();
    const chips = ['all', 'urgent', 'work', 'need', 'want'].map((l) =>
      `<button data-act="tab-board" data-v="${l}" class="${filt === l ? 'on' : ''}">${l === 'all' ? 'All lanes' : LANE_LABELS[l]}</button>`).join('');
    const col = (key, title, hint) => {
      const list = cols[key];
      const k = 'board_' + key;
      const lim = this.showMore[k] ? list.length : 6;
      return `<div class="bcol">
        <div class="bcol-h"><span>${title}</span><span class="mono">${list.length}</span></div>
        <div class="bcol-hint">${hint}</div>
        ${list.length ? list.slice(0, lim).map((i) => this._bRow(i)).join('') : '<div class="empty">Nothing here.</div>'}
        ${list.length > lim ? `<button class="btn" style="margin-top:6px" data-act="more" data-v="${k}">View all ${list.length}</button>` : ''}
      </div>`;
    };
    const sortSel = `<select id="mcBoardSort" title="Sort the board's columns">${SORTS.map(([k, l]) =>
      `<option value="${k}"${this.boardSort === k ? ' selected' : ''}>${k === 'default' ? 'Sort: default' : 'Sort: ' + esc(l)}</option>`).join('')}</select>`;
    return `<div class="card" style="margin-top:12px"><div class="tabs">${chips}${sortSel}</div>
      <div class="board3">
        ${col('flight', 'In progress', 'A real note in the last 14 days')}
        ${col('you', 'Ready for you', 'Hands-on, decisions, calls')}
        ${col('waiting', 'Blocked', 'Waiting on a card, a date or an event')}
      </div></div>`;
  }

  /* ---- pages: AI can handle / Someday shelf / Housekeeping / Past dates ---- */

  _pageHead(icon, color, title, sub, actions) {
    return `<div class="bh" style="margin-bottom:14px">
      <button class="btn" data-act="hub">← Hub</button>
      <ha-icon icon="${icon}" style="color:${color}"></ha-icon><h2>${esc(title)}</h2>
      <span class="muted">${sub}</span>${actions ? `<span style="margin-left:auto;display:flex;gap:8px">${actions}</span>` : ''}</div>`;
  }

  _claudeView() {
    const list = this.claudeList();
    const lim = this.showMore.claudeView ? list.length : 60;
    const rows = list.slice(0, lim).map((i) => this._row(i,
      `${laneOf(i) === 'urgent' ? '<span class="chip" style="background:#ff6b6b22;color:var(--danger)">URGENT</span>' : ''}
       <button class="btn" data-act="copy-init" data-k="${i.branch}" data-u="${i.uid}">Copy start prompt</button>`)).join('');
    return this._pageHead('mdi:creation', '#b07cf0', 'AI can handle', `${list.length} cards a Claude session can finish without you`,
        `<button class="btn pri" data-act="copy-brief">Copy session brief</button>`)
      + `<div class="card">${rows || '<div class="empty">Nothing queued for Claude.</div>'}
        ${list.length > lim ? `<button class="btn" style="margin-top:8px" data-act="more" data-v="claudeView">Show all ${list.length}</button>` : ''}</div>`;
  }

  _somedayView() {
    const list = this.somedayList();
    const by = {};
    for (const x of list) (by[x.i.branch] = by[x.i.branch] || []).push(x);
    const secs = BRANCHES.filter((b) => by[b.key]).map((b) => `<div class="card" style="margin-bottom:12px">
      <h3 class="lbl" style="color:${b.color}">${esc(b.label)} · ${by[b.key].length}</h3>
      ${by[b.key].map((x) => this._row(x.i, `<span class="ag">${x.d}d quiet</span>
        <button class="btn" data-act="bring-back" data-k="${x.i.branch}" data-u="${x.i.uid}">Bring back</button>`)).join('')}
    </div>`).join('');
    return this._pageHead('mdi:weather-night', 'var(--t2)', 'Someday shelf',
        `${list.length} parked — want cards quiet 30+ days and anything untouched ${SOMEDAY_DAYS}+ days. Nothing here is closed; a dated note brings a card back.`)
      + (secs || '<div class="card"><div class="empty">The shelf is empty.</div></div>');
  }

  _pastView() {
    const list = this.allOpen().map((i) => ({ i, d: this.dueIn(i) })).filter((x) => x.d != null && x.d < 0).sort((a, b) => a.d - b.d);
    return this._pageHead('mdi:calendar-alert', 'var(--warn)', 'Dates that have passed', 'Close the ones that happened; reschedule the rest.')
      + `<div class="card">${list.map((x) => this._row(x.i, `<span class="ag">${-x.d}d ago</span>
          <button class="btn" data-act="done" data-k="${x.i.branch}" data-u="${x.i.uid}">Done</button>`)).join('') || '<div class="empty">None.</div>'}</div>`;
  }

  _houseView() {
    const sync = SYNC_SOURCES.map((s) => {
      const d = stateAge(this._hass, `input_datetime.sync_${s.id}`);
      const c = d == null ? 'var(--danger)' : d < 2 ? 'var(--ok)' : d < 7 ? 'var(--warn)' : 'var(--danger)';
      return `<span class="s"><i style="background:${c}"></i>${esc(s.label)} <span class="mono" style="color:var(--t3)">${fmtAge(d)}</span></span>`;
    }).join('');
    const reviews = BRANCHES.map((b) => {
      const rev = stateAge(this._hass, `input_datetime.${b.key}_last_review`);
      const r = this.reviewDelta(b.key);
      const c = r.n >= REVIEW_MIN_CHANGES ? 'var(--warn)' : 'var(--ok)';
      return `<div class="row" data-act="branch" data-k="${b.key}">
        <span class="sb-rev" style="background:${c}"></span>
        <span class="t">${esc(b.label)} <span class="muted">· ${r.n === 0 ? 'nothing changed' : r.n + ' changed'}</span></span>
        <span class="meta"><span class="ag">${rev == null ? 'never' : fmtAge(rev)}</span>
        <button class="btn" data-act="copy-review" data-k="${b.key}">Copy review prompt</button></span></div>`;
    }).join('');
    return this._pageHead('mdi:broom', 'var(--t2)', 'Housekeeping', 'Format, overlaps, stale cards and reviews — kept off the hub.', '')
      + `<div class="grid">${this._attention()}
        <div style="display:flex;flex-direction:column;gap:16px">
          <div class="card"><h3 class="lbl">Branch reviews</h3><div class="muted" style="margin-bottom:6px">Due at ${REVIEW_MIN_CHANGES}+ cards changed since the last review — unchanged cards aren't reviewed again.</div>${reviews}</div>
          ${SYNC_SOURCES.length ? `<div class="card"><h3 class="lbl">Sync sources</h3><div class="sync" style="margin:0">${sync}</div></div>` : ''}
        </div></div>`;
  }

  /* Session brief — the panel's copy of brief.py, for pasting into a fresh Claude session. */
  _sessionBrief() {
    const all = this.allOpen();
    const row = (i, useNote) => {
      const n = i.p.notes[0];
      const txt = useNote && n ? n.text : (i.p.nextStep ? 'Next: ' + i.p.nextStep : '');
      return `- [${BY_KEY[i.branch].label} ${i.uid.slice(0, 8)}] ${i.summary}${txt ? ' — ' + txt.slice(0, 90) : ''}`;
    };
    const fl = all.filter((i) => this.isInFlight(i)).sort((a, b) => (b.p.lastNote || '').localeCompare(a.p.lastNote || ''));
    const urg = all.filter((i) => laneOf(i) === 'urgent');
    const cl = this.claudeList();
    return [
      `MISSION CONTROL BRIEF ${ymd()} — ${all.length} open · ${fl.length} in progress · ${urg.length} urgent · ${cl.length} Claude can do · ${this.somedayList().length} on the Someday shelf`,
      '', 'IN PROGRESS (resume point = latest note):', ...fl.slice(0, 10).map((i) => row(i, true)),
      '', 'URGENT:', ...urg.map((i) => row(i, false)),
      '', 'AI CAN HANDLE (top 8):', ...cl.slice(0, 8).map((i) => row(i, false)),
      '', 'Pick one card, open it by uid (WebSocket mission_control/items/get), work it, and finish with one dated "Stopped: … Next: … Blocked: …" note.',
    ].join('\n');
  }

  /* ---- toolbox ---- */

  tbOf(i) { return i.p.toolbox || (this.tbMap[i.uid] && this.tbMap[i.uid].t) || ''; }
  buyOf(i) { return i.p.buyFirst || (this.tbMap[i.uid] && this.tbMap[i.uid].b) || ''; }
  tbSource(i) { const m = this.tbMap[i.uid]; return (i.p.toolbox || i.p.buyFirst) ? 'tracker' : (m && (m.t || m.b) ? 'proposed' : ''); }

  async _loadToolboxMap() {
    this.tbMap = {};  /* TOOLBOX lives on the card itself */
  }

  _inToolbox(i, def) {
    const t = this.tbOf(i);
    return def.visit ? /\bvisit$/i.test(t) : t === def.k;
  }

  _toolbox() {
    const open = this.allOpen();
    const sel = this.tabs.toolbox;
    const chip = (d, n) => `<button class="tb-chip${sel === d.k ? ' on' : ''}${n ? '' : ' zero'}" data-act="tab-tb" data-v="${esc(d.k)}" style="--c:${d.color}">
      <ha-icon icon="${d.icon}"></ha-icon><span>${esc(d.k)}</span><b>${n}</b></button>`;
    const tbChips = TOOLBOXES.map((d) => chip(d, open.filter((i) => this._inToolbox(i, d)).length)).join('');
    const buyChips = BUY_RUNS.map((d) => chip(d, open.filter((i) => this.buyOf(i) === d.k).length)).join('');
    const proposed = open.filter((i) => this.tbSource(i) === 'proposed').length;

    let body;
    if (!sel) {
      body = '<div class="empty">Pick what\'s out — everything that needs the same setup shows up together.</div>';
    } else {
      const buy = BUY_RUNS.find((d) => d.k === sel);
      const def = TOOLBOXES.find((d) => d.k === sel);
      const list = buy ? open.filter((i) => this.buyOf(i) === sel) : open.filter((i) => this._inToolbox(i, def));
      const sec = (t, arr, note) => arr.length
        ? `<div class="tb-grp">${esc(t)} <span class="cnt">${arr.length}${note ? ' — ' + esc(note) : ''}</span></div>` +
          arr.map((i) => this._tbRow(i, !!buy)).join('')
        : '';
      if (buy) {
        body = sec(sel, list, 'one trip') || '<div class="empty">Nothing waiting on this.</div>';
      } else if (def.visit) {
        const by = {};
        for (const i of list) (by[this.tbOf(i)] = by[this.tbOf(i)] || []).push(i);
        body = Object.keys(by).sort((a, b) => by[b].length - by[a].length)
          .map((t) => sec(t, by[t], by[t].length > 1 ? 'one trip' : '')).join('') || '<div class="empty">Nothing here.</div>';
      } else {
        const ready = list.filter((i) => !i.p.hasBlocker && !this.buyOf(i));
        const needBuy = list.filter((i) => !i.p.hasBlocker && this.buyOf(i));
        const blocked = list.filter((i) => i.p.hasBlocker);
        body = (sec('Ready now', ready) + sec('Buy first', needBuy, 'parts not in hand') + sec('Blocked', blocked))
          || '<div class="empty">Nothing here.</div>';
      }
    }
    return `<div class="card"><h3 class="lbl">Toolbox${proposed
        ? ` <span class="tb-note">· ${proposed} placements proposed, not yet written to the trackers</span>` : ''}</h3>
      <div class="tb-row">${tbChips}</div>
      <div class="tb-row" style="margin-top:6px">${buyChips}</div>
      <div style="margin-top:12px">${body}</div></div>`;
  }

  _tbRow(i, showToolbox) {
    const b = BY_KEY[i.branch];
    const t = this.tbOf(i), buy = this.buyOf(i);
    return `<div class="row" data-act="open" data-k="${i.branch}" data-u="${i.uid}">
      <span class="ck"></span><span class="t">${esc(i.summary)}</span>
      <span class="meta">
        ${showToolbox && t && t !== 'none' ? `<span class="chip" style="background:rgba(255,255,255,.06);color:var(--t2)">${esc(t)}</span>` : ''}
        ${!showToolbox && buy ? `<span class="chip" style="background:#f0b35a22;color:var(--warn)">${esc(buy)}</span>` : ''}
        <span class="chip" style="background:${b.color}22;color:${b.color}">${esc(b.label)}</span>
      </span></div>`;
  }

  _tbOptions(cur) {
    const visits = new Set();
    for (const v of Object.values(this.tbMap)) if (/\bvisit$/i.test(v.t || '')) visits.add(v.t);
    for (const i of this.allOpen()) { const t = this.tbOf(i); if (/\bvisit$/i.test(t)) visits.add(t); }
    const vals = ['none', ...TOOLBOX_VALUES, ...[...visits].sort()];
    if (cur && !vals.includes(cur)) vals.push(cur);
    const c = cur || 'none';
    return vals.map((v) => `<option value="${esc(v)}"${v === c ? ' selected' : ''}>${v === 'none' ? '— none —' : esc(v)}</option>`).join('');
  }

  _today() {
    const now = Date.now();
    const rows = this.events.slice(0, 3).map((e) => {
      const startRaw = (e.start && (e.start.dateTime || e.start.date)) || e.start;
      const endRaw = (e.end && (e.end.dateTime || e.end.date)) || e.end;
      const s = Date.parse(startRaw), en = Date.parse(endRaw);
      const allDay = typeof startRaw === 'string' && startRaw.length <= 10;
      let color = 'var(--t2)';
      if (!allDay && s <= now && en > now) color = 'var(--danger)';
      else if (!allDay && s - now < 12 * 3600000) color = 'var(--warn)';
      const tm = allDay ? 'all day'
        : new Date(s).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      const day = new Date(s).toDateString() === new Date().toDateString()
        ? '' : ' ' + new Date(s).toLocaleDateString([], { month: 'numeric', day: 'numeric' });
      return `<div class="ev"><span class="tm" style="color:${color}">${esc(tm + day)}</span>
        <span style="flex:1">${esc(e.summary || '(no title)')}</span></div>`;
    }).join('');
    return `<div class="card"><h3 class="lbl">Today</h3>${rows || '<div class="empty">Nothing on the calendar.</div>'}</div>`;
  }

  _attention() {
    const ov = this.overlaps();
    const st = this.stale();
    const lf = this.lintList();
    const tab = this.tabs.attention;

    const tabs = [
      ['overlap', `Overlapping ${ov.length}`],
      ['stale', `Stale ${st.length}`],
      ['format', `Format ${lf.length}`],
    ].map(([k, l]) => `<button data-act="tab-att" data-v="${k}" class="${tab === k ? 'on' : ''}">${esc(l)}</button>`).join('');

    let body = '';
    if (tab === 'overlap') {
      body = ov.length ? ov.slice(0, 8).map((p) => `<div class="row" data-act="open" data-k="${p.a.branch}" data-u="${p.a.uid}">
          <span class="t">${esc(p.a.summary)}<br><span class="muted">↔ ${esc(p.b.summary)}</span></span>
          <span class="meta"><span class="mono" style="font-size:10.5px;color:var(--t3)">${Math.round(p.c * 100)}%</span>
          <button class="btn" data-act="compare" data-k="${p.a.branch}" data-u="${p.a.uid}" data-k2="${p.b.branch}" data-u2="${p.b.uid}">Compare</button></span>
        </div>`).join('')
        : '<div class="empty">No pairs above the 0.34 overlap threshold.</div>';
    } else if (tab === 'format') {
      const lim = this.showMore.lint ? lf.length : 12;
      body = lf.length ? `<div class="muted" style="margin-bottom:6px">Open cards that drift from the lean card. Open one to fix it.</div>`
        + lf.slice(0, lim).map((x) => {
          const b = BY_KEY[x.i.branch];
          return `<div class="row" data-act="open" data-k="${x.i.branch}" data-u="${x.i.uid}">
            <span class="ck"></span>
            <span class="t">${esc(x.i.summary)}<br><span class="lint-i">${x.issues.map(esc).join(' · ')}</span></span>
            <span class="meta"><span class="chip" style="background:${b.color}22;color:${b.color}">${esc(b.label)}</span></span>
          </div>`;
        }).join('')
        + (lf.length > lim ? `<button class="btn" style="margin-top:8px" data-act="more" data-v="lint">Show all ${lf.length}</button>` : '')
        : '<div class="empty">Every open card matches the lean card.</div>';
    } else {
      const und = this.undocumented();
      const lim = this.showMore.undoc ? und.length : 0;
      body = (st.length ? st.slice(0, 10).map((x) => this._row(x.i,
        `<span class="ag">${x.d}d</span>` +
        `<button class="btn" data-act="open" data-k="${x.i.branch}" data-u="${x.i.uid}">Open</button>`)).join('')
        : '<div class="empty">Every item with a dated PROGRESS note has been touched in the last 30 days.</div>')
        + (und.length ? `<div style="margin-top:12px;padding-top:11px;border-top:1px solid var(--line)">
            <div class="muted">${und.length} open items carry no dated PROGRESS note at all — undocumented rather than stale.</div>
            ${lim ? und.slice(0, 40).map((i) => this._row(i)).join('')
                  : `<button class="btn" style="margin-top:8px" data-act="more" data-v="undoc">Show them</button>`}
          </div>` : '');
    }

    return `<div class="card"><h3 class="lbl">Needs attention</h3><div class="tabs">${tabs}</div>${body}</div>`;
  }

  _searchResults() {
    const q = this.search.trim().toLowerCase();
    const toks = q.split(/\s+/).filter(Boolean);
    const pool = [];
    for (const b of BRANCHES) {
      const d = this.data[b.key];
      if (!d) continue;
      pool.push(...d.open);
      if (this.searchDone) pool.push(...d.done);
    }
    const hits = [];
    for (const i of pool) {
      const title = i.summary.toLowerCase();
      const hay = title + '\n' + (i.description || '').toLowerCase() + '\n' + (BY_KEY[i.branch].label || '').toLowerCase()
        + '\n' + this.tagsOf(i).map((t) => (BY_KEY[t] || {}).label || t).join(' ').toLowerCase();
      if (!toks.every((t) => hay.includes(t))) continue;
      hits.push({ i, inTitle: toks.every((t) => title.includes(t)) });
    }
    let list;
    if (this.sort === 'default') {
      hits.sort((a, b) => (b.inTitle - a.inTitle) || (b.i.p.lastNote || '').localeCompare(a.i.p.lastNote || ''));
      list = hits.map((h) => h.i);
    } else list = sortItems(hits.map((h) => h.i), this.sort);
    const key = 'search';
    const lim = this.showMore[key] ? list.length : 60;
    const rows = list.slice(0, lim).map((i) => {
      const c = uuidCreated(i.uid);
      const d = i.p.lastNote == null ? null : daysSince(i.p.lastNote);
      const extra = `${i.status === 'completed' ? '<span class="chip" style="background:#8b909822;color:var(--t3)">Done</span>' : ''}
        <span class="ag" title="created · last note">${c != null ? esc(fmtCreated(c)) : '—'} · ${d == null ? 'no notes' : d + 'd'}</span>`;
      return this._row(i, extra);
    }).join('');
    const doneN = BRANCHES.reduce((n, b) => n + ((this.data[b.key] && this.data[b.key].done.length) || 0), 0);
    return `<div class="card">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px">
        <h3 class="lbl" style="margin:0">Search · ${list.length} match${list.length === 1 ? '' : 'es'}</h3>
        <span class="muted">"${esc(this.search.trim())}" across all 8 trackers${this.searchDone ? ' incl. done' : ''}</span>
        <button class="btn" style="margin-left:auto" data-act="search-done">${this.searchDone ? 'Hide done' : `Include done (${doneN})`}</button>
        <button class="btn" data-act="search-clear">Clear</button>
      </div>
      ${rows || '<div class="empty">No matches.</div>'}
      ${list.length > lim ? `<div style="padding-top:10px"><button class="btn" data-act="more" data-v="${key}">Show ${list.length - lim} more</button></div>` : ''}
    </div>`;
  }

  _row(i, extra) {
    const b = BY_KEY[i.branch];
    const d = i.p.lastNote == null ? null : daysSince(i.p.lastNote);
    return `<div class="row" data-act="open" data-k="${i.branch}" data-u="${i.uid}">
      <span class="ck"></span>
      <span class="t">${esc(i.summary)}</span>
      <span class="meta">
        <span class="chip" style="background:${b.color}22;color:${b.color}">${esc(b.label)}</span>
        ${extra || `<span class="ag">${d == null ? '—' : d + 'd'}</span>`}
      </span>
    </div>`;
  }

  _lanes() {
    const m = this.mode();
    const def = m.work ? 'work' : 'in_progress';
    const tab = this.tabs.lanes || def;
    const open = this.allOpen();
    const effLane = (i) => i.p.lane || (WORK_TRACKERS.has(i.branch) ? 'work' : '');

    const tabs = [
      ['in_progress', 'In progress'], ['urgent', 'Urgent'], ['work', 'Work'],
      ['need', 'Need'], ['want', 'Want'],
    ].map(([k, l]) => `<button data-act="tab-lane" data-v="${k}" class="${tab === k ? 'on' : ''}">${esc(l)}</button>`).join('');

    let body;
    {
      const list = tab === 'in_progress'
        ? open.filter((i) => groupOf(i.p) === 'in_progress')
        : open.filter((i) => effLane(i) === tab);
      const sorted = this.sort === 'default'
        ? list.sort((a, b) => (b.p.lastNote || '').localeCompare(a.p.lastNote || ''))
        : sortItems(list, this.sort);
      const key = 'lane_' + tab;
      const lim = this.showMore[key] ? sorted.length : 12;
      body = sorted.length
        ? sorted.slice(0, lim).map((i) => this._row(i)).join('') +
          (sorted.length > lim ? `<div style="padding-top:10px"><button class="btn" data-act="more" data-v="${key}">Show ${sorted.length - lim} more</button></div>` : '')
        : '<div class="empty">Nothing here.</div>';
    }

    return `<div class="card"><h3 class="lbl">Lanes</h3><div class="tabs">${tabs}</div>${body}</div>`;
  }

  /* ---- branch board ---- */

  _branchBoard() {
    const k = this.view.branch;
    const b = BY_KEY[k];
    const d = this.data[k] || { open: [], done: [] };
    const h = this._hass;
    const rev = stateAge(h, `input_datetime.${k}_last_review`);

    const tagged = this.taggedInto(k, false);
    const open = d.open.concat(tagged);
    const cols = [
      ['In progress', open.filter((i) => groupOf(i.p) === 'in_progress')],
      ['Next up', open.filter((i) => groupOf(i.p) === 'next_up')],
      ['Blocked', open.filter((i) => groupOf(i.p) === 'blocked')],
      ['Done recently', d.done.slice(0, 40)],
    ];

    const colHtml = cols.map(([title, arr]) => {
      const sorted = title === 'Done recently' ? arr
        : this.sort === 'default' ? arr.slice().sort((x, y) => x.p.priNum - y.p.priNum) : sortItems(arr, this.sort);
      const key = 'col_' + k + '_' + title;
      const lim = this.showMore[key] ? sorted.length : 6;
      return `<div class="col">
        <h4 class="lbl">${esc(title)} <span class="cnt">${sorted.length}</span></h4>
        ${sorted.slice(0, lim).map((i) => this._itemCard(i, b, k)).join('') || '<div class="empty">—</div>'}
        ${sorted.length > lim ? `<button class="btn" data-act="more" data-v="${esc(key)}">Show ${sorted.length - lim} more</button>` : ''}
      </div>`;
    }).join('');

    return `<div class="bh">
        <button class="btn" data-act="hub">← Hub</button>
        <ha-icon icon="${b.icon}" style="color:${b.color}"></ha-icon>
        <h2>${esc(b.label)}</h2>
        <span class="muted">${d.open.length} open${tagged.length ? ` <span class="via" title="tagged into this branch from another tracker">+${tagged.length} tagged</span>` : ''}</span>
        <span class="sb-rev" style="background:${this._reviewColor(rev)}"></span>
        <span class="muted">Reviewed ${fmtAge(rev)}</span>
        <button class="btn" data-act="copy-review" data-k="${k}">Copy review prompt</button>

        ${this.branchView !== 'table' ? `<select id="mcSort" title="Sort the columns" style="margin-left:auto">${SORTS.map(([k2, l]) =>
          `<option value="${k2}"${this.sort === k2 ? ' selected' : ''}>${k2 === 'default' ? 'Sort: default' : 'Sort: ' + esc(l)}</option>`).join('')}</select>` : ''}
        <span class="bt-seg"${this.branchView !== 'table' ? ' style="margin-left:0"' : ''}>
          <button class="${this.branchView === 'table' ? 'on' : ''}" data-act="bt-view" data-v="table"><ha-icon icon="mdi:table"></ha-icon>Table</button>
          <button class="${this.branchView !== 'table' ? 'on' : ''}" data-act="bt-view" data-v="board"><ha-icon icon="mdi:view-column-outline"></ha-icon>Board</button>
        </span>
      </div>
      ${this.qaOpen ? this._quickAdd(k) : this._qaFeed()}
      ${this.branchView === 'table' ? this._branchTable(k) : `<div class="cols">${colHtml}</div>`}`;
  }

  /* ---- branch table / spreadsheet view (2026-09-25) ---- */

  btStatus(i) { return i.status === 'completed' ? 'done' : groupOf(i.p); }

  /* Whose move a row is (2026-11-24): blocked → Waiting, else the owner. */
  btWho(i) { return i.status === 'completed' ? 'done' : i.p.hasBlocker ? 'wait' : ownerOf(i); }

  /* Card uid prefix → card (open or done, any tracker). */
  findPrefix(tok) {
    const t = (tok || '').toLowerCase();
    for (const b of BRANCHES) {
      const d = this.data[b.key];
      if (!d) continue;
      const hit = d.open.find((i) => i.uid.startsWith(t)) || d.done.find((i) => i.uid.startsWith(t));
      if (hit) return hit;
    }
    return null;
  }

  /* BLOCKED BY as words: each uid becomes the blocking card's title (plain text — escape at use). */
  _waitLabel(text) {
    return (text || '').replace(UID_TOKEN_G, (m) => {
      const c = this.findPrefix(m.slice(0, 8));
      if (!c) return m;
      const t = c.summary.length > 48 ? c.summary.slice(0, 47) + '…' : c.summary;
      return `"${t}"${c.status === 'completed' ? ' (done)' : ''}`;
    });
  }

  /* Cards that share a trade-visit toolbox ride one trip: collapse them into one bundle row. */
  _btBundles(rows) {
    const vis = (i) => (i.status !== 'completed' && / visit$/i.test(i.p.toolbox || '') ? i.p.toolbox : null);
    const by = {};
    for (const i of rows) { const t = vis(i); if (t) (by[t] = by[t] || []).push(i); }
    const out = [], seen = new Set();
    for (const i of rows) {
      const t = vis(i);
      if (t && by[t].length > 1) {
        if (seen.has(t)) continue;
        seen.add(t);
        const items = by[t];
        const free = items.find((x) => !x.p.hasBlocker);
        out.push({ bundle: t, items, who: free ? ownerOf(free) : 'wait' });
      } else out.push({ i, who: this.btWho(i) });
    }
    return out;
  }

  /* Overdue cards pinned above the branch table, most late first. */
  _btOverdue(k) {
    const d = this.data[k] || { open: [] };
    const od = d.open.concat(this.taggedInto(k, false))
      .map((i) => ({ i, d: this.dueIn(i) })).filter((x) => x.d != null && x.d < 0).sort((a, b) => a.d - b.d);
    if (!od.length) return '';
    const key = 'od_' + k;
    const lim = this.showMore[key] ? od.length : 5;
    return `<div class="bt-od"><div class="bt-od-h">Overdue <b>${od.length}</b></div>
      ${od.slice(0, lim).map(({ i, d: n }) => `<div class="bt-od-r" data-act="open" data-k="${i.branch}" data-u="${i.uid}">
        <span class="late">${-n}d late</span><span class="t">${esc(i.summary)}</span>
        <span class="ns">${i.p.hasBlocker ? 'Waiting: ' + esc(this._waitLabel(i.p.blockedBy)) : esc(i.p.nextStep || '')}</span></div>`).join('')}
      ${od.length > lim ? `<button class="btn" data-act="more" data-v="${key}">Show ${od.length - lim} more</button>` : ''}</div>`;
  }

  btAge(i) {
    if (i.p.lastNote) return daysSince(i.p.lastNote);
    const c = uuidCreated(i.uid);
    return c == null ? null : Math.floor((Date.now() - c) / 86400000);
  }

  _btCols() {
    const on = this.bt.cols;
    return BT_COLS.filter((c) => (Array.isArray(on) ? on.includes(c.k) : c.def));
  }

  _btPool(k) {
    const d = this.data[k] || { open: [], done: [] };
    const bt = this.bt;
    const open = d.open.concat(this.taggedInto(k, false));
    const base = bt.done ? open.concat(d.done, this.taggedInto(k, true)) : open;
    const q = bt.q.trim().toLowerCase();
    const filtered = base.filter((i) =>
      (!bt.status || this.btStatus(i) === bt.status) &&
      (!bt.lane || (laneOf(i) || 'none') === bt.lane) &&
      (!bt.owner || ownerOf(i) === bt.owner) &&
      (!q || [i.summary, i.p.nextStep, i.p.details, i.p.toolbox, i.uid].join('\n').toLowerCase().includes(q)));
    const counts = { all: filtered.length, in_progress: 0, next_up: 0, blocked: 0, done: 0 };
    for (const i of filtered) counts[this.btStatus(i)]++;
    const rows = bt.tab === 'all' ? filtered.slice() : filtered.filter((i) => this.btStatus(i) === bt.tab);
    const SO = { in_progress: 0, next_up: 1, blocked: 2, done: 3 };
    const keyOf = {
      title: (i) => i.summary.toLowerCase(),
      status: (i) => SO[this.btStatus(i)],
      lane: (i) => LANE_RANK[laneOf(i)],
      due: (i) => { const d = this.dueIn(i); return d == null ? 1e9 : d; },
      owner: (i) => ownerOf(i),
      next: (i) => (i.p.hasBlocker ? i.p.blockedBy : i.p.nextStep || '').toLowerCase(),
      toolbox: (i) => (this.tbOf(i) || '').toLowerCase(),
      effort: (i) => (i.p.effortMin == null ? 1e9 : i.p.effortMin),
      cost: (i) => (i.p.costNum == null ? -1 : i.p.costNum),
      created: (i) => uuidCreated(i.uid) || 0,
      updated: (i) => { const a = this.btAge(i); return a == null ? -99999 : -a; },
      note: (i) => { const n = realNote(i.p); return n ? n.date : ''; },
    }[bt.sort] || ((i) => i.summary.toLowerCase());
    const dir = bt.dir === 'asc' ? 1 : -1;
    rows.sort((a, b) => {
      const x = keyOf(a), y = keyOf(b);
      return (x < y ? -1 : x > y ? 1 : 0) * dir || a.summary.localeCompare(b.summary);
    });
    return { rows, counts };
  }

  _branchTable(k) {
    const bt = this.bt;
    const { rows, counts } = this._btPool(k);
    const cols = this._btCols();
    const per = bt.per || 25;
    let disp = this._btBundles(rows);
    const grouped = bt.group !== 'none';
    if (grouped) disp = disp.map((x, n) => ({ ...x, n })).sort((a, b) => (WHO_RANK[a.who] - WHO_RANK[b.who]) || a.n - b.n);
    const gCount = {};
    for (const x of disp) gCount[x.who] = (gCount[x.who] || 0) + (x.bundle ? x.items.length : 1);
    const pages = Math.max(1, Math.ceil(disp.length / per));
    bt.page = Math.min(Math.max(0, bt.page), pages - 1);
    const start = bt.page * per;
    const dslice = disp.slice(start, start + per);
    const slice = [];
    for (const x of dslice) { if (x.bundle) { if (bt.openB.has(x.bundle)) slice.push(...x.items); } else slice.push(x.i); }
    const cur = this.drawer ? this.drawer.uid : null;
    const sel = bt.sel;
    const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
    const pill = (txt, c) => `<span class="pill" style="background:${c}1f;color:${c};border-color:${c}38">${esc(txt)}</span>`;
    const opt = (v0, list) => list.map(([v, l]) => `<option value="${v}"${v0 === v ? ' selected' : ''}>${esc(l)}</option>`).join('');
    const lanes = WORK_TRACKERS.has(k) ? ['urgent', 'work', 'need', 'want'] : ['urgent', 'need', 'want'];
    const statusOpts = [['', 'Status'], ['in_progress', 'In progress'], ['next_up', 'Next up'], ['blocked', 'Blocked']].concat(bt.done ? [['done', 'Done']] : []);
    const laneOpts = [['', 'Priority']].concat(lanes.map((l) => [l, cap(l)]));
    const ownOpts = [['', 'Who can do it'], ['me', 'You'], ['claude', 'Claude']];
    const any = bt.q || bt.status || bt.lane || bt.owner;
    const pageUids = slice.map((i) => i.uid);
    const nSel = pageUids.filter((u) => sel.has(u)).length;
    const allBox = nSel === 0 ? '' : nSel === pageUids.length ? ' on' : ' mid';

    const tabs = [['all', bt.done ? 'All' : 'All open'], ['in_progress', 'In progress'], ['next_up', 'Next up'], ['blocked', 'Blocked']]
      .map(([v, l]) => `<button class="bt-tab${bt.tab === v ? ' on' : ''}" data-act="bt-tab" data-v="${v}">${l} <span>${counts[v]}</span></button>`).join('');
    const th = (sk, label) => `<th class="${bt.sort === sk ? 'on' : ''}" data-act="bt-sort" data-v="${sk}">${esc(label)}${bt.sort === sk ? `<span class="ar">${bt.dir === 'asc' ? '↑' : '↓'}</span>` : ''}</th>`;
    const dash = '<span class="muted">—</span>';
    const cell = (c, i) => {
      const s = this.btStatus(i);
      switch (c.k) {
        case 'status': return `<td>${pill(BT_STATUS[s][0], BT_STATUS[s][1])}</td>`;
        case 'lane': { const l = laneOf(i); return `<td>${l ? pill(cap(l), LANE_COLORS[l]) : dash}</td>`; }
        case 'owner': return `<td class="c">${ownerOf(i) === 'claude' ? 'Claude' : 'You'}</td>`;
        case 'due': {
          const d = this.dueIn(i);
          if (d == null) return `<td class="a">${dash}</td>`;
          const txt = d < 0 ? `<span style="color:var(--danger)">${-d}d late</span>` : d === 0 ? '<span style="color:var(--warn)">today</span>'
            : d <= 7 ? esc(this._dayLabel(d)) : esc(String(i.due).slice(0, 10));
          return `<td class="a" title="${esc(String(i.due).slice(0, 10))}">${txt}</td>`;
        }
        case 'next': {
          const w = i.p.hasBlocker ? this._waitLabel(i.p.blockedBy) : '';
          const txt = i.p.hasBlocker ? 'Waiting: ' + w : i.p.nextStep;
          return `<td class="n" title="${esc(txt)}">${i.p.hasBlocker ? `<span style="color:var(--danger)">Waiting:</span> ${esc(w)}` : (esc(i.p.nextStep) || dash)}</td>`;
        }
        case 'note': {
          const n = realNote(i.p);
          const top = i.p.notes[0];
          const sys = top && SYSTEM_NOTE_RE.test(top.text) && top !== n ? top : null;
          const age = n ? daysSince(n.date) : null;
          const mark = sys ? `<span class="via" title="${esc(sys.date + ' — ' + sys.text)}">⚙</span>` : '';
          return `<td class="n nt" title="${esc(n ? n.date + ' — ' + n.text : '')}">${n ? `<span class="ag">${age <= 0 ? 'today' : age + 'd'}</span> ${esc(n.text)}` : dash}${mark}</td>`;
        }
        case 'toolbox': { const t = this.tbOf(i); return `<td class="c">${t && t !== 'none' ? esc(t) : dash}</td>`; }
        case 'effort': return `<td class="a" title="${esc(i.p.effort)}">${i.p.effort ? esc(i.p.effort) : dash}</td>`;
        case 'cost': return `<td class="a">${i.p.cost ? esc(i.p.cost) : dash}</td>`;
        case 'created': return `<td class="a">${esc(fmtCreated(uuidCreated(i.uid))) || dash}</td>`;
        case 'updated': { const a = this.btAge(i); return `<td class="a">${a == null ? dash : a <= 0 ? 'today' : a + 'd'}</td>`; }
        default: return '<td></td>';
      }
    };
    const rowHtml = (i, member) => `<tr class="r${member ? ' bm' : ''}${i.uid === cur ? ' cur' : ''}${sel.has(i.uid) ? ' sel' : ''}" data-act="open" data-k="${i.branch}" data-u="${i.uid}">
        <td class="x" data-act="bt-sel" data-u="${i.uid}"><span class="box${sel.has(i.uid) ? ' on' : ''}"></span></td>
        <td class="t" title="${esc(i.summary)}"${i.status === 'completed' ? ' style="color:var(--t3);text-decoration:line-through"' : ''}>${esc(i.summary)}${i.p.repeat ? `<span class="via" title="repeats ${esc(i.p.repeat)}">↻</span>` : ''}${i.branch !== k ? `<span class="via">via ${esc(BY_KEY[i.branch].label)}</span>` : ''}</td>
        ${cols.map((c) => cell(c, i)).join('')}
        <td class="x"><button class="dots" data-act="copy-init" data-k="${i.branch}" data-u="${i.uid}" title="Copy start prompt"><ha-icon icon="mdi:content-copy"></ha-icon></button></td>
      </tr>`;
    const LR = { urgent: 0, work: 1, need: 2, want: 3 };
    const bundleHtml = (x) => {
      const open = bt.openB.has(x.bundle);
      const its = x.items;
      const effs = its.map((i) => i.p.effortMin).filter((v) => v != null);
      const costs = its.map((i) => i.p.costNum).filter((v) => v != null);
      const dues = its.map((i) => this.dueIn(i)).filter((v) => v != null);
      const lane = its.map(laneOf).filter(Boolean).sort((a, b) => LR[a] - LR[b])[0];
      const due = dues.length ? Math.min(...dues) : null;
      const bcell = (c) => {
        switch (c.k) {
          case 'status': return `<td>${pill('Bundle', '#c79bff')}</td>`;
          case 'lane': return `<td>${lane ? pill(cap(lane), LANE_COLORS[lane]) : dash}</td>`;
          case 'due': return `<td class="a">${due == null ? dash : due < 0 ? `<span style="color:var(--danger)">${-due}d late</span>` : due === 0 ? '<span style="color:var(--warn)">today</span>' : esc(this._dayLabel(due))}</td>`;
          case 'next': { const t = its.map((i) => i.summary).join(' · '); return `<td class="n" title="${esc(t)}">${esc(t)}</td>`; }
          case 'toolbox': return `<td class="c">${esc(x.bundle)}</td>`;
          case 'effort': return `<td class="a">${effs.length ? esc(fmtMin(effs.reduce((a, b) => a + b, 0))) : dash}</td>`;
          case 'cost': return `<td class="a">${costs.length ? '$' + costs.reduce((a, b) => a + b, 0).toLocaleString() : dash}</td>`;
          default: return `<td class="a">${dash}</td>`;
        }
      };
      return `<tr class="r bn" data-act="bt-bundle" data-v="${esc(x.bundle)}">
        <td class="x"><ha-icon icon="mdi:chevron-${open ? 'down' : 'right'}" style="--mdc-icon-size:16px"></ha-icon></td>
        <td class="t"><b>${esc(x.bundle)}</b><span class="via">${its.length} cards · one trip</span></td>
        ${cols.map(bcell).join('')}<td class="x"></td></tr>` + (open ? its.map((i) => rowHtml(i, true)).join('') : '');
    };
    let lastWho = null;
    const parts = [];
    for (const x of dslice) {
      if (grouped && x.who !== lastWho) {
        lastWho = x.who;
        parts.push(`<tr class="gh"><td colspan="${cols.length + 3}">${esc(WHO_LABEL[x.who] || x.who)} <span>${gCount[x.who]}</span></td></tr>`);
      }
      parts.push(x.bundle ? bundleHtml(x) : rowHtml(x.i, false));
    }
    const body = parts.length ? parts.join('')
      : `<tr><td colspan="${cols.length + 3}"><div class="empty">${any ? 'Nothing matches these filters.' : 'Nothing here.'}</div></td></tr>`;

    const bulk = sel.size ? `<div class="bt-bulk"><b>${sel.size} selected</b>
        <span class="muted">· Priority</span>${lanes.map((l) => `<button class="btn" data-act="bt-bulk" data-v="lane:${l}">${cap(l)}</button>`).join('')}
        <span class="muted">· Who</span><button class="btn" data-act="bt-bulk" data-v="owner:me">You</button><button class="btn" data-act="bt-bulk" data-v="owner:claude">Claude</button>
        <span class="muted">·</span><button class="btn pri" data-act="bt-bulk" data-v="done">Mark done</button>
        <button class="bt-link" data-act="bt-selclear" style="margin-left:auto">Clear selection</button></div>` : '';
    const colMenu = bt.menu ? `<div class="bt-menu">${BT_COLS.map((c) => {
        const on = cols.some((x) => x.k === c.k);
        return `<button data-act="bt-col" data-v="${c.k}"><span class="box${on ? ' on' : ''}"></span>${esc(c.label)}</button>`;
      }).join('')}<button data-act="bt-col" data-v="__reset" style="color:var(--t2)">Reset to default</button></div>` : '';

    return `${this._btOverdue(k)}<div class="bt-top">${tabs}</div>
      <div class="bt-fil">
        <label class="srch"><ha-icon icon="mdi:magnify"></ha-icon><input id="btSearch" autocomplete="off" placeholder="Search ${esc(BY_KEY[k].label)} tasks…" value="${esc(bt.q)}"></label>
        <select id="btStatus" class="bt-sel${bt.status ? ' on' : ''}">${opt(bt.status, statusOpts)}</select>
        <select id="btLane" class="bt-sel${bt.lane ? ' on' : ''}">${opt(bt.lane, laneOpts)}</select>
        <select id="btOwner" class="bt-sel${bt.owner ? ' on' : ''}">${opt(bt.owner, ownOpts)}</select>
        <select id="btGroup" class="bt-sel" title="Group rows">${opt(bt.group === 'none' ? 'none' : 'who', [['who', 'Group: whose move'], ['none', 'No grouping']])}</select>
        ${any ? '<button class="bt-link" data-act="bt-clear">Clear</button>' : ''}
        <span style="flex:1"></span>
        <span class="bt-colm"><button class="btn" data-act="bt-colmenu"><ha-icon icon="mdi:view-column-outline" style="--mdc-icon-size:15px"></ha-icon> Columns</button>${colMenu}</span>
        <button class="bt-chk" data-act="bt-done"><span class="box${bt.done ? ' on' : ''}"></span>Show completed</button>
      </div>
      ${bulk}
      <div class="bt-wrap"><table class="bt">
        <thead><tr>
          <th class="x" data-act="bt-selall" data-v="${pageUids.join(',')}"><span class="box${allBox}"></span></th>
          ${th('title', 'Task')}
          ${cols.map((c) => th(c.k, c.label)).join('')}
          <th class="x"></th>
        </tr></thead>
        <tbody>${body}</tbody>
      </table></div>
      <div class="bt-foot">
        <span>${disp.length ? `${start + 1}–${start + dslice.length} of ${disp.length} rows · ${rows.length} tasks` : '0 tasks'}</span>
        <span class="muted">· Click a column title to sort · click a row for details</span>
        <span style="flex:1"></span>
        <span>Rows per page</span>
        <select id="btPer" class="bt-sel">${opt(String(per), [['25', '25'], ['50', '50'], ['100', '100']])}</select>
        <button class="btn" data-act="bt-page" data-v="-1"${bt.page === 0 ? ' disabled' : ''}>‹ Previous</button>
        <button class="btn" data-act="bt-page" data-v="1"${bt.page >= pages - 1 ? ' disabled' : ''}>Next ›</button>
      </div>`;
  }

  _itemCard(i, b, pageKey) {
    const lane = i.p.lane || (WORK_TRACKERS.has(i.branch) ? 'work' : '');
    const lc = LANE_COLORS[lane] || '#8b9098';
    const d = i.p.lastNote == null ? null : daysSince(i.p.lastNote);
    return `<div class="ic" data-act="open" data-k="${i.branch}" data-u="${i.uid}">
      <div class="t">${esc(i.summary)}${pageKey && i.branch !== pageKey ? `<span class="via">via ${esc(BY_KEY[i.branch].label)}</span>` : ''}</div>
      ${i.p.nextStep ? `<div class="ns">${esc(i.p.nextStep)}</div>` : ''}
      <div class="ft">
        ${lane ? `<span class="chip" style="background:${lc}22;color:${lc}">${LANE_LABELS[lane] || lane}</span>` : ''}
        <span style="flex:1"></span>
        <span class="ag mono">${d == null ? '—' : d + 'd'}</span>
      </div>
    </div>`;
  }

  /* ---- Compare: two overlapping cards side by side (2026-10-07) ---- */

  _compare() {
    if (!this.cmp) return '';
    const A = this.find(this.cmp.a.k, this.cmp.a.u), B = this.find(this.cmp.b.k, this.cmp.b.u);
    if (!A || !B) { this.cmp = null; return ''; }
    const c = overlapCoefficient(tokenize(A.summary), tokenize(B.summary));
    const col = (i, other) => {
      const b = BY_KEY[i.branch];
      const lane = laneOf(i);
      const lc = LANE_COLORS[lane] || '#8b9098';
      const notes = i.p.notes.slice(0, 3);
      return `<div class="cmp-col">
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
          <span class="chip" style="background:${b.color}22;color:${b.color}">${esc(b.label)}</span>
          ${lane ? `<span class="chip" style="background:${lc}22;color:${lc}">${LANE_LABELS[lane] || lane}</span>` : ''}
          ${i.p.hasBlocker ? '<span class="chip" style="background:#f0b35a22;color:var(--warn)">Blocked</span>' : ''}
          <span class="chip" style="background:#8b909822;color:var(--t2)">${ownerOf(i) === 'claude' ? 'Claude can do' : 'Needs you'}</span>
          <span class="ag mono" style="margin-left:auto">${esc(i.uid.slice(0, 8))}</span>
        </div>
        <h3 class="cmp-t">${esc(i.summary)}</h3>
        <div class="cmp-l">Next step</div><div class="cmp-v">${esc(i.p.hasBlocker ? 'Waiting: ' + i.p.blockedBy : i.p.nextStep) || '<span class="muted">—</span>'}</div>
        ${this.tbOf(i) && this.tbOf(i) !== 'none' ? `<div class="cmp-l">Toolbox</div><div class="cmp-v">${esc(this.tbOf(i))}</div>` : ''}
        <div class="cmp-l">Created · last note</div><div class="cmp-v">${esc(fmtCreated(uuidCreated(i.uid))) || '—'} · ${i.p.lastNote ? esc(i.p.lastNote) : 'no notes'}</div>
        ${notes.length ? `<div class="cmp-l">Recent notes</div>${notes.map((n) => `<div class="note"><b>${esc(n.date)}</b>${esc(n.text)}</div>`).join('')}` : ''}
        ${i.p.details ? `<div class="cmp-l">Details</div><div class="pre cmp-d">${esc(i.p.details)}</div>` : ''}
        <div class="cmp-a">
          <button class="btn" data-act="open" data-k="${i.branch}" data-u="${i.uid}">Open</button>
          <button class="btn" data-act="merge" data-k="${i.branch}" data-u="${i.uid}" data-k2="${other.branch}" data-u2="${other.uid}" title="Keep this card; append a merge note here and remove the other">Keep this, merge the other in</button>
        </div>
      </div>`;
    };
    return `<div class="cf" data-act="cmp-close"><div class="cf-box cmp-box" data-act="noop">
      <div class="cf-t" style="display:flex;align-items:center;gap:10px">Compare <span class="mono" style="font-size:11px;color:var(--t3)">${Math.round(c * 100)}% title overlap</span>
        <span style="flex:1"></span>
        <button class="btn" data-act="cmp-swap" title="Swap sides">⇄</button>
        <button class="btn" data-act="cmp-close">Close</button></div>
      <div class="cmp-g">${col(A, B)}${col(B, A)}</div>
      <div class="muted" style="font-size:11px;margin-top:10px">Merge only when the same action would close both. Different setups or owners: link them with PARENT / BLOCKED BY instead (open a card to edit it). Merging asks for confirmation.</div>
    </div></div>`;
  }

  /* ---- drawer ---- */

  _drawer() {
    if (!this.drawer) return '';
    const i = this.find(this.drawer.branchKey, this.drawer.uid);
    if (!i) return '';
    const b = BY_KEY[i.branch];
    const lane = i.p.lane || (WORK_TRACKERS.has(i.branch) ? 'work' : '');
    const lc = LANE_COLORS[lane] || '#8b9098';
    const sim = this.similarTo(i.summary, null).filter((x) => x.i.uid !== i.uid).slice(0, 3);

    const laneChips = ['work', 'need', 'want', 'urgent'].map((l) =>
      `<button class="btn" data-act="set-lane" data-k="${i.branch}" data-u="${i.uid}" data-v="${l}"
        style="${lane === l ? `border-color:${LANE_COLORS[l]};color:${LANE_COLORS[l]}` : ''}">${LANE_LABELS[l]}</button>`).join(' ');

    return `<div class="scrim" data-act="close"></div>
    <aside class="dw">
      <div class="dw-b">
        ${WRITES_ENABLED ? '' : '<div class="ro">Writes are switched off in this browser (localStorage mc.writes = off) — the buttons below are inert.</div>'}
        <div style="display:flex;gap:7px;flex-wrap:wrap">
          <span class="chip" style="background:${b.color}22;color:${b.color}">${esc(b.label)}</span>
          ${this.tagsOf(i).map((t) => { const tb = BY_KEY[t]; return tb ? `<span class="chip" style="background:${tb.color}14;color:${tb.color};opacity:.8" title="also on the ${esc(tb.label)} page">+ ${esc(tb.label)}</span>` : ''; }).join('')}
          ${lane ? `<span class="chip" style="background:${lc}22;color:${lc}">${LANE_LABELS[lane] || lane}</span>` : ''}
          ${i.p.priority === 'high' ? `<span class="chip" style="background:#ff6b6b22;color:var(--danger)">High</span>` : ''}
          ${i.p.hasBlocker ? `<span class="chip" style="background:#f0b35a22;color:var(--warn)">Blocked</span>` : ''}
          ${i.p.repeat ? `<span class="chip" style="background:#5aa9ff22;color:var(--work)" title="REPEAT: ${esc(i.p.repeat)}">Repeats</span>` : ''}
          ${i.p.effort ? `<span class="chip" data-act="dw-edit" style="cursor:pointer;background:#8b909822;color:var(--t1)" title="Hands-on time (EFFORT)">⏱ ${esc(i.p.effort)}</span>` : ''}
          ${i.p.cost ? `<span class="chip" data-act="dw-edit" style="cursor:pointer;background:#5cc98b22;color:var(--ok)" title="Hardware + paid services (COST)">${esc(i.p.cost)}</span>` : ''}
          ${i.status !== 'completed' ? `<span class="chip" style="background:#8b909822;color:var(--t2)">${ownerOf(i) === 'claude' ? 'Claude can do' : 'Needs you'}</span>` : ''}
          ${i.status === 'completed' ? `<span class="chip" style="background:#5cc98b22;color:var(--ok)">Done</span>` : ''}
        </div>
        <h2>${esc(i.summary)}</h2>
        ${(() => { const li = i.status === 'completed' ? [] : lintCard(i); return li.length ? `<div class="lint-d">Format: ${li.map(esc).join(' · ')}</div>` : ''; })()}
        ${(() => {
          /* Next step first, as text you read; edit on tap. */
          const ns = i.p.nextStep || '';
          if (this.dw.editNext || !ns) return `<div class="sec"><span class="lbl">Next step</span>
            <textarea id="dwNext" class="ta" rows="3" placeholder="One sentence: the very next thing to do">${esc(ns)}</textarea>
            <div style="display:flex;gap:6px;margin-top:7px"><button class="btn pri" data-act="set-next" data-k="${i.branch}" data-u="${i.uid}">Save next step</button>
            ${ns ? '<button class="btn" data-act="dw-tog" data-v="editNext">Cancel</button>' : ''}</div></div>`;
          const long = longStep(ns);
          const shown = long && !this.dw.nextFull ? firstStep(ns) : ns;
          return `<div class="sec"><span class="lbl">Next step</span>
            <div class="nx">${esc(shown)}</div>
            <div class="dw-links">${long ? `<button class="bt-link" data-act="dw-tog" data-v="nextFull">${this.dw.nextFull ? 'Show less' : 'Show the full step'}</button>` : ''}
            <button class="bt-link" data-act="dw-tog" data-v="editNext">Edit</button></div></div>`;
        })()}

        ${this._blocksSection(i)}
        ${(() => { const n = realNote(i.p); return n ? `<div class="resume"><span class="lbl">Where it stopped · ${esc(n.date)}</span>${esc(n.text)}</div>` : ''; })()}

        ${(() => {
          const sp = splitProvenance(i.p.details);
          let out = '';
          if (sp.body) {
            const LIM = 700;
            const cut = sp.body.length > LIM && !this.dw.detFull;
            const txt = cut ? sp.body.slice(0, sp.body.lastIndexOf(' ', LIM) > 400 ? sp.body.lastIndexOf(' ', LIM) : LIM) + '…' : sp.body;
            out += `<div class="sec"><span class="lbl">Details</span><div class="dtx">${esc(txt)}</div>
              ${sp.body.length > LIM ? `<div class="dw-links"><button class="bt-link" data-act="dw-tog" data-v="detFull">${this.dw.detFull ? 'Show less' : 'Show all details'}</button></div>` : ''}</div>`;
          }
          if (sp.prov.length) out += `<div class="sec"><button class="bt-link" data-act="prov-toggle">${this.provOpen ? '▾' : '▸'} Card history · ${sp.prov.length} line${sp.prov.length > 1 ? 's' : ''}</button>${this.provOpen ? `<div class="dtx muted" style="margin-top:6px;font-size:12px">${esc(sp.prov.join('\n'))}</div>` : ''}</div>`;
          return out;
        })()}
        ${i.p.refs.length ? `<div class="muted" style="margin:-6px 0 12px">Refs: ${i.p.refs.map(esc).join(' · ')}</div>` : ''}

        <div class="sec">
          ${i.p.notes.length ? `<span class="lbl">Progress log</span>` + (this.dw.notesAll ? i.p.notes : i.p.notes.slice(0, 3)).map((n) => `<div class="note"><b>${esc(n.date)}</b>${esc(n.text)}</div>`).join('')
            + (i.p.notes.length > 3 ? `<div class="dw-links"><button class="bt-link" data-act="dw-tog" data-v="notesAll">${this.dw.notesAll ? 'Show fewer' : `Show all ${i.p.notes.length} notes`}</button></div>` : '') : ''}
          ${this.dw.noteOpen ? `<textarea id="dwNote" class="ta" rows="2" placeholder="What happened? It is saved with today's date." style="margin-top:9px"></textarea>
            <div style="display:flex;gap:6px;margin-top:7px"><button class="btn pri" data-act="add-note" data-k="${i.branch}" data-u="${i.uid}">Save note</button>
            <button class="btn" data-act="dw-tog" data-v="noteOpen">Cancel</button></div>`
            : `<button class="btn" style="margin-top:${i.p.notes.length ? 9 : 0}px" data-act="dw-tog" data-v="noteOpen">+ Add a note</button>`}
        </div>

        ${sim.length ? `<div class="sec"><span class="lbl">Possible overlap</span>` +
          sim.map((x) => `<div class="row">
            <span class="t">${esc(x.i.summary)}</span>
            <span class="meta"><span class="mono" style="font-size:10.5px;color:var(--t3)">${Math.round(x.c * 100)}%</span>
            <button class="btn" data-act="open" data-k="${x.i.branch}" data-u="${x.i.uid}">Open</button>
            <button class="btn" data-act="merge" data-k="${i.branch}" data-u="${i.uid}" data-k2="${x.i.branch}" data-u2="${x.i.uid}">Merge here</button></span>
          </div>`).join('') + `</div>` : ''}

        <div class="sec dw-fold"><button class="bt-link" data-act="dw-tog" data-v="editOpen">${this.dw.editOpen ? '▾' : '▸'} Edit card <span class="muted">· lane, branches, toolbox, time and cost${i.p.repeat ? ', repeat' : ''}</span></button>
        ${this.dw.editOpen ? `
        <div class="sec"><span class="lbl">Lane</span><div style="display:flex;gap:6px;flex-wrap:wrap">${laneChips}</div>
          ${lane === 'work' && WORK_LANE_NEEDS_APPROVAL.has(i.branch)
            ? '<div class="qa-warn" style="margin-top:7px">LANE: work on this branch needs your explicit OK.</div>' : ''}
        </div>

        <div class="sec"><span class="lbl">Also in</span>
          <div style="display:flex;gap:6px;flex-wrap:wrap">${BRANCHES.filter((x) => x.key !== i.branch).map((x) => {
            const on = this.tagsOf(i).includes(x.key);
            return `<button class="btn" data-act="toggle-tag" data-k="${i.branch}" data-u="${i.uid}" data-v="${x.key}" title="${on ? 'Remove from' : 'Show on'} the ${esc(x.label)} page"
              style="${on ? `border-color:${x.color};color:${x.color}` : ''}">${esc(x.label)}</button>`; }).join(' ')}</div>
          <div class="muted" style="margin-top:6px;font-size:11px">Shows this card on another branch page too; it stays stored in ${esc(b.label)}.</div>
        </div>

        <div class="sec"><span class="lbl">Toolbox${this.tbSource(i) === 'proposed' ? ' — proposed' : ''}</span>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
            <select id="dwToolbox" class="tb-sel">${this._tbOptions(this.tbOf(i))}</select>
            <select id="dwBuy" class="tb-sel">${['', ...BUY_RUNS.map((d) => d.k)].map((v) =>
              `<option value="${esc(v)}"${v === this.buyOf(i) ? ' selected' : ''}>${v ? 'Buy first: ' + esc(v) : 'Parts in hand'}</option>`).join('')}</select>
            <button class="btn" data-act="set-toolbox" data-k="${i.branch}" data-u="${i.uid}">Save</button>
          </div></div>

        <div class="sec"><span class="lbl">Time and cost</span>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
            <input id="dwEffort" class="tb-sel" style="width:90px" placeholder="2h" value="${esc(i.p.effort)}" title="Hands-on time: 30m, 2h, 1d">
            <input id="dwCost" class="tb-sel" style="width:100px" placeholder="$0" value="${esc(i.p.cost)}" title="Hardware + paid services, e.g. $45. Leave empty when nothing to buy">
            <button class="btn" data-act="set-effort" data-k="${i.branch}" data-u="${i.uid}">Save</button>
          </div></div>
        ${i.p.repeat ? `<div class="sec"><span class="lbl">Repeats</span>
          <div class="row"><span class="t">${esc(i.p.repeat)}${i.due ? ` · next due ${esc(String(i.due).slice(0, 10))}` : ''}
            <small class="muted" style="display:block;font-size:11px">Marking it done stamps a note and reopens it with the next due date${/from\s+close/i.test(i.p.repeat) ? ', counted from the day you close it' : ', counted from the due date'}.</small></span>
            <span class="meta"><button class="btn" data-act="stop-repeat" data-k="${i.branch}" data-u="${i.uid}">Stop repeating</button></span></div></div>` : ''}` : ''}
        </div>

        ${(() => { const h = this.historyCache[i.uid]; const real = Array.isArray(h) ? h.filter((x) => x.field !== 'created') : [];
          return real.length ? `<div class="sec dw-fold"><button class="bt-link" data-act="dw-tog" data-v="histOpen">${this.dw.histOpen ? '▾' : '▸'} Change history · ${real.length}</button>${this.dw.histOpen ? this._historySection(i) : ''}</div>` : ''; })()}
        <div class="sec"><button class="btn" data-act="copy-init" data-k="${i.branch}" data-u="${i.uid}">Copy start prompt</button></div>

        <div class="sec"><span class="lbl">Meta</span>
          <div class="muted mono" style="font-size:11px">${esc(ent(i.branch))}<br>${esc(i.uid)}</div></div>
      </div>
      <div class="dw-f">
        <button class="btn pri" data-act="${i.status === 'completed' ? 'reopen' : 'done'}" data-k="${i.branch}" data-u="${i.uid}">
          ${i.status === 'completed' ? 'Reopen' : 'Mark done'}</button>
        <button class="btn" data-act="${i.p.hasBlocker ? 'unblock' : 'block'}" data-k="${i.branch}" data-u="${i.uid}">
          ${i.p.hasBlocker ? 'Unblock' : 'Mark blocked'}</button>
        <button class="btn" data-act="flag" data-k="${i.branch}" data-u="${i.uid}">Flag</button>
        <button class="btn" data-act="close" style="margin-left:auto">Close</button>
      </div>
    </aside>`;
  }

  /* =======================================================================
   * Events
   * ===================================================================== */

  _bind() {
    const r = this.shadowRoot;

    const text = r.getElementById('qaText');
    if (text) {
      text.oninput = (e) => { this.qa.text = e.target.value; this._debounce(); };
      text.onkeydown = (e) => {
        if (e.key === 'Enter') {
          const btn = r.querySelector('[data-act="qa-file"]');
          if (btn) btn.click();
        }
      };
    }
    const srch = r.getElementById('mcSearch');
    if (srch) {
      srch.oninput = (e) => {
        this.search = e.target.value; this.showMore.search = false;
        clearTimeout(this._sbt); this._sbt = setTimeout(() => this.render(), 150);
      };
      srch.onkeydown = (e) => { if (e.key === 'Escape') { e.stopPropagation(); this.search = ''; this.render(); } };
    }
    const sortSel = r.getElementById('mcSort');
    if (sortSel) sortSel.onchange = (e) => {
      this.sort = e.target.value;
      try { localStorage.setItem('mc.sort', this.sort); } catch (err) {}
      this.render();
    };
    const bsort = r.getElementById('mcBoardSort');
    if (bsort) bsort.onchange = (e) => {
      this.boardSort = e.target.value;
      try { localStorage.setItem('mc.boardSort', this.boardSort); } catch (err) {}
      this.render();
    };
    const bts = r.getElementById('btSearch');
    if (bts) {
      bts.oninput = (e) => {
        this.bt.q = e.target.value; this.bt.page = 0;
        clearTimeout(this._btt); this._btt = setTimeout(() => this.render(), 150);
      };
      bts.onkeydown = (e) => { if (e.key === 'Escape') { e.stopPropagation(); this.bt.q = ''; this.render(); } };
    }
    [['btStatus', 'status'], ['btLane', 'lane'], ['btOwner', 'owner']].forEach(([id, f]) => {
      const el = r.getElementById(id);
      if (el) el.onchange = (e) => { this.bt[f] = e.target.value; this.bt.page = 0; this.render(); };
    });
    const btGroup = r.getElementById('btGroup');
    if (btGroup) btGroup.onchange = (e) => {
      this.bt.group = e.target.value === 'none' ? 'none' : 'who'; this.bt.page = 0;
      try { localStorage.setItem('mc.btGroup', this.bt.group); } catch (err) {}
      this.render();
    };
    const btPer = r.getElementById('btPer');
    if (btPer) btPer.onchange = (e) => {
      this.bt.per = +e.target.value || 25; this.bt.page = 0;
      try { localStorage.setItem('mc.btPer', String(this.bt.per)); } catch (err) {}
      this.render();
    };
    const sel = r.getElementById('qaBranch');
    if (sel) sel.onchange = (e) => { this.qa.branch = e.target.value; this.qa.branchTouched = true; this.render(); };

    r.querySelectorAll('[data-act]').forEach((el) => {
      el.addEventListener('click', (ev) => {
        ev.stopPropagation();
        this._act(el.dataset, ev);
      });
    });
  }

  _debounce() {
    clearTimeout(this._dbt);
    this._dbt = setTimeout(() => this.render(), 260);
  }

  _act(d, ev) {
    const a = d.act;
    const item = d.k && d.u ? this.find(d.k, d.u) : null;

    switch (a) {
      case 'hub': this.view = { name: 'hub', branch: null }; this.drawer = null; this.qa.pins = {}; return this.render();
      case 'view': this.view = { name: d.v || 'hub', branch: null }; this.drawer = null; this.qa.pins = {}; return this.render();
      case 'setup': this.setup = (this.setup === d.v && d.v !== 'all') ? 'all' : d.v; return this.render();
      case 'setup-more': this.setupMore = true; return this.render();
      case 'sb-toggle':
        this.sbCollapsed = !this.sbCollapsed;
        try { localStorage.setItem('mc.sbCollapsed', this.sbCollapsed ? '1' : ''); } catch (e) {}
        return this.render();
      case 'board-toggle':
        this.boardOpen = !this.boardOpen;
        try { localStorage.setItem('mc.boardOpen', this.boardOpen ? '1' : ''); } catch (e) {}
        return this.render();
      case 'qa-toggle': {
        this.qaOpen = !this.qaOpen; this.render();
        const el = this.qaOpen && this.shadowRoot.getElementById('qaText');
        if (el) el.focus();
        return;
      }
      case 'unskip': this.skipped.clear(); return this.render();
      case 'copy-digest':
        copyToClipboard(this._digestText()).then(() => this._toast('Digest copied.'));
        return;
      case 'copy-morning':
      case 'copy-wrap': {
        const kind = d.act === 'copy-morning' ? 'morning' : 'evening';
        this._hass.callWS({ type: 'call_service', domain: 'mission_control', service: 'digest', service_data: { kind }, return_response: true })
          .then((r) => { const o = (r && r.response) || {}; return copyToClipboard(`${o.title || ''}\n${o.message || ''}`); })
          .then(() => this._toast(d.act === 'copy-morning' ? 'Morning plan copied — the text the morning push sends.' : 'Evening wrap copied — the text the evening push sends.'))
          .catch((e) => this._toast('Could not build it: ' + (e.message || e)));
        return;
      }
      case 'copy-brief':
        copyToClipboard(this._sessionBrief()).then(() => this._toast('Session brief copied — paste it into a fresh Claude session.'));
        return;
      case 'bring-back': return this.write(() => this._appendNote(d.k, d.u, 'Brought back from the Someday shelf.'), 'Bring back');
      case 'branch': this.view = { name: 'branch', branch: d.k }; this.drawer = null; this.qa.pins = {};
        this.bt = { ...this.bt, tab: 'all', q: '', status: '', lane: '', owner: '', page: 0, sel: new Set(), menu: false };
        this.qa.branch = d.k; return this.render();
      case 'mode': this._writeMode(d.v); return this.render();
      case 'tab-att': this.tabs.attention = d.v; return this.render();
      case 'tab-lane': this.tabs.lanes = d.v; return this.render();
      case 'tab-board': this.tabs.board = d.v; return this.render();
      case 'tab-tb': this.tabs.toolbox = this.tabs.toolbox === d.v ? null : d.v; return this.render();
      case 'set-toolbox': {
        const t = (this.shadowRoot.getElementById('dwToolbox') || {}).value || 'none';
        const b = (this.shadowRoot.getElementById('dwBuy') || {}).value || '';
        return this.write(async () => {
          await this._setField(d.k, d.u, 'TOOLBOX', t === 'none' ? '' : t);
          await this.refresh();
          await this._setField(d.k, d.u, 'BUY FIRST', b);
        }, 'Set toolbox');
      }
      case 'set-effort': {
        const e = ((this.shadowRoot.getElementById('dwEffort') || {}).value || '').trim();
        let c = ((this.shadowRoot.getElementById('dwCost') || {}).value || '').trim();
        if (/^\d[\d,.]*$/.test(c)) c = '$' + c;
        if (/^\$?0+(\.0+)?$/.test(c)) c = '';
        if (e && effortMin(e) == null) { this._toast('Time must read like 30m, 2h or 1d'); return; }
        if (c && costNum(c) == null) { this._toast('Cost must read like $45'); return; }
        return this.write(async () => {
          await this._setField(d.k, d.u, 'EFFORT', e);
          await this.refresh();
          await this._setField(d.k, d.u, 'COST', c);
        }, 'Set time and cost');
      }
      /* ---- branch table ---- */
      case 'bt-view':
        this.branchView = d.v === 'board' ? 'board' : 'table';
        try { localStorage.setItem('mc.branchView', this.branchView); } catch (e) {}
        return this.render();
      case 'bt-tab': this.bt.tab = d.v; this.bt.page = 0; return this.render();
      case 'bt-bundle': { const s = this.bt.openB; if (s.has(d.v)) s.delete(d.v); else s.add(d.v); return this.render(); }
      case 'prov-toggle': this.provOpen = !this.provOpen; return this.render();
      case 'bt-sort':
        if (this.bt.sort === d.v) this.bt.dir = this.bt.dir === 'asc' ? 'desc' : 'asc';
        else { this.bt.sort = d.v; this.bt.dir = (d.v === 'updated' || d.v === 'created') ? 'desc' : 'asc'; }
        return this.render();
      case 'bt-page': this.bt.page += +d.v; return this.render();
      case 'bt-sel': { const s = this.bt.sel; if (s.has(d.u)) s.delete(d.u); else s.add(d.u); return this.render(); }
      case 'bt-selall': {
        const uids = (d.v || '').split(',').filter(Boolean);
        const all = uids.length && uids.every((u) => this.bt.sel.has(u));
        uids.forEach((u) => (all ? this.bt.sel.delete(u) : this.bt.sel.add(u)));
        return this.render();
      }
      case 'bt-selclear': this.bt.sel = new Set(); return this.render();
      case 'bt-clear': Object.assign(this.bt, { q: '', status: '', lane: '', owner: '', page: 0 }); return this.render();
      case 'bt-done':
        this.bt.done = !this.bt.done; this.bt.page = 0;
        if (!this.bt.done && this.bt.status === 'done') this.bt.status = '';
        return this.render();
      case 'bt-colmenu': this.bt.menu = !this.bt.menu; return this.render();
      case 'bt-col': {
        if (d.v === '__reset') this.bt.cols = null;
        else {
          const curCols = this._btCols().map((c) => c.k);
          this.bt.cols = curCols.includes(d.v) ? curCols.filter((x) => x !== d.v)
            : BT_COLS.map((c) => c.k).filter((x) => curCols.includes(x) || x === d.v);
        }
        try { localStorage.setItem('mc.btCols', this.bt.cols ? JSON.stringify(this.bt.cols) : ''); } catch (e) {}
        return this.render();
      }
      case 'bt-bulk': {
        const bk = this.view.branch;
        const items = [...this.bt.sel].map((u) => this.find(bk, u)).filter((i) => i && i.status !== 'completed');
        if (!items.length) return;
        const [op, val] = (d.v || '').split(':');
        if (op === 'lane' && val === 'work' && items.some((i) => WORK_LANE_NEEDS_APPROVAL.has(i.branch))) return;
        const what = op === 'done' ? 'Mark done' : op === 'lane' ? `Set priority ${LANE_LABELS[val] || val}` : `Set who: ${val === 'claude' ? 'Claude' : 'You'}`;
        const names = items.slice(0, 8).map((i) => '“' + i.summary + '”').join(', ') + (items.length > 8 ? ` and ${items.length - 8} more` : '');
        return this._confirm(`${what} on ${items.length} task${items.length === 1 ? '' : 's'}?`, `Applies to ${names}.`, what,
          () => this.write(async () => {
            for (const i of items) {
              if (op === 'done') await this._setStatus(i.branch, i.uid, 'completed');
              else if (op === 'lane') await this._setField(i.branch, i.uid, 'LANE', val);
              else if (op === 'owner') await this._setField(i.branch, i.uid, 'OWNER', val);
            }
            this.bt.sel = new Set();
          }, what));
      }
      case 'more': this.showMore[d.v] = true; return this.render();
      case 'search-clear': this.search = ''; return this.render();
      case 'search-done': this.searchDone = !this.searchDone; return this.render();
      case 'open': this.drawer = { branchKey: d.k, uid: d.u }; this.dw = {}; this.provOpen = false; this._loadBlocks(d.u); return this.render();
      case 'dw-tog': this.dw = { ...this.dw, [d.v]: !this.dw[d.v] }; return this.render();
      case 'dw-edit': this.dw = { ...this.dw, editOpen: true }; return this.render();
      case 'clear-block': return this.write(() => this._clearBlock(d.k, d.u, Number(d.seq)), 'Clear block');
      case 'stop-repeat': return this.write(() => this._setField(d.k, d.u, 'REPEAT', ''), 'Stop repeating');
      case 'revert': return this.write(() => this._revert(d.u, Number(d.id)), 'Undo');
      case 'close': this.drawer = null; return this.render();
      case 'skip': this.skipped.add(d.k + '|' + d.u); return this.render();
      case 'qa-chip': this.qa.menu = this.qa.menu === d.k ? null : d.k; return this.render();
      case 'qa-pick': this.qa.pins[d.k] = d.v; this.qa.menu = null; this.render(); { const el = this.shadowRoot.getElementById('qaText'); if (el) el.focus(); } return;
      case 'qa-file': {
        const note = this.qa.text; const pins = { ...this.qa.pins };
        if (!note.trim()) return;
        const keepBranch = this.view.name === 'branch' ? this.view.branch : null;
        this.qa.text = ''; this.qa.pins = keepBranch ? { branch: keepBranch } : {}; this.qa.menu = null;
        this._fileNote(note, pins);
        return;
      }
      case 'qa-dismiss': this.feed = this.feed.filter((e) => e.id !== d.id); return this.render();
      case 'qa-undo': {
        const e = this.feed.find((x) => x.id === d.id);
        if (!e || !e.res || !e.res.uid) return;
        const list = (e.res.cards && e.res.cards.length) ? e.res.cards : [{ entity_id: e.res.entity_id, uid: e.res.uid }];
        return this.write(async () => {
          for (const f of list.slice().reverse()) if (f.uid) await this._svc('remove_item', { entity_id: f.entity_id, item: f.uid });
          e.status = 'undone';
        }, 'Undo quick add');
      }
      case 'qa-merge': {
        const e = this.feed.find((x) => x.id === d.id);
        if (!e || !e.res) return;
        const cv = (e.res.covers || [])[+d.i];
        if (!cv) return;
        const nk = (e.res.entity_id || '').replace(/^todo\.|_tracker$/g, '');
        const ok = cv.entity_id.replace(/^todo\.|_tracker$/g, '');
        const old = this.find(ok, cv.uid);
        const bits = [`Merged in “${cv.title}” (${cv.uid.slice(0, 8)})`];
        if (old && old.p.nextStep) bits.push(`its next step was: ${old.p.nextStep}`);
        if (old && old.p.notes[0]) bits.push(`its last note (${old.p.notes[0].date}): ${old.p.notes[0].text.slice(0, 200)}`);
        return this.write(async () => {
          await this._appendNote(nk, e.res.uid, bits.join('; ') + '.');
          await this._appendNote(ok, cv.uid, `Merged into “${(e.res.card || {}).title}” (${String(e.res.uid).slice(0, 8)}) from a quick note.`);
          await this._setStatus(ok, cv.uid, 'completed');
          e.merged = [...(e.merged || []), cv.uid];
        }, 'Merge it in');
      }
      case 'qa-dup-note': {
        const e = this.feed.find((x) => x.id === d.id);
        if (!e || !e.res) return;
        const dk = (e.res.duplicate_entity || '').replace(/^todo\.|_tracker$/g, '');
        return this.write(async () => {
          await this._appendNote(dk, e.res.duplicate_uid, `From a quick note: “${e.note}”`);
          await this._svc('remove_item', { entity_id: 'todo.mission_inbox', item: e.note });
          e.status = 'noted';
        }, 'Add as note');
      }
      case 'qa-dup-force': {
        const e = this.feed.find((x) => x.id === d.id);
        if (!e) return;
        this._fileNote(e.note, {}, { entry: e, fromInbox: true, force: true });
        return;
      }
      case 'inbox-file': {
        const it = (this.inbox || []).find((x) => x.uid === d.u);
        if (!it) return;
        const src = ((it.description || '').match(/^source:\s*(.+)$/m) || [])[1] || 'inbox';
        this._fileNote(it.summary, {}, { fromInbox: true, force: !!d.force, source: src });
        return;
      }
      case 'inbox-all': {
        const todo = (this.inbox || []).filter((x) => !/^held:/m.test(x.description || ''));
        (async () => { for (const it of todo) {
          const src = ((it.description || '').match(/^source:\s*(.+)$/m) || [])[1] || 'inbox';
          await this._fileNote(it.summary, {}, { fromInbox: true, source: src });
        } })();
        return;
      }
      case 'qa-lane': this.qa.lane = d.v; this.qa.laneTouched = true; return this.render();

      case 'copy-init':
        if (item) copyToClipboard(this._startPrompt(item)).then(() => this._toast('Start prompt copied.'));
        return;
      case 'copy-review':
        copyToClipboard(this._reviewPrompt(d.k)).then(() => this._toast('Review prompt copied.'));
        return;
      case 'compare':
        this.cmp = { a: { k: d.k, u: d.u }, b: { k: d.k2, u: d.u2 } }; return this.render();
      case 'cmp-close': this.cmp = null; return this.render();
      case 'cmp-swap': if (this.cmp) this.cmp = { a: this.cmp.b, b: this.cmp.a }; return this.render();

      /* ---- mutating actions: all routed through write(), which is gated ---- */
      case 'done': return this.write(() => this._setStatus(d.k, d.u, 'completed'), 'Mark done');
      case 'reopen': return this.write(() => this._setStatus(d.k, d.u, 'needs_action'), 'Reopen');
      case 'add-note': return this.write(async () => { await this._appendNote(d.k, d.u,
        (this.shadowRoot.getElementById('dwNote') || {}).value || ''); this.dw = { ...this.dw, noteOpen: false }; }, 'Append note');
      case 'append-note': return this.write(() => this._appendNote(d.k, d.u, this.qa.text), 'Append as progress note');
      case 'set-next': return this.write(async () => { await this._setField(d.k, d.u, 'NEXT STEP',
        (this.shadowRoot.getElementById('dwNext') || {}).value || ''); this.dw = { ...this.dw, editNext: false }; }, 'Save next step');
      case 'set-lane':
        /* Guardrail 1 — LANE: work on a non-work branch needs an explicit OK. */
        if (d.v === 'work' && WORK_LANE_NEEDS_APPROVAL.has(d.k)) {
          return this._confirm('Put a non-work item in the work lane?',
            `${BY_KEY[d.k].label} items are not supposed to carry LANE: work — the work lane is Ticketing and Business Ops only, and the business-hours focus filter shows work-lane items exclusively. Confirm only if you really want this item competing for work hours.`,
            'Set LANE: work',
            () => this.write(() => this._setField(d.k, d.u, 'LANE', 'work'), 'Set lane'));
        }
        return this.write(() => this._setField(d.k, d.u, 'LANE', d.v), 'Set lane');
      case 'block': return this.write(() => this._setField(d.k, d.u, 'BLOCKED BY', 'Manually blocked'), 'Mark blocked');
      case 'unblock': return this.write(() => this._setField(d.k, d.u, 'BLOCKED BY', ''), 'Unblock');
      case 'toggle-tag': {
        const i = this.find(d.k, d.u);
        if (!i) return;
        const cur = this.tagsOf(i);
        const next = cur.includes(d.v) ? cur.filter((t) => t !== d.v) : cur.concat(d.v);
        return this.write(() => this._setField(d.k, d.u, 'TAGS', next.join(', ')), 'Set tags');
      }
      case 'flag': return this.write(() => this._setField(d.k, d.u, 'NEXT STEP',
        '⚠️ Flagged for manual review — verify before closing or deleting'), 'Flag');
      case 'merge': {
        const keep = this.find(d.k, d.u), dup = this.find(d.k2, d.u2);
        if (!keep || !dup) return;
        return this._confirm(
          'Merge and remove duplicate?',
          `A dated note recording the merge will be appended to "${keep.summary}", then "${dup.summary}" will be REMOVED from ${ent(dup.branch)}. Removal cannot be undone from here.`,
          'Merge and remove',
          () => this.write(() => this._merge(keep, dup), 'Merge')
        );
      }
      case 'qa-add': return this.write(() => this._addItem(d.k, d.lane, this.qa.text), 'Add item');
      default: return;
    }
  }

  /* ---- write primitives (unreachable while WRITES_ENABLED is false) ---- */

  async _svc(service, data) {
    return this._hass.callService('todo', service, data);
  }
  _setStatus(k, u, status) { return this._svc('update_item', { entity_id: ent(k), item: u, status }); }

  _appendNote(k, u, text) {
    const i = this.find(k, u);
    if (!i || !text.trim()) return Promise.resolve();
    const date = ymd();
    const line = `${date} — ${text.trim()}`;
    const d = i.description;
    const lines = d.split('\n');
    /* Insert after the last existing dated note, else right after the PROGRESS: line. */
    let at = -1;
    for (let n = 0; n < lines.length; n++) {
      if (NOTE_RE.test(lines[n].trim())) at = n;
      else if (at === -1 && /^PROGRESS:/i.test(lines[n].trim())) at = n;
    }
    if (at === -1) lines.push('', 'PROGRESS:', line);
    else lines.splice(at + 1, 0, line);
    return this._svc('update_item', { entity_id: ent(k), item: u, description: lines.join('\n') });
  }

  /* Replace exactly one field line in place; never rebuild the description. */
  _setField(k, u, field, value) {
    const i = this.find(k, u);
    if (!i) return Promise.resolve();
    const lines = i.description.split('\n');
    const re = new RegExp('^' + field.replace(/ /g, '\\s') + ':', 'i');
    let found = false;
    const out = [];
    for (const ln of lines) {
      if (re.test(ln.trim())) {
        found = true;
        if (value) out.push(`${field}: ${value}`);
      } else out.push(ln);
    }
    if (!found && value) {
      /* header fields sit above NEXT STEP; LANE goes first */
      let idx = field === 'LANE' ? 0 : out.findIndex((l) => /^NEXT STEP:/i.test(l.trim()));
      if (idx === -1) idx = out.findIndex((l) => l.trim() === '');
      if (idx === -1) out.push(`${field}: ${value}`); else out.splice(idx, 0, `${field}: ${value}`);
    }
    return this._svc('update_item', { entity_id: ent(k), item: u, description: out.join('\n') });
  }

  /* Merge: record the merge on the keeper first, then remove the duplicate.
     Order matters — if the remove fails, the keeper still carries the note and
     nothing is lost. Never call this without _confirm(). */
  async _merge(keep, dup) {
    const bits = [`Merged in duplicate "${dup.summary}" from ${ent(dup.branch)}`];
    if (dup.p.nextStep) bits.push(`its NEXT STEP was: ${dup.p.nextStep}`);
    await this._appendNote(keep.branch, keep.uid, bits.join('; ') + '.');
    await this._svc('remove_item', { entity_id: ent(dup.branch), item: dup.uid });
  }

  async _addItem(k, lane, summary) {
    const s = (summary || '').trim();
    if (!s) return;
    await this._svc('add_item', { entity_id: ent(k), item: s });
    const items = await this._items(ent(k), 'needs_action');
    const made = items.find((x) => x.summary === s);
    if (!made) return;
    const desc = [`LANE: ${lane}`, 'NEXT STEP: Define the first concrete action.'].join('\n');
    await this._svc('update_item', { entity_id: ent(k), item: made.uid, description: desc });
    this.qa.text = '';
  }

  /* ---- prompt templates ---- */

  _reviewPrompt(k) {
    const b = BY_KEY[k];
    const r = this.reviewDelta(k);
    const scope = r.since == null
      ? `Scope: this branch has never been reviewed — review every open card once (${r.n}).`
      : `Scope: only the ${r.n} card(s) created or updated since the last review (${r.sinceDate}). Cards untouched since then were already reviewed — leave them alone.` + (r.open.length ? `\nChanged open cards: ${r.open.map((i) => i.uid.slice(0, 8) + ' ' + i.summary).join('; ')}` : '');
    return `Run a Mission Control branch review for ${b.label} (todo.${k}_tracker). Read cards with the mission_control/items/list WebSocket command (or todo.get_items) and write with mission_control/items/update and mission_control/notes/add.

${scope}

Lean card: short verb-first title; header fields one per line (LANE, OWNER, TOOLBOX, BUY FIRST, EFFORT, COST, BLOCKED BY, PARENT, TAGS, REPEAT, NEXT STEP); free-text details; then PROGRESS as an append-only log of dated notes.

Review: every open item has a lane and a one-sentence NEXT STEP; flag items with no dated note in 30+ days; flag items that read as done; then, if the helper input_datetime.${k}_last_review exists, set it to now.

Guardrails: PROGRESS is append-only; never delete an item — flag it instead${WORK_LANE_NEEDS_APPROVAL.has(k) ? '; LANE: work on this branch needs my explicit OK' : ''}. Report tersely: what changed, what you flagged, what needs me.`;
  }

  /* Generated on demand — replaces the stored INITIATION PROMPT field (lean card 2026-09-24). */
  _startPrompt(i) {
    const b = BY_KEY[i.branch];
    const notes = i.p.notes.slice(0, 3).map((n) => `- ${n.date} — ${n.text}`).join('\n');
    const det = i.p.details.length > 3000 ? i.p.details.slice(0, 3000) + ' …' : i.p.details;
    return [
      `Continue the Mission Control item "${i.summary}" (${b.label}, ${ent(i.branch)}, uid ${i.uid}).`,
      realNote(i.p) ? `Where it stopped (${realNote(i.p).date}): ${realNote(i.p).text}` : 'No progress notes yet — this is a fresh start.',
      i.p.nextStep ? `Next step: ${i.p.nextStep}` : 'No next step is recorded — start by agreeing one.',
      `Who moves it: ${ownerOf(i) === 'claude' ? 'Claude can do this alone' : 'needs you (hands-on or a decision)'}.`,
      i.p.refs.length ? `Read first: ${i.p.refs.join('; ')}` : '',
      i.p.blockedBy ? `Blocked by: ${i.p.blockedBy}` : '',
      det ? `Context:\n${det}` : '',
      notes ? `Recent progress (newest first):\n${notes}` : '',
      'Before you finish, append ONE dated PROGRESS note as the resume point — "YYYY-MM-DD — Stopped: … Next: … Blocked: …" (omit Blocked if nothing is) — and update NEXT STEP to match. Never rewrite the log.',
    ].filter(Boolean).join('\n\n');
  }
}

if (!customElements.get('mission-control-app')) {
  customElements.define('mission-control-app', MissionControlV2App);
}
