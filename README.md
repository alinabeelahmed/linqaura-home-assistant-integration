# LinqAura for Home Assistant

Control LinqAura smart switches from Home Assistant, either **directly on your home network** or through your
**LinqAura account**.

- **Direct (local):** Home Assistant finds your switches by itself and controls them on the home network, with no
  cloud involved, so they keep working when the internet is down.
- **Cloud:** sign in once and every switch on your account appears, owned and shared, with live updates.

## Install with HACS

1. In HACS, open the menu (⋮) and choose **Custom repositories**. Add
   `https://github.com/alinabeelahmed/linqaura-home-assistant-integration` with type **Integration**.
2. Find **LinqAura** in HACS and click **Download**.
3. Restart Home Assistant.

**Without HACS:** copy `custom_components/linqaura` into your Home Assistant `config/custom_components/` folder, then
restart Home Assistant.

## Set up

Each switch is connected one way: **Direct (local control)** or **Cloud**, never both.

### Found automatically

Your switches show up under **Settings → Devices & services → Discovered**. Click **Add** on one and choose how to
connect it:

- **Direct (local control).** Sign in to your LinqAura account once, and Home Assistant reads the switch's key. The
  sign-in is ended right away and only the key is kept. If your LinqAura account is already added to Home Assistant,
  the key is read from it and you don't need to sign in.
- **Cloud.** Sign in to your LinqAura account. Every switch on the account is added, with live updates. Home
  Assistant keeps only a sign-in token, never your password. After this, switches on that account are no longer
  offered under Discovered.

### Add it yourself

Go to **Settings → Devices & services → Add integration → LinqAura**, then:

- choose **Cloud (LinqAura account)** and sign in with the e-mail and password you use in the LinqAura app; or
- choose **Direct (local control) for one switch** and enter the switch's IP address. Use this when discovery doesn't
  find the switch, for example when Home Assistant is on a different network or VLAN.

If a switch on your cloud account is later added directly, it leaves the cloud entry, so it never shows twice. Its
entities keep their names and IDs.

## What you get

| | Direct | Cloud |
|---|---|---|
| Four buttons, with the names from the app | ✓ | ✓ |
| Child lock | ✓ | ✓ (owner or Full access) |
| Touch lock per button (disabled by default) | ✓ | |
| Wi-Fi signal, uptime (disabled by default) | ✓ | |
| Firmware update | | ✓ (owner) |

**Direct:**

- Polled every 5 seconds.
- Needs Home Assistant and the switch on the same network.
- Uses one of the switch's three local sessions. The LinqAura app on your phones uses the others.

**Cloud:**

- Changes arrive straight away over a live connection.
- Needs internet.

## Troubleshooting

- **A switch isn't discovered.** Discovery (mDNS) doesn't cross subnets. Add the switch by address instead. A switch
  in setup mode isn't offered: finish adding it in the LinqAura app first.
- **"The switch no longer accepts Home Assistant's key".** Someone lost access to the switch, so LinqAura made a new
  key. Click **Reconfigure** or the repair notice to read the new key. The switch must have been online since then.
- **Your phones get signed out after restoring a Home Assistant backup.** The backup held an old sign-in token, and
  LinqAura treats a reused token as stolen. Sign in again on the phones. Reauthenticate the cloud entry if Home
  Assistant asks you to.
- **Switches show twice.** Turn off the switch's own local MQTT (Home Assistant discovery) in the LinqAura app, or
  don't use it together with this integration.

## Remove

Go to **Settings → Devices & services → LinqAura**, open the entry's menu (⋮) and choose **Delete**. Then remove the
integration in HACS.

## Development

The tests run in the Home Assistant container. Nothing big is downloaded, and `scripts/test.sh` explains the two
small patches the test harness needs on HA 2026.11:

```sh
docker run --rm -v "$PWD:/w" -w /w --entrypoint sh ghcr.io/home-assistant/home-assistant:stable scripts/test.sh
```

To regenerate `translations/en.json` after editing `strings.json`, run `python scripts/gen_translations.py` with Home
Assistant installed.

The protocol code in `custom_components/linqaura/api/` has no Home Assistant imports. The protocol is documented in
the LinqAura docs repository (`docs/contracts/local_protocol_v2.md`), and the design in
`docs/specs/2026-11-25-home-assistant-integration-design.md`.
