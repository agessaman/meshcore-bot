# Alternative Plugins Directory

This directory is for alternative implementations of bot commands that can replace or supplement the default plugins.

## Purpose

Some default plugins may not work well in all contexts. For example:
- The `wx` command uses NOAA data which is primarily for US locations
- International users may need weather data from different sources
- Regional variations may require different implementations

Alternative plugins allow you to swap out default plugins without modifying the core codebase.

## Weather Sources Are Not Alternative Plugins

`wx_international.py` is a compatibility module now, not an alternative plugin: it re-exports `GlobalWxCommand` (an alias of `modules/commands/gwx_command.py`'s `GwxCommand`) and the names code imported from it, and defines no command class of its own, so the loader skips it. `gwx` is a normal command in `modules/commands/`, and `wx` and `gwx` share one request path and differ only in the provider that answers.

A new weather source belongs in `modules/weather_providers/`, registered in `PROVIDERS` or `ALERT_PROVIDERS` and selected with `[Weather] weather_provider` or `alerts_provider`, not in this directory. To make `wx` answer from Open-Meteo, set `[Weather] weather_provider = openmeteo`; `[Plugin_Overrides] wx = wx_international` still does that, with a deprecation warning.

## How It Works

### Automatic Replacement by Name

If an alternative plugin has the same `name` metadata as a default plugin, it will automatically replace the default plugin. For example:

- Default plugin: `modules/commands/wx_command.py` with `name = "wx"`
- Alternative plugin: `modules/commands/alternatives/your_plugin.py` with `name = "wx"`

The alternative plugin will automatically replace the default one.

### Configuration-Based Overrides

You can explicitly configure which alternative plugin to use for a command by adding a `[Plugin_Overrides]` section to your `config.ini`:

```ini
[Plugin_Overrides]
wx = your_plugin
```

This tells the bot to use `your_plugin.py` from the alternatives directory to replace the `wx` command.

`wx = wx_international` is retired: it logs a deprecation warning and sets `[Weather] weather_provider = openmeteo` instead, which is what it used to achieve.

## Creating an Alternative Plugin

1. **Copy the structure**: Start with the default plugin as a reference
2. **Place in alternatives directory**: Save your plugin as `modules/commands/alternatives/your_plugin_name.py`
3. **Match the plugin name**: Set the `name` class attribute to match the command you want to replace:
   ```python
   class WxInternationalCommand(BaseCommand):
       name = "wx"  # This will replace the default wx command
       keywords = ['wx', 'weather', 'wxa', 'wxalert']
       # ... rest of implementation
   ```
4. **Implement required methods**: Your plugin must inherit from `BaseCommand` and implement the `execute` method
5. **Test**: Restart the bot and verify your alternative plugin is loaded

## Example: International Weather Plugin

For international users who need weather data from sources other than NOAA, no alternative plugin is needed: `gwx` answers worldwide, and `wx` answers from Open-Meteo when `[Weather] weather_provider = openmeteo`. A source neither of them speaks belongs in `modules/weather_providers/`.

An alternative plugin is still the way to replace a command's whole behavior:

1. Create `modules/commands/alternatives/your_plugin.py`
2. Implement a command that uses the API you need (e.g., OpenWeatherMap, WeatherAPI.com)
3. Set `name = "wx"` to replace the default wx command
4. Optionally add to `config.ini`:
   ```ini
   [Plugin_Overrides]
   wx = your_plugin
   ```

## Best Practices

1. **Keep the same interface**: Alternative plugins should maintain the same keywords and command interface as the default plugin for consistency
2. **Document differences**: Add comments explaining why this alternative is needed and what makes it different
3. **Test thoroughly**: Make sure your alternative plugin works correctly before deploying
4. **Version control**: Consider keeping alternative plugins in a separate repository or clearly marking them as local modifications

## Plugin Loading Order

1. Default plugins are loaded first from `modules/commands/`
2. Configuration-based overrides are applied (from `[Plugin_Overrides]` section)
3. Alternative plugins with matching names automatically replace defaults
4. Standalone alternative plugins (with unique names) are loaded as additional commands

## Troubleshooting

- **Plugin not loading**: Check that your file is in `modules/commands/alternatives/` and has a `.py` extension
- **Wrong plugin loaded**: Verify the `name` attribute matches the command you want to replace
- **Import errors**: Make sure your alternative plugin imports from the correct paths (use relative imports like `from ..base_command import BaseCommand`)
- **Check logs**: The bot logs will show which plugins are loaded and from where

## Inactive Plugins

The `inactive/` subdirectory is for storing alternative plugins you want to keep but not load. Plugins in this directory are completely ignored by the plugin loader.

- Use it to store backup versions, unused alternatives, or plugins you might switch to later
- To activate an inactive plugin, move it from `inactive/` back to the main `alternatives/` directory
- See `inactive/README.md` for more details

## Notes

- Alternative plugins are loaded after default plugins, so they take precedence
- If multiple alternative plugins have the same name, the last one loaded wins
- Configuration-based overrides take precedence over automatic name matching
- The default plugins remain unchanged - alternatives are additive/replacement only
- Plugins in the `inactive/` subdirectory are never loaded, even if configured
- Compatibility modules such as `wx_international.py` are skipped: they define no command class of their own

