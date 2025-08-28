from homeassistant import config_entries
import voluptuous as vol

DOMAIN = "local_daikin"

class DaikinConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Let user enter host and a custom title."""
        errors = {}
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required("host"): str,
                        vol.Optional("title", default="Local Daikin"): str,
                    }
                ),
                errors=errors,
            )

        host = str(user_input.get("host", "")).strip()
        title = str(user_input.get("title") or f"Local Daikin ({host})").strip()
        if not host:
            errors["host"] = "required"
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required("host"): str,
                        vol.Optional("title", default=title or "Local Daikin"): str,
                    }
                ),
                errors=errors,
            )

        # 防重複：以 host 當 unique_id（你也可以改成 MAC）
        await self.async_set_unique_id(host)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(title=title, data={"host": host})


# ==============================
# Options Flow：允許修改顯示名稱
# ==============================
class LocalDaikinOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is None:
            # 只讓使用者改 title；host 維持不在這裡改，避免 unique_id 變更議題
            schema = vol.Schema(
                {
                    vol.Required(
                        "title",
                        default=self.config_entry.title or "Local Daikin",
                    ): str,
                }
            )
            return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

        # 寫回 entry 的標題
        new_title = str(user_input.get("title") or "").strip() or self.config_entry.title
        if new_title != self.config_entry.title:
            self.hass.config_entries.async_update_entry(self.config_entry, title=new_title)

        # 這裡我們不需要保存任何 options（回傳空 dict 即可）
        return self.async_create_entry(title="", data={})


async def async_get_options_flow(config_entry: config_entries.ConfigEntry):
    return LocalDaikinOptionsFlow(config_entry)