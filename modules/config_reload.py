"""Config reload helpers, mixed into MeshCoreBot.

What reload_config uses to check a new config before applying it: the sections that only take effect on restart, validation of the snapshot, and saving/restoring command config state for rollback. reload_config itself stays in core, where tests patch modules.core.CommandManager."""

import configparser
from typing import Any

from .command_manager import CommandManager


class ConfigReloadMixin:
    """Mixed into MeshCoreBot."""

    _COMMAND_CONFIG_STATE: Any

    @staticmethod
    def _config_section_values(
        config: configparser.ConfigParser, section: str
    ) -> dict[str, str]:
        if not config.has_section(section):
            return {}
        return dict(config.items(section, raw=True))

    def _restart_only_config_changes(
        self,
        old_config: configparser.ConfigParser,
        new_config: configparser.ConfigParser,
    ) -> list[str]:
        """Return changed settings whose owning component is startup-only.

        Service plugins commonly cache constructor settings.  Until they expose
        a transactional reload contract, rejecting those edits is safer than a
        split state where on-demand reads see new values and cached fields do not.
        """
        changed: list[str] = []
        # Built-in services may currently be disabled and therefore absent from
        # ``self.services``.  Their enable flags and constructor-cached settings
        # are still restart-only; otherwise a successful reload would claim to
        # start a service that was never instantiated.
        startup_sections = {
            "Admin",
            "Connection",
            "DARC_MoWaS_Service",
            "DiscordBridge",
            "Earthquake_Service",
            "Feed_Manager",
            "Logging",
            "MapUploader",
            "MqttWeather",
            "PacketCapture",
            "RepeaterPrefixCollision_Service",
            "Service_Overrides",
            "TelegramBridge",
            "Weather_Service",
            "Webhook",
            "Web_Viewer",
            "Worldcup_Service",
        }
        for service in getattr(self, "services", {}).values():
            section = getattr(service, "config_section", None)
            if not section:
                derive = getattr(service, "_derive_config_section", None)
                if callable(derive):
                    section = derive()
            if isinstance(section, str) and section:
                startup_sections.add(section)
            if service.__class__.__name__ == "WeatherService":
                # WeatherService also caches units from the shared [Weather]
                # section during construction.
                startup_sections.add("Weather")

        for section in sorted(startup_sections):
            if self._config_section_values(old_config, section) != self._config_section_values(
                new_config, section
            ):
                changed.append(f"[{section}]")

        for key in ("db_path", "local_dir_path", "prefix_bytes"):
            old_value = old_config.get("Bot", key, fallback="")
            new_value = new_config.get("Bot", key, fallback="")
            if old_value != new_value:
                changed.append(f"[Bot] {key}")
        return changed

    @staticmethod
    def _validate_config_snapshot(config: configparser.ConfigParser) -> list[str]:
        """Validate the fully merged candidate before it can be published.

        Returns the options whose values would fail '%' interpolation. They do not
        block the reload: a literal '%' is legal in values read raw (templates,
        strftime formats), and startup accepts the same file.
        """
        from .config_schema import SECTIONS
        from .config_validation import REQUIRED_SECTIONS

        missing = sorted(REQUIRED_SECTIONS - set(config.sections()))
        if missing:
            raise ValueError(
                "Missing required configuration section(s): " + ", ".join(missing)
            )

        # Expand interpolation across every final value, not just the base file,
        # so a malformed '%' expression in an overlay is reported before publish.
        uninterpolatable = []
        for section in config.sections():
            for key in config.options(section):
                try:
                    config.get(section, key)
                except configparser.InterpolationError:
                    uninterpolatable.append(f"[{section}] {key}")

        # Validate all typed keys currently covered by the project schema.
        for section, section_meta in SECTIONS.items():
            if not config.has_section(section):
                continue
            for key, meta in section_meta.keys.items():
                if not config.has_option(section, key):
                    continue
                if meta.type == "int":
                    config.getint(section, key)
                elif meta.type == "bool":
                    config.getboolean(section, key)
                elif meta.type == "enum" and meta.values:
                    value = config.get(section, key).strip().lower()
                    if value not in meta.values:
                        raise ValueError(
                            f"[{section}] {key} must be one of "
                            f"{', '.join(meta.values)} (got {value!r})"
                        )
        return uninterpolatable

    @classmethod
    def _command_config_state(cls, manager: CommandManager) -> dict[str, Any]:
        return {name: getattr(manager, name) for name in cls._COMMAND_CONFIG_STATE}

    @classmethod
    def _apply_command_config_state(
        cls, manager: CommandManager, state: dict[str, Any]
    ) -> None:
        for name in cls._COMMAND_CONFIG_STATE:
            setattr(manager, name, state[name])
