# cisco-7941-messenger-bridge

Turns an old Cisco 7941 IP phone into a little desk dashboard: idle-screen
weather and exchange rates, plus a "Services" menu (hard key on the phone)
with a detailed forecast, a currency rates list, a currency converter you
type an amount into with the keypad, and basic infrastructure status.

It runs entirely on your own SIP server -- no cloud service, no CUCM license,
no Cisco account needed. The phone just talks SIP to [Asterisk](https://www.asterisk.org/)
and fetches its screens as Cisco's own push-XML format
(`CiscoIPPhoneText` / `CiscoIPPhoneMenu` / `CiscoIPPhoneInput`) from a small
Python HTTP service.

## What it looks like on the phone

- **Idle screen** (always on, refreshes every minute): time/date, current
  weather, a couple of exchange rates.
- **Services menu** (Services hard key):
  - Weather detail / forecast -- current conditions + next few days
  - Currency rates (list) -- a longer list of CBR exchange rates
  - Currency converter -- pick a direction (e.g. RUB -> USD), type an
    amount on the keypad, get the converted value
  - Server status -- uptime / SIP registration state of the local host,
    and (optionally) a second host's status
  - About the project

## How it works

```
Cisco 7941  --TFTP-->  boot config (SEP<mac>.cnf.xml)  --tells the phone--> where to register (SIP) and where idleURL/servicesURL point

Cisco 7941  --SIP----->  Asterisk (registration only, no calling features needed)

Cisco 7941  --HTTP GET-->  phone-idle/idle.py  --parses/serves-->  CiscoIPPhoneText / Menu / Input XML
                               |
                               +--> api.met.no (weather)
                               +--> cbr.ru (exchange rates)
                               +--> local Asterisk status file (written by astatus.py)
                               +--> optional: a second host's /status endpoint (uknow/vpn-status.py)
```

## Known limitations (tested on this hardware/firmware)

- Tested against a Cisco 7941G running SIP firmware `SIP41.8-5-4S` in
  non-CUCM ("USECALLMANAGER") mode. Other 79xx models/firmware may behave
  differently.
- All push-XML screens (`CiscoIPPhoneText`/`Menu`/`Input`) are parsed as
  **ISO-8859-1 (Latin-1) only** on this firmware -- there is no non-Latin
  text support, regardless of the encoding declared in the HTTP response.
  Keep all screen text ASCII/Latin.
- `CiscoIPPhoneImage` (bitmap screens) does **not** work on this firmware:
  the phone hangs on "Requesting..." indefinitely even though the server
  responds quickly with a valid, correctly-sized payload. There's no bitmap
  fallback implemented here because of this.

## Repo layout

```
phone-idle/
  idle.py                        the HTTP service the phone talks to
  astatus.py                     root-owned helper: polls Asterisk, writes
                                  a status file idle.py can read unprivileged
  phone-idle.service.example     systemd unit template for idle.py
  phone-astatus.service.example  systemd unit template for astatus.py
uknow/
  vpn-status.py                  optional: status endpoint for a second host,
                                  shown in the Server status screen
  vpn-status.service.example     systemd unit template for it
asterisk/
  pjsip.conf.example             minimal PJSIP config for one phone
  extensions.conf.example        minimal dialplan
tftp/
  SEP_TEMPLATE.cnf.xml           TFTP boot config template for the phone
```

Every `*.example` / `*_TEMPLATE.*` file has `{{PLACEHOLDER}}` or
`<PLACEHOLDER>` values you need to fill in -- see "Setup" below. None of the
real, filled-in configs (with your actual SIP password, IPs, MAC) are meant
to be committed; `.gitignore` already excludes the usual filenames.

## Setup

1. **Firmware**: this project does not include Cisco firmware files
   (`*.sbn`, `*.loads`) -- they're Cisco's copyrighted binaries. Source your
   own SIP firmware for your phone model and drop it in your TFTP root
   alongside the boot config.
2. **Asterisk**: copy `asterisk/pjsip.conf.example` and
   `asterisk/extensions.conf.example` into your Asterisk config, fill in
   the placeholders (server IP, a strong random SIP password), and
   `asterisk -rx "core reload"`.
3. **TFTP boot config**: copy `tftp/SEP_TEMPLATE.cnf.xml` to
   `SEP<YOUR_PHONE_MAC>.cnf.xml` (uppercase MAC, no separators) in your TFTP
   root, fill in the placeholders (must match what you put in `pjsip.conf`).
   Point your DHCP server at your TFTP server (option 66 or similar) so the
   phone finds it at boot.
4. **phone-idle service**:
   - `pip install` needs nothing beyond the Python standard library.
   - Copy `phone-idle/idle.py` and `phone-idle/astatus.py` to e.g.
     `/opt/phone-idle/` on your SIP server.
   - Copy `phone-idle.service.example` and `phone-astatus.service.example`
     to `/etc/systemd/system/`, drop the `.example` suffix, fill in the
     environment variables (at minimum `BASE_URL`, `CITY*`, `LAT`/`LON`,
     `TZ_NAME`, `MET_NO_USER_AGENT` -- [met.no asks for a descriptive User-Agent
     with contact info](https://developer.yr.no/doc/TermsOfService/)).
   - `systemctl daemon-reload && systemctl enable --now phone-astatus phone-idle`
5. **Optional second-host status**: if you want the Server status screen to
   also show a second host (e.g. a VPN box), deploy `uknow/vpn-status.py`
   there with `uknow/vpn-status.service.example`, generate a random token
   (`openssl rand -hex 32`), set it as `STATUS_TOKEN` there and as
   `UKNOW_TOKEN`/`UKNOW_HOST`/`UKNOW_PORT` on the phone-idle side, and
   firewall that port to the phone-idle host's IP only.

## License

MIT -- see [LICENSE](LICENSE).
