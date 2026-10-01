# Region scopes

MeshCore lets a client confine a channel message to one region of the mesh. The setting is called a region, a region code, or a flood scope depending on where you look; this page calls it a scope. The bot can answer inside the sender's region, post its own messages inside a region, and ignore traffic from regions it should not answer. All of it needs the bot to be told the region names, because they cannot be read off the air.

## How a region works on the air

A client with no region sends each channel message as an ordinary `FLOOD`, and every repeater that hears it rebroadcasts it.

A client with a region set sends a `TC_FLOOD` instead: the same packet plus a 16-bit transport code. The code is an HMAC of the packet's own payload, keyed by a hash of the region name (`#home`, say). A repeater configured for that region computes the same HMAC, sees a match, and forwards the packet. A repeater that denies the region, or does not know it, drops it. That is what keeps a scoped message inside its region.

Two consequences follow:

- **The code changes on every packet.** It is computed over the payload, so two messages scoped to `#home` carry different codes. There is no fixed number that means "home".
- **The code cannot be turned back into a name.** Given a packet and a candidate name, anyone can check whether the code matches. Given only the code, there is nothing to look up. The bot therefore checks the inbound code against each region name it has been given, and a name it was not given can never match.

Scopes only limit forwarding. A companion radio accepts a scoped message whatever region it has set itself, so someone with no region still hears a scoped reply, as long as the repeaters between you carry that region.

## Answering in the sender's region

By default `flood_scopes` is empty. The bot answers channel commands regardless of how they arrived, and the replies go out as ordinary global `FLOOD`, even when the command came in scoped. A repeater that denies `#home` then forwards the reply to a `#home` command, because the reply has no region on it.

To mirror the sender's region, list it:

```ini
[Channels]
flood_scopes = *, #home
```

With that set, a command that arrives scoped to `#home` matches the `#home` entry and the reply is sent scoped to `#home` too. The `*` entry keeps the bot answering people with no region set; leave it out and unscoped commands are ignored.

`flood_scopes` is an allowlist as well as a mirror list. Once it has any entry, the bot only answers messages whose scope it can match against the list. A command scoped to a region you did not list is ignored, not answered globally.

| `flood_scopes` | Command scoped to `#home` | Command with no region |
| --- | --- | --- |
| empty (default) | answered, reply is global `FLOOD` | answered, reply is global `FLOOD` |
| `*, #home` | answered, reply scoped to `#home` | answered, reply is global `FLOOD` |
| `#home` | answered, reply scoped to `#home` | ignored |
| `*` | ignored | answered, reply is global `FLOOD` |

Mirroring depends on RF correlation. The bot matches the code on the packet it actually heard for that message. When it cannot tie the message to a packet, it does not guess from some other recent packet: with a non-empty `flood_scopes` the message is ignored rather than answered at the wrong scope.

## Scoping the bot's own messages

Messages the bot starts itself (scheduled messages, webhooks, feeds, alerts, service plugins) have no inbound packet to mirror. They use the first of these that is set:

1. A scope given for that one send: a webhook request's `flood_scope`, or `channel:#scope:message` in `[Scheduled_Messages]`.
2. `flood_scope` in the plugin or service's own config section.
3. `[Channels] flood_scope.<channel>` for the channel being posted to.
4. `[Channels] outgoing_flood_scope_override`.
5. Global flood.

`outgoing_flood_scope_override` also applies to replies that have no mirrored scope, so a bot with an empty `flood_scopes` and the override set to `#home` answers everything in `#home`. A reply that did mirror a scope keeps it; the override never replaces a matched scope.

```ini
[Channels]
flood_scopes = *, #home
outgoing_flood_scope_override = #home
flood_scope.weather = #sea
```

Any of these accepts `region` or `#region`; the `#` is added if missing. `*`, `0`, `None` or an empty value mean global flood.

A regional send costs 10 bytes of message length, so scoped channel messages are split a little sooner.

## The Radio page

The web viewer's **Radio** page edits `flood_scopes` and `outgoing_flood_scope_override` in its **Region Scopes** card and reloads the bot, so you do not need to restart. Per-channel `flood_scope.<channel>` entries are listed there read-only; edit those in `config.ini`.

The **Default Region Scope** card on the same page is a different setting: the radio's own default, stored in firmware. It does not decide what the bot sends or answers.

## Checking it works

Send a command from a client with the region set, then look for these lines in the bot log:

- `Flood scope allowlist active: ['#home'] (global/unscoped permitted: True)` at startup or reload confirms what `flood_scopes` was parsed to.
- `Incoming TC_FLOOD matched scope '#home'` means the inbound code matched a listed name.
- `Outbound channel flood scope: #home (... set_flood_scope)` means the reply went out scoped.
- `Outbound channel flood scope: global` means it did not. When an override was configured but not applied, the line says so and names the scope that won.
- `Ignoring TC_FLOOD: scope not in flood_scopes allowlist` means the message was scoped to a region you have not listed.

On the air, a scoped reply is a `TC_FLOOD` (route type 0) with a nonzero first transport code; a global reply is a `FLOOD` (route type 1) with no transport codes.

## Related

- [Configuration](configuration.md#channels-section) covers every `[Channels]` key.
- [Region warnings](region-warnings.md) counts how much unscoped traffic your mesh carries and can tell senders to set a region.
