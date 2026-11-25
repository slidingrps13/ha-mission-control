"""Config and options flow for Mission Control.

Setup asks three optional questions (your name, an AI Task entity for quick add, a
calendar for due dates) and starts with the default branches.  Configure later
changes those settings, turns the optional modules on or off, and adds, edits or
removes branches.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_ACTIVE,
    CONF_AI_TASK,
    CONF_BRANCHES,
    CONF_CALENDAR,
    CONF_PACKAGES,
    CONF_PERSON,
    CONF_SHOPPING,
    DEFAULT_BRANCHES,
    DOMAIN,
    slugify_branch,
)

PALETTE = ["#ff9f43", "#4fb3f6", "#f06292", "#b07cf0", "#c6d24a", "#3fc9d6", "#66bb6a", "#ff6b6b"]


def _hex(rgb: Any, fallback: str) -> str:
    try:
        r, g, b = (int(x) for x in rgb)
        return f"#{r:02x}{g:02x}{b:02x}"
    except (TypeError, ValueError):
        return fallback


def _rgb(hexcolor: str) -> list[int]:
    h = (hexcolor or "#888888").lstrip("#")
    try:
        return [int(h[i : i + 2], 16) for i in (0, 2, 4)]
    except ValueError:
        return [136, 136, 136]


def _settings_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_PERSON): selector.TextSelector(),
            vol.Optional(CONF_AI_TASK): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="ai_task")
            ),
            vol.Optional(CONF_CALENDAR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="calendar")
            ),
        }
    )


def _branch_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("name"): selector.TextSelector(),
            vol.Optional("icon", default="mdi:folder-outline"): selector.IconSelector(),
            vol.Optional("color"): selector.ColorRGBSelector(),
            vol.Optional("work", default=False): selector.BooleanSelector(),
            vol.Optional("assistant", default=False): selector.BooleanSelector(),
            vol.Optional("keywords"): selector.TextSelector(),
        }
    )


class MissionControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """One instance; it starts serving the default branches straight away."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MissionControlOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=_settings_schema())
        options = {
            CONF_ACTIVE: True,
            CONF_BRANCHES: [dict(b) for b in DEFAULT_BRANCHES],
            CONF_PACKAGES: False,
            CONF_SHOPPING: False,
            **{k: v for k, v in user_input.items() if v not in (None, "")},
        }
        return self.async_create_entry(title="Mission Control", data={}, options=options)


class MissionControlOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._edit_key: str | None = None

    # ------------------------------------------------------------ helpers
    @property
    def _branches(self) -> list[dict]:
        return [dict(b) for b in self.config_entry.options.get(CONF_BRANCHES) or DEFAULT_BRANCHES]

    def _save(self, **changes: Any) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    def _branch_options(self) -> list[selector.SelectOptionDict]:
        return [selector.SelectOptionDict(value=b["key"], label=b["name"]) for b in self._branches]

    def _branch_from_form(self, user_input: dict, base: dict) -> dict:
        out = dict(base)
        out["name"] = user_input["name"].strip()
        out["icon"] = user_input.get("icon") or base.get("icon") or "mdi:folder-outline"
        out["color"] = _hex(user_input.get("color"), base.get("color") or PALETTE[0])
        out["work"] = bool(user_input.get("work"))
        out["assistant"] = bool(user_input.get("assistant"))
        out["keywords"] = (user_input.get("keywords") or "").strip()
        return out

    # -------------------------------------------------------------- steps
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "add_branch", "edit_branch", "remove_branch"]
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        schema = _settings_schema().extend(
            {
                vol.Optional(CONF_PACKAGES, default=False): selector.BooleanSelector(),
                vol.Optional(CONF_SHOPPING, default=False): selector.BooleanSelector(),
            }
        )
        if user_input is not None:
            opts = {k: v for k, v in self.config_entry.options.items()
                    if k not in (CONF_PERSON, CONF_AI_TASK, CONF_CALENDAR)}
            opts.update({k: v for k, v in user_input.items() if v not in (None, "")})
            return self.async_create_entry(data=opts)
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(schema, dict(self.config_entry.options)),
        )

    async def async_step_add_branch(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        branches = self._branches
        if user_input is not None:
            name = (user_input.get("name") or "").strip()
            if not name:
                errors["name"] = "name_required"
            elif any(b["name"].lower() == name.lower() for b in branches):
                errors["name"] = "name_taken"
            else:
                key, n = slugify_branch(name), 2
                keys = {b["key"] for b in branches}
                base_key = key
                while key in keys:
                    key, n = f"{base_key}_{n}", n + 1
                base = {"key": key, "color": PALETTE[len(branches) % len(PALETTE)]}
                return self._save(**{CONF_BRANCHES: [*branches, self._branch_from_form(user_input, base)]})
        return self.async_show_form(step_id="add_branch", data_schema=_branch_schema(), errors=errors)

    async def async_step_edit_branch(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._edit_key = user_input["branch"]
            return await self.async_step_edit_branch_details()
        schema = vol.Schema(
            {vol.Required("branch"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=self._branch_options()))}
        )
        return self.async_show_form(step_id="edit_branch", data_schema=schema)

    async def async_step_edit_branch_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        branches = self._branches
        cur = next(b for b in branches if b["key"] == self._edit_key)
        errors: dict[str, str] = {}
        if user_input is not None:
            name = (user_input.get("name") or "").strip()
            if not name:
                errors["name"] = "name_required"
            elif any(b["name"].lower() == name.lower() and b["key"] != cur["key"] for b in branches):
                errors["name"] = "name_taken"
            else:
                new = self._branch_from_form(user_input, cur)
                return self._save(**{CONF_BRANCHES: [new if b["key"] == cur["key"] else b for b in branches]})
        suggested = {**cur, "color": _rgb(cur.get("color"))}
        return self.async_show_form(
            step_id="edit_branch_details",
            data_schema=self.add_suggested_values_to_schema(_branch_schema(), suggested),
            description_placeholders={"key": cur["key"]},
            errors=errors,
        )

    async def async_step_remove_branch(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        branches = self._branches
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input["branch"]
            if len(branches) <= 1:
                errors["branch"] = "last_branch"
            else:
                data = getattr(self.config_entry, "runtime_data", None)
                count = 0
                if data is not None:
                    rows = await data.run(data.store.items)
                    count = sum(1 for r in rows if r["branch"] == key)
                if count:
                    errors["branch"] = "branch_not_empty"
                else:
                    return self._save(**{CONF_BRANCHES: [b for b in branches if b["key"] != key]})
        schema = vol.Schema(
            {vol.Required("branch"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=self._branch_options()))}
        )
        return self.async_show_form(step_id="remove_branch", data_schema=schema, errors=errors)
