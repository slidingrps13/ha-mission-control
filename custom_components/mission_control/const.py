"""Constants and the runtime branch registry for Mission Control."""

from __future__ import annotations

import re

import voluptuous as vol

DOMAIN = "mission_control"
VERSION = "1.0.0"

# --------------------------------------------------------------------- branches
# A branch is one tracker: one to-do list (todo.<key>_tracker) and one page in the
# panel.  The set is configured per install (Settings -> Devices & services ->
# Mission Control -> Configure) and stored in the entry options as a list of dicts:
#   key       stable slug, also the entity id (todo.<key>_tracker) — never renamed
#   name      label shown in the panel and on the to-do list
#   icon      mdi icon
#   color     hex colour for the panel
#   work      True for a work branch: untagged cards there read as LANE: work, and
#             the work-hours mode (weekdays 08:00–17:00) brings them forward
#   assistant True when the AI assistant can usually finish desk work there on its
#             own — cards default to "the assistant's move" instead of yours
#   keywords  optional words the quick-add box uses to guess the branch
DEFAULT_BRANCHES: list[dict] = [
    {
        "key": "home",
        "name": "Home",
        "icon": "mdi:home-heart",
        "color": "#ff9f43",
        "work": False,
        "assistant": False,
        "keywords": "hvac, yard, plumbing, paint, fence, roof, garage, furniture, switch, outlet, "
        "fixture, bulb, breaker, faucet, toilet, sink, shower, drywall, door, window, closet, "
        "cabinet, bathroom, kitchen, attic, leak, water heater, caulk, tile, floor, repair, "
        "install, mount, electrician, plumber",
    },
    {
        "key": "projects",
        "name": "Projects",
        "icon": "mdi:lightbulb-on-outline",
        "color": "#4fb3f6",
        "work": False,
        "assistant": True,
        "keywords": "automation, home assistant, dashboard, sensor, script, integration, server, "
        "backup, website, app, code, research",
    },
    {
        "key": "personal",
        "name": "Personal",
        "icon": "mdi:account-outline",
        "color": "#f06292",
        "work": False,
        "assistant": False,
        "keywords": "doctor, dentist, appointment, birthday, gift, pet, vet, gym, trip, travel, "
        "renew, passport",
    },
    {
        "key": "work",
        "name": "Work",
        "icon": "mdi:briefcase-outline",
        "color": "#b07cf0",
        "work": True,
        "assistant": False,
        "keywords": "client, invoice, meeting, report, deadline, proposal, ticket",
    },
    {
        "key": "automotive",
        "name": "Automotive",
        "icon": "mdi:car-outline",
        "color": "#c6d24a",
        "work": False,
        "assistant": False,
        "keywords": "car, oil change, tire, brake, inspection, registration, battery, wiper",
    },
]

# Runtime registry, filled from the config entry by apply_branches() at setup.
# These objects are shared by reference (``from .const import BRANCHES``), so they
# are mutated in place and never rebound.
BRANCHES: dict[str, str] = {}  # key -> name
BRANCH_CONF: dict[str, dict] = {}  # key -> the full branch dict
CLAUDE_BRANCHES: set[str] = set()  # "assistant" branches
WORK_BRANCHES: set[str] = set()


def slugify_branch(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return key[:30] or "branch"


def apply_branches(branches: list[dict]) -> None:
    """Load the configured branch list into the runtime registry (and the tag aliases)."""
    from .codec import set_tag_aliases  # noqa: PLC0415  (codec imports const)

    BRANCHES.clear()
    BRANCH_CONF.clear()
    CLAUDE_BRANCHES.clear()
    WORK_BRANCHES.clear()
    for b in branches:
        BRANCHES[b["key"]] = b["name"]
        BRANCH_CONF[b["key"]] = dict(b)
        if b.get("assistant"):
            CLAUDE_BRANCHES.add(b["key"])
        if b.get("work"):
            WORK_BRANCHES.add(b["key"])
    set_tag_aliases(branches)


def valid_branch(value):
    """Voluptuous validator: a configured branch key (checked at call time)."""
    if not isinstance(value, str) or value not in BRANCHES:
        raise vol.Invalid(f"unknown branch {value!r}; configured: {', '.join(BRANCHES)}")
    return value


# ----------------------------------------------------------------------- owners
# OWNER: me | claude.  "me" is the person using the board; "claude" stands for the
# AI assistant (any AI Task provider).  The person's own name is accepted as an
# alias for "me" when it is set in the options.
OWNER_ME = "me"
OWNER_AI = "claude"
OWNER_ALIASES: dict[str, str] = {"me": OWNER_ME, "you": OWNER_ME, "claude": OWNER_AI, "ai": OWNER_AI}


def set_person_name(name: str | None) -> None:
    for k in [k for k, v in OWNER_ALIASES.items() if v == OWNER_ME and k not in ("me", "you")]:
        OWNER_ALIASES.pop(k)
    n = (name or "").strip().lower()
    if n and n not in OWNER_ALIASES:
        OWNER_ALIASES[n] = OWNER_ME


def owner_key(value: str | None) -> str:
    """Normalize an OWNER value to "me" / "claude" / ""."""
    return OWNER_ALIASES.get((value or "").strip().lower(), "")


# ---------------------------------------------------------------------- options
CONF_ACTIVE = "active"
CONF_BRANCHES = "branches"
CONF_PERSON = "person_name"
CONF_AI_TASK = "ai_task_entity"
CONF_CALENDAR = "calendar_entity"
CONF_PACKAGES = "packages"
CONF_SHOPPING = "shopping"

DB_FILE = "mission_control.db"
BACKUP_DIR = "mission_control_backups"
EXPORT_KEEP = 30

PANEL_URL = "mission-control"
STATIC_URL = "/mission_control_frontend"

SIGNAL_CHANGED = f"{DOMAIN}_changed"
# Package tracker: fired after every orders write; event on stage change.
SIGNAL_ORDERS = f"{DOMAIN}_orders_changed"
EVENT_ORDER = f"{DOMAIN}_order"
# Shopping list: fired after every shopping write; event when an order email is
# too vague to match a list item.
SIGNAL_SHOPPING = f"{DOMAIN}_shopping_changed"
EVENT_SHOP_QUESTION = f"{DOMAIN}_shopping_question"
SHOPPING_TODO = "todo.mission_shopping"

# Toolbox values that need the person on site (mirrors app.js HANDS_ON).
HANDS_ON = {
    "Physical checks",
    "On-site",
    "Electrical tools out",
    "Plumbing tools out",
    "DIY session",
    "Attic",
    "Exterior",
    "Car",
    "Network closet",
    "Portals & follow-ups",
}


def entity_id_for(branch: str) -> str:
    return f"todo.{branch}_tracker"
