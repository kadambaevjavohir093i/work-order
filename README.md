# Fleet form bot

Telegram bot ([@work_0rder_bot](https://t.me/work_0rder_bot)) that turns a raw
dispatch message into a filled repair form.

Send it the raw message and it replies with the filled form, field names in bold.
Correct any field by replying with `FIELD: value` lines.

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
  and any `/name` overrides.
- In a group chat everyone shares one current form, but each person keeps their
  own `FLEET MEMBER` name.
