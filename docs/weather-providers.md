# Weather Providers

`wx` and `gwx` take their forecast from one provider and their alerts from another. You pick each in `config.ini`, and any alerts provider works with any forecast provider. This page covers what each provider offers, where it works, and how the settings fit together.

For the commands themselves (options, examples, location lookup), see [`wx`](command-reference.md#wx-location-option) and [`gwx`](command-reference.md#gwx-location-option) in the command reference.

---

## Forecast providers

`[Weather] weather_provider` sets the forecast source for `wx`. `gwx` has its own setting, `[Gwx_Command] weather_provider`, which defaults to `openmeteo`, so a bot can answer `wx` from NOAA and still offer worldwide forecasts through `gwx`.

| Value | Source | Coverage | Key | Longest forecast | Alerts with `auto` |
|---|---|---|---|---|---|
| `noaa` | National Weather Service (api.weather.gov) | United States and its territories | None | About 7 days | NWS |
| `openmeteo` | Open-Meteo | Worldwide | None | 15 days after today | None |
| `metno` | MET Norway Locationforecast | Worldwide | None | About 9 days | None |
| `eccc` | Environment and Climate Change Canada city pages | Canada, within 150 km of a city page | None | 6 days | ECCC |

Every provider answers current conditions, `tomorrow`, `Nd` and `hourly`. `Nd` accepts 2 to 16 days with any provider, and the reply stops where the provider's forecast does.

**NOAA** forecasts are written by the National Weather Service in English, whatever the bot's language. A place outside NWS coverage gets an error reply unless `openmeteo_fallback` is on (see [Combining providers](#combining-providers)). With the fallback on, a point found to be outside coverage is remembered for 24 hours, and later requests for it go straight to Open-Meteo.

**Open-Meteo** is the default for `gwx`. `[Weather] weather_model` picks the forecast model; leave it unset for Open-Meteo's best match. The free service sometimes stalls. The bot tries three times, waiting a few seconds each time, before it replies "Error fetching weather data from Open-Meteo". Setting `failover_provider` answers those requests from another provider instead.

**MET Norway** identifies the bot to the service with a User-Agent that includes the project's address, as its terms require. `[Weather] metno_user_agent` replaces it. Forecasts are cached for as long as the service says they are current, and a cached forecast up to six hours old is used while the service is down. The data carries no time zones, so local times come from a time zone lookup:

- `tzfpy`, which ships with the Docker images and the installer's optional extras, or `pip install .[tz]`
- `timezonefinder` on Nix
- an estimate from the longitude when neither is installed, or when the lookup doesn't know the point (out at sea, for example). The estimate can be an hour or two off where civil time differs from solar time (Spain, for example).

**Environment Canada** answers from the nearest city page within 150 km. Its text is French when `[Localization] language` is French and English otherwise. A place farther than 150 km from any city page counts as outside coverage. Near the border, a US city can be answered from a Canadian city page (Seattle from Victoria), since the nearest page wins. With `openmeteo_fallback` on, a quick check against a rectangle around Canada comes first, and a place outside the rectangle goes to Open-Meteo without a city-page search. A few places just over the Yukon border in Alaska are answered from Open-Meteo that way, even though a city page is within 150 km.

**Open-Meteo and MET Norway** add a second message for extreme heat, cold, wind, heavy rain or snow, or thunderstorms, worked out from the forecast itself. When the alerts provider has an active alert for the place, the alert summary is sent instead.

---

## Alerts providers

`[Weather] alerts_provider` sets where alerts come from, for both `wx` and `gwx`.

| Value | Source | Coverage | Key | Language |
|---|---|---|---|---|
| `nws` | National Weather Service | United States and its territories | None | English |
| `meteoalarm` | Meteoalarm, through MeteoGate | The EUMETNET member countries (most of Europe, plus a few neighbors) | `meteoalarm_token` | The bot's language when the alert includes it, then English, then the alert's first language |
| `eccc` | Environment and Climate Change Canada | Canada | None | French or English, as for ECCC forecasts |
| `dwd` | German weather service (DWD), through Bright Sky | Germany | None | German when the bot's language is German, otherwise English |

The other values are `auto` (the default) and `none`. `auto` pairs alerts with each command's own forecast provider: NWS alerts for `noaa`, ECCC alerts for `eccc`, and no alerts for `openmeteo` or `metno`. So with `auto`, `wx` and `gwx` can get alerts from different sources, or one can have alerts and the other none. A named alerts provider applies to both. **With Open-Meteo or MET Norway forecasts, set `alerts_provider` yourself to get alerts.**

Alerts show up in two ways:

- With a current-conditions request (`wx Seattle`, no option), active alerts follow as a second message.
- `wx <place> alerts` lists every active alert for the place.

Outside the alerts provider's coverage there is no second message, and `alerts` usually replies "Not available for this weather source". There are two exceptions:

- With NOAA forecasts, NWS alerts and `openmeteo_fallback` off, `alerts` for a place outside the US replies "Error fetching weather data from NOAA", as `wx` always has.
- DWD's coverage check is a rectangle around Germany, so for places just over the border (Prague, Strasbourg) `alerts` replies "No weather alerts".

If the alerts service can't be reached, the forecast still goes out, and `alerts` replies with a fetch error rather than "No weather alerts".

**Meteoalarm** needs an API key. Get one from the [MeteoGate developer portal](https://devportal.meteogate.eu): sign in, choose "Create API Key", and put the key in `[Weather] meteoalarm_token`. Without a key, Meteoalarm alerts stay off and the bot logs a warning. The default `meteoalarm_api_url` is MeteoGate's gateway. A direct re-user token from api.meteoalarm.org needs `meteoalarm_api_url = https://api.meteoalarm.org/edr/v1` instead.

The gateway allows about 50 requests per IP address in each rate-limit window. The bot caches the country list for a day, today's warnings for 10 minutes and earlier days' warnings for 30 minutes, so each country costs about twelve requests an hour. Other bots or scripts behind the same public IP share that allowance. Meteoalarm data is CC BY 4.0, provided by EUMETNET members, and `wx` help credits it.

**DWD** alerts need no key. `wx` help credits DWD while they are in use.

---

## Combining providers

Three settings decide what happens when the forecast provider can't answer. They cover different situations:

| Situation | Setting |
|---|---|
| The place is outside the forecast provider's coverage (`wx Tokyo` on NOAA) | `openmeteo_fallback` |
| The forecast provider timed out, returned an error, or sent an answer the bot can't read | `failover_provider` |
| You want alerts with Open-Meteo or MET Norway forecasts | `alerts_provider` |

**`openmeteo_fallback = true`** answers places outside the forecast provider's coverage from Open-Meteo: outside the US for NOAA, and farther than 150 km from a city page for Environment Canada. It does nothing for Open-Meteo or MET Norway, which cover the whole world.

**`failover_provider`** names a second forecast provider for failures. The reply keeps the first provider's units and place name, so it reads like any other reply. A place outside coverage is not a failure and never fails over. If the second provider fails too, the reply is the first provider's error.

**Alerts are independent of the forecast source,** so a forecast failure still sends the alert summary. The one exception is NOAA forecasts with NWS alerts. Both come from the same service, so when the forecast fails, the bot doesn't send NWS alerts.

**The weather service plugin** (scheduled forecasts and alert posts) has its own settings and doesn't use any of these. See [Weather Service](weather-service.md).

**Units** come from `[Weather] temperature_unit`, `wind_speed_unit` and `precipitation_unit` for every provider, and WXSIM stations use the temperature and wind units the same way. The same keys in `[Wx_Command]` or `[Gwx_Command]` override them for that command.

---

## Example configurations

These set `[Weather] weather_provider`, which is `wx`'s provider. `gwx` stays on Open-Meteo unless `[Gwx_Command] weather_provider` says otherwise, but it shares the alerts, fallback and failover settings.

**United States, all from the National Weather Service.** Places outside the US are answered from Open-Meteo.

```ini
[Weather]
weather_provider = noaa
alerts_provider = auto
openmeteo_fallback = true
```

**United States on Open-Meteo, with MET Norway as a backup and NWS alerts.** Open-Meteo's stalls are answered from MET Norway, and NWS alerts have to be named because `auto` gives none with Open-Meteo.

```ini
[Weather]
weather_provider = openmeteo
failover_provider = metno
alerts_provider = nws
```

**Canada.** Environment Canada forecasts and alerts, with Open-Meteo for remote places far from a city page.

```ini
[Weather]
weather_provider = eccc
alerts_provider = auto
openmeteo_fallback = true
```

**Near the US–Canada border.** `wx` answers US places from NOAA with NWS alerts, and `gwx` answers Canadian places from Environment Canada with ECCC alerts. `auto` gives each command the alerts that go with its own forecast provider.

```ini
[Weather]
weather_provider = noaa
alerts_provider = auto
openmeteo_fallback = true

[Gwx_Command]
weather_provider = eccc
```

**Europe.** MET Norway forecasts, Open-Meteo as the backup, and Meteoalarm alerts.

```ini
[Weather]
weather_provider = metno
failover_provider = openmeteo
alerts_provider = meteoalarm
meteoalarm_token = your-meteogate-key
```

**Germany.** The same, with DWD alerts, which need no key.

```ini
[Weather]
weather_provider = metno
failover_provider = openmeteo
alerts_provider = dwd
```

**Anywhere else.** Worldwide forecasts, no alerts.

```ini
[Weather]
weather_provider = openmeteo
failover_provider = metno
alerts_provider = none
```

**`wx` from NOAA, `gwx` worldwide.** This is the default behavior; the `[Gwx_Command]` line only makes it explicit.

```ini
[Weather]
weather_provider = noaa

[Gwx_Command]
weather_provider = openmeteo
```

---

## Troubleshooting

**"Error fetching weather data from Open-Meteo"** means Open-Meteo didn't answer in three tries, answered with an error, or sent a forecast without current conditions. The first is the usual cause, and it comes and goes on Open-Meteo's side. Set `failover_provider = metno` to answer those requests from MET Norway.

**"Not available for this weather source"** after `alerts` means the alerts provider doesn't cover that place, or the command has no alerts provider. With Open-Meteo or MET Norway forecasts, `auto` means no alerts; set `alerts_provider`.

**Meteoalarm alerts stop for a while and the log mentions a rate limit.** The gateway's allowance is per IP address, so other bots or scripts on the same network use it too. The bot waits for the window to reset and uses cached warnings in the meantime.

**Times in British Columbia are an hour off.** British Columbia moved to permanent UTC−7 in 2026, which needs `pytz` 2026.2 or newer. `pip install -r requirements.txt` brings it in. The Nix package uses nixpkgs' `pytz`, so it follows `flake.lock`.

**MET Norway's hourly times are an hour or two off.** The time zone lookup isn't installed, so the bot is estimating from the longitude. Install it with `pip install .[tz]`, or rerun the installer with its optional extras. The log names the lookup in use the first time it is needed.
