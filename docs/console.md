# PleatView Console: design

Status: design, with the first code in [`console/`](../console). Roadmap: [C1](../ROADMAP.md#c1--pleatview-console).

The PleatView Console manages screens in many places, with or without Home Assistant. It's
a server you install (a Docker image built from this repository) with a web app you open in
a browser or install from it, on a computer, tablet or phone. Its users are installers who
look after several customers, businesses with screens at several sites, and homes with
more than one place.

It's another controller for the same screens: the PleatView apps on Android, Fire TV and
Linux, the light client and later the web player don't change for it.

## Principles

- **A thin agent.** The screens' apps show, play, report and keep their keys. Everything else
  lives in the console: schedules, playlists, policies, media conversion, logs.
- **Tenants are silos.** A tenant's data is stored apart, encrypted with its own key, and
  only reachable by that tenant's members. Nothing is shared between tenants.
- **As little interaction as possible.** Screens on the console's network are found by
  themselves and paired with the PIN they show, as in Home Assistant.
- **Free, open source and auditable.** Only packages from Debian's main archive, on a base
  image pinned by its digest, and a list of every installed package for scanners. The web
  app uses no libraries.
- **Any format.** Media in whatever format and container ffmpeg can read is converted
  on the console into what each screen plays. Screens never get a codec pack.

## How it fits together

```
 Browser or installed app ──HTTPS──▶ Console ──────────────▶ Screens (PleatView apps)
                                     │  ├─ console.db        protocol v1 on the local
                                     │  ├─ tenants/<id>/     network today; v2, where
                                     │  │   tenant.db        screens connect out, with S1
                                     │  │   media/
                                     │  └─ ffmpeg ───────▶  media in any format, converted
                                     └──▶ Renderers (R1)    dashboards rendered for slow screens
```

- **Server:** Python 3 with aiohttp, one process. Long-lived tasks keep a connection to every
  paired screen and follow its state.
- **Storage:** SQLite, which comes with Python. One database for the console (users,
  tenants, memberships, sessions) and **one database per tenant** (sites, screens, groups,
  media, schedules, the tenant's audit log), each in the tenant's own folder.
- **Media:** ffmpeg and ffprobe from Debian, run as separate processes.
- **Web app:** plain HTML, CSS and JavaScript served by the console, installable as a
  progressive web app (a manifest and a service worker that keeps the app's shell offline).

## Tenants, sites and isolation

A **tenant** is one customer or organisation. It holds **sites** (a building, a branch, a
home), and a site holds screens and renderers.

- **Separate databases.** A tenant's records live only in `tenants/<id>/tenant.db`, so no
  query can return another tenant's rows, whatever the code gets wrong. Export is a copy of
  the folder, and deletion removes it.
- **A key per tenant.** Each tenant has a random 256-bit key, stored wrapped (AES-256-GCM)
  by the console's master key. Secrets in a tenant's database, such as the screens' tokens,
  are encrypted with the tenant's key. Deleting a tenant destroys its key, so copies of its
  secrets in old backups can't be read either.
- **The master key** is a file the console makes on first start (`/data/master.key`,
  readable only by the console), or a file you supply apart from the data, such as a Docker
  secret. Backups of `/data` without the master key don't reveal secrets.
- **Console administrators** create tenants and invite their first owner, but don't see
  inside a tenant unless they are made members of it, and that is written to the tenant's
  audit log.
- The rest of a tenant's database is not encrypted inside the file (SQLite can't do that
  without another package), so `/data` belongs on an encrypted disk, as any server's data does.

## Users, roles and sign-in

- **Users** are accounts on the console. A user belongs to one or more tenants, with a role
  in each:
  - **Owner:** members, sites and everything below.
  - **Operator:** pairs, controls and configures screens.
  - **Viewer:** sees screens and logs.
- **Passwords** are hashed with scrypt (N 2^15, r 8, p 3, from OWASP's recommendations), in
  Python's standard library. Nothing is ever stored in plain text.
- **Two-factor sign-in** with an authenticator app (TOTP, RFC 6238), written with the
  standard library, with ten one-time recovery codes for a lost phone. Each code works
  once. A console administrator can require it for everyone; until then, each person
  chooses.
- **Members** are added by an owner, who gets a one-time password to pass on; the member
  chooses their own at the first sign-in. Owners reset passwords and two-factor sign-in only
  for accounts that open no other organisation. Accounts that open several are reset by a
  console administrator. An organisation always keeps at least one owner.
- **Sessions** are random 256-bit tokens in a cookie that scripts can't read
  (`HttpOnly`, `Secure`, `SameSite=Strict`), stored only as hashes. They expire after
  12 hours idle and 7 days at most, and signing out or a password change ends them.
- **Requests that change something** must carry a custom header and come from the console's
  own origin, which stops cross-site request forgery.
- **Wrong passwords** slow down: after 5 failures for a name or an address, sign-in waits a
  minute, doubling up to an hour.

## Screens

- **Found by themselves:** the console listens for PleatView's mDNS announcements
  (`_pleatview._tcp`) on its network, with a small resolver written for it on Python's
  standard library.
- **Paired with the PIN** the screen shows, into a site. The screen keeps only a hash of the
  token it gives the console. The console keeps the token encrypted with the tenant's key.
- **A screen can have several controllers.** One paired with Home Assistant keeps Home
  Assistant's settings and sign-in. The console then only sends it commands, and lists it as
  managed by Home Assistant.
- **Without Home Assistant**, the console keeps a home page and zoom on the screen: its own
  pages, media or any website. The console stamps its settings with a revision and puts them
  back whenever the screen comes back with others, as Home Assistant does. The apps leave
  their setup screen with a home page that is a full web address from 1.0.17
  (`home_without_ha` in their features); older ones only take commands.
- **Moved screens:** when a paired screen announces a new address, the console moves there
  only once the screen proves it issued the console's token, as Home Assistant does.
- **Commands:** show a page, play media, notify, home, reload, standby and wake.

## Media in any format

**Pages** are what screens show as their home page: a web page by its address, or a playlist
of the library's pictures, videos and sounds, which the console serves as a page that plays
them full screen, in order, again and again. **Groups** of screens take a command, or a home
page, all at once.

ffprobe reads every upload: container, codecs, size, length. Then the console makes one file
that every screen plays:

1. If the screen plays the codecs and the container, the file is sent as it is.
2. If only the container doesn't suit it, ffmpeg remuxes it (no quality lost, little work).
3. Otherwise ffmpeg converts it to H.264 or HEVC video and AAC audio in HLS, using the
   graphics' encoder when there is one (VA-API), and keeps the result for the next time.

Live sources such as RTSP cameras, SRT, RTMP, MPEG-TS and HLS are re-streamed the same way.
Screens will report what they decode (a `codecs` list in their state). Until then, the
console uses what each platform is known to play. Debian's ffmpeg has only free codecs, and
it reads nearly every format in use: H.264, HEVC, VP8, VP9, AV1, MPEG-2 and MPEG-4, ProRes,
AAC, MP3, Opus, Vorbis, FLAC, ALAC and PCM, in MP4, MKV, WebM, MOV, AVI, MPEG-TS, Ogg and more.

## Connections and certificates

- The console serves **HTTPS** with a certificate it makes for itself, and logs that
  certificate's fingerprint so the browser's warning can be checked against it. You can also
  give it your own certificate.
- It can instead serve plain HTTP on `127.0.0.1` only, behind a reverse proxy or a ZTNA
  connector that adds TLS with a trusted certificate. That's the usual setup for a console
  reached from outside.
- **Installing the web app** needs a certificate the browser trusts, or `localhost`.
  Browsers don't install apps from sites whose certificate they had to warn about.
- Protocol v1 to the screens is plain HTTP on the local network, as in Home Assistant. M3
  encrypts it, and S1 moves screens to connecting out over TLS with a key of their own.

## Running it

```sh
docker build -t pleatview-console https://github.com/Hustlesman/pleatview.git#main:console
docker run -d --name pleatview-console --restart unless-stopped --network host \
  -v pleatview-console:/data pleatview-console
```

Host networking lets it find screens over mDNS. It listens on port 8790. On first start it
asks for the first administrator's name and password, and sets up a tenant and a site.

| Setting | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8790` | The port for the web app and its API |
| `TLS` | `auto` | `auto`: HTTPS with the console's own certificate. `off`: plain HTTP, for a reverse proxy or ZTNA connector on the same machine |
| `HOST` | `0.0.0.0`, or `127.0.0.1` with `TLS=off` | The address to listen on |
| `TLS_CERT`, `TLS_KEY` | — | Your own certificate and key (PEM files) instead of the console's |
| `MASTER_KEY_FILE` | `/data/master.key` | The master key, best kept apart from `/data` and its backups |
| `DISCOVERY` | `1` | `0` turns off looking for screens over mDNS |
| `SCREEN_PORT`, `SCREEN_HOST` | `8791`, `0.0.0.0` | Where screens load playlists and media: plain HTTP on the local network, like protocol v1, with nothing else on it |
| `SCREEN_URL` | `http://<this machine>:8791` | The address screens use for it, when they reach the console another way |
| `MEDIA_MAX_MB` | `4096` | The largest upload |

To try the web app from another computer without a trusted certificate, run it with
`TLS=off`, forward port 8790 to that computer (an SSH tunnel, or your editor's port
forwarding) and open `http://localhost:8790`. Browsers treat `localhost` as secure, so the
app installs from there.

`/api/packages` (for administrators) and `/app-packages.txt` in the image list every
installed package and version, for audits and vulnerability scans.

## Packages

Approved on 11 Nov 2026, all from Debian 13's main archive, on `debian:trixie-slim` pinned by
digest: `python3`, `python3-aiohttp`, `python3-cryptography` (certificates, encryption,
device keys), `ffmpeg` and `tini`. Anything more needs approval first.

## Threats considered

| Threat | Answer |
| --- | --- |
| One tenant reads another's data | Separate databases and keys; every request is checked against the user's membership of the tenant in its address |
| A stolen backup | Secrets are encrypted with tenant keys, wrapped by a master key kept outside the backup |
| Password guessing | scrypt, growing delays, two-factor sign-in |
| Cross-site requests and scripts | `SameSite=Strict` cookies, a required header, origin checks, a strict Content Security Policy, no inline scripts |
| Someone on the network posing as a screen | Pairing needs the PIN on the screen; moving to a new address needs proof of the token |
| Tokens readable on the local network (protocol v1) | Encrypted connections in M3, connect-out with device keys in S1 |
| A console administrator looking into tenants | Not without membership, and membership is in the tenant's own log |
| A malicious media file | ffmpeg runs as a separate, unprivileged process with time and size limits, and Debian's security updates |

## The work, in order

1. **Foundation** (done 11 Nov): the server, first start, sign-in, tenants and sites,
   discovering and pairing screens, their live state, commands, the audit log, the
   installable web app, the Docker image and tests.
2. Two-factor sign-in, members and roles in the web app (done 11 Nov).
3. The apps' home page without Home Assistant (Android and Linux; done 11 Nov).
4. Groups, pages and the media library with conversion (done 11 Nov).
5. Schedules (done 11 Nov): a page as the home page by day and time, on a screen, a group or
   a site, by the site's clock (the most specific wins). Playlists came with step 4.
6. Many sites and organisations (11 Nov: time zones per site; organisations renamed, exported
   as JSON without secrets, and deleted with their key by an administrator). While the console
   can't be reached, screens keep their home page; a web page goes on showing, and a playlist
   plays on from the last list and the screen's cache, which keeps media a week (videos as
   far as the screen's browser keeps them). Still to come: a full offline copy of playlists
   (it needs HTTPS that screens trust, for a service worker), PleatView Light on screens
   without Home Assistant (it needs a renderer the console drives, R1), and S1's connections
   for sites elsewhere.
