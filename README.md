# Fleet form bot

Telegram bot ([@work_0rder_bot](https://t.me/work_0rder_bot)) that turns a raw
dispatch message into a filled repair form.

Send it the raw message and it replies with the filled form, field names in bold.
Correct any field by replying to the form with `FIELD: value` lines.

```
DM WOLD                                    FLEET MEMBER: Jacob
212654 | 2335614PLA                        COMPANY: DM WORLD
ISMAEL, HASSAN MOHAMED / ABDI, ABDI ALI    DRIVER NAME: ISMAEL, HASSAN MOHAMED / ...
612-322-8018 / 254-447-8151
                                   ->      TRUCK#: 212654
comcheck driver pay                        TRAILER#: 2335614PLA
                                           ...
TRL hose REPLACE                           RESPONSIBLE PARTY: driver
                                           ISSUE: TRL hose REPLACE
Brothers Truck Repair
11949 Tramway Dr, Cincinnati, OH 45241     PAYMENT METHOD: comcheck
+15134770709                               LOC: 11949 Tramway Dr, Cincinnati, OH 45241
```

## Naming a field directly

Any line may name the field it belongs to, in the message or as a correction:

```
note: shop closes at 5pm      note driver waiting on site
loc: 11949 Tramway Dr         shop: Brothers Truck Repair
```

`note` also works with just a space after it. Every other field needs the colon,
so a carrier actually named "Driver Logistics" is not mistaken for a label.

A prefix the bot does not recognise (`Ph:`, `Addr:`) does not confuse it either
— the line is still read as a phone or an address, and stored exactly as sent.

## The PM form

A message whose first line is `pm` gets a different form back:

```
pm                                         FLEET MEMBER: JACOB
DM WOLD                                    COMPANY: DM WORLD
212654                                     TRUCK: 212654
ISMAEL, HASSAN MOHAMED           ->        DRIVER: ISMAEL, HASSAN MOHAMED

Brothers Truck Repair                      ISSUE: TRK PM SERVICE
11949 Tramway Dr, Cincinnati, OH 45241
+15134770709                               APP DATE & TIME: 08/18/2026

                                           SERVICE:
                                           Brothers Truck Repair
                                           11949 Tramway Dr, Cincinnati, OH 45241
                                           +15134770709
```

The shop's name, address and phone are one `SERVICE` block rather than three
fields. `ISSUE` defaults to `TRK PM SERVICE` (`PM_DEFAULT_ISSUE`) unless the
message says otherwise, `APP DATE & TIME` is today, and `NOTE` and `WO` stay
blank for you to fill in.

## Empty messages

A message with nothing to parse — a stray `1`, `.`, `+` or the like — gets a
blank template back instead of a form full of `NA`. Edit `SAMPLE_TEMPLATE` to
change what it says.

## Commands

| Command | Effect |
| --- | --- |
| `/start`, `/help` | Usage summary |
| `/name Jacob` | Override `FLEET MEMBER` for your account |
| `/last` | Re-send the last form |

## Run locally

```bash
pip install -r requirements.txt
export BOT_TOKEN="123456:ABC..."
python fleet_form_bot.py
```

With no `PORT` set it long-polls, so nothing else is needed.

## Deploy on Render

`render.yaml` defines a free web service. The bot switches to webhook mode on
its own when Render supplies `PORT` and `RENDER_EXTERNAL_URL`.

1. Render dashboard -> **New** -> **Blueprint**, pick this repo.
2. Render reads `render.yaml` and proposes the `work-order-bot` service.
3. Set `BOT_TOKEN` to the token from [@BotFather](https://t.me/BotFather).
   Optionally set `WEBHOOK_SECRET` to any string of `A-Z a-z 0-9 _ -`.
4. **Apply**. First build takes a couple of minutes.

The service registers its own webhook with Telegram at startup — no `setWebhook`
call needed. Logs should show `bot running (webhook)`.

### Free plan caveat

Free instances sleep after ~15 minutes idle. The next message wakes the service,
so the first reply after a quiet spell lands roughly 30–60s late; Telegram retries
delivery, so nothing is lost. Switching the service to Starter removes the delay.

## Configuration

Both live at the top of `fleet_form_bot.py`:

- `CARRIER_ALIASES` — fixes known carrier typos (`DM WOLD` -> `DM WORLD`).
- `SERVICE_FULL_NAME` / `REP_FULL_PHONE` — full shop name and phone, or short forms.

## Notes

- Forms are held in memory, so a restart (or a free-plan sleep) clears `/last`
  and any `/name` overrides. Editing still works across a restart as long as you
  **reply** to the form you want to change — the bot reads the fields back out
  of the message instead of its own memory.
- In a group chat everyone shares one current form, but each person keeps their
  own `FLEET MEMBER` name.
