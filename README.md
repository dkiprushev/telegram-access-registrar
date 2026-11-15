# Telegram Access Registrar

## Akuvox temporary keys

The `telegram_access_registrar.issue_akuvox_key` action issues a 48-hour key and
sends both the PIN and QR image to an approved Telegram user.  It intentionally
requires the exact relay names on every call; use `Вхід 25-Е` and `Ліфт
пасажирський 25-Е` in the automation.

Home Assistant custom integration for approving recipients of the existing `SmartHouse` Telegram bot before they can receive temporary building-access keys.

## What it does

1. A visitor presses **Start** in `SmartHouse`.
2. The bot sends the configured administrator an approval request with **Allow** and **Reject** buttons.
3. On approval, the visitor's immutable Telegram `chat_id` and current username are stored locally in Home Assistant.
4. Only the administrator can use `/access @username`. The integration fires `telegram_access_registrar_access_requested` with the approved recipient. A Home Assistant automation can then create an Akuvox key and call `telegram_access_registrar.send_message` to deliver it.

The integration does **not** open doors and does not create Akuvox keys itself. Keeping key creation in an explicit Home Assistant automation makes the selected doors, expiry and audit trail visible and reviewable.

## Security model

- Telegram permits only one polling consumer per token. This integration is therefore the **replacement** for Home Assistant's built-in `Telegram bot` integration for the existing `SmartHouse` token; do not leave both integrations polling the same bot.
- Only the configured administrator chat can approve applications or submit `/access`.
- A username is only a convenient lookup label. The authorization record uses Telegram's numeric `chat_id`.
- Pending and approved identities are stored locally through Home Assistant's `.storage`; the token stays in the integration's config entry and is never logged or returned by services.
- The integration must be installed before a public bot username is shared. It sends no access key on `/start`.

## Installation through HACS

1. In HACS, add this GitHub repository as a custom **Integration** repository.
2. Download **Telegram Access Registrar** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Telegram Access Registrar**.
4. Enter the existing `SmartHouse` bot token and the administrator's numeric Telegram chat ID. The administrator must already have pressed **Start** in `SmartHouse`.
5. After this integration is configured, remove the former **Telegram bot** integration that uses the same token. Do this only after the registrar appears loaded; it takes over long polling at that point.

## Services

- `telegram_access_registrar.list_requests` returns local pending and approved records.
- `telegram_access_registrar.approve` and `telegram_access_registrar.reject` are manual fallbacks for a numeric chat ID.
- `telegram_access_registrar.send_message` sends text only to an approved `username`.

## Event for the Akuvox automation

When the configured administrator writes `/access @username`, the integration fires:

```yaml
event_type: telegram_access_registrar_access_requested
event_data:
  recipient:
    chat_id: 123456789
    username: visitor
    display_name: Visitor Name
  requested_by: 393495611
```

Build the Akuvox automation with this event as the trigger. Make it create a 48-hour key for `Вхід 25-Е` and `Ліфт пасажирський 25-Е`; after successful creation, use `telegram_access_registrar.send_message` with `username: "{{ trigger.event.data.recipient.username }}"` to deliver the code.

## Migrating the existing SmartHouse bot

The built-in `telegram_bot` actions use a separate allowlist and will stop when that integration is removed. Before removing it, replace any existing uses of `telegram_bot.send_message` with `telegram_access_registrar.send_message` for approved recipients. The registrar does not copy the core integration's allowlist: the configured administrator is always recognized by the configured numeric ID, while every visitor registers through the same bot.

## Development

```sh
python3 -m pip install -r requirements-dev.txt
pytest
python3 -m compileall custom_components
```
