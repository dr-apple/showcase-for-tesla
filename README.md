# Showcase for Tesla

Work in progress: wireless CarPlay in the Tesla browser, built on [Showcase](https://github.com/amineross/showcase) by Amine Rostane (software CarPlay receiver for jailbroken iPads). The iPad receives CarPlay from the iPhone and forwards the H.264 stream unchanged over a secure WebSocket to a page in the Tesla browser, which decodes it with WebCodecs onto a canvas and sends touches back. Audio stays on the iPhone → Bluetooth → Tesla.

## Sileo source

```text
https://dr-apple.github.io/showcase-for-tesla
```

## Current package: network test (0.2~nettest2)

Not CarPlay yet. Checks that the Tesla can reach the iPad over the iPad's hotspot.

- Needs Showcase (or Showcase-hud) installed and connected, so the hotspot is up. Depends on `python3`, `openssl` and `network-cmds`.
- Status page on the iPad itself (Safari), always available: `http://127.0.0.1:8080` – shows whether the service runs, the hotspot was found, 100.96.0.1 is set, and the last error.
- While the hotspot (172.20.10.1) is up, a background service adds `100.96.0.1` to the hotspot interface (the Tesla browser blocks private addresses but allows 100.64.0.0/10) and serves a test page on:
  - `http://100.96.0.1/` and `https://100.96.0.1/`
  - `http://172.20.10.1/` and `https://172.20.10.1/` (self-signed certificate)
- The page shows whether it counts as a secure context, whether WebCodecs is there, and whether a (secure) WebSocket works. `/log` lists every visit.
- When the hotspot stops, the servers stop and the extra address is removed. Nothing listens at home.

The service source is in [`src/nettestd.py`](src/nettestd.py). The browser capability test is at <https://dr-apple.github.io/showcase-hud/tesla-test/>.

Unofficial, not affiliated with the Showcase author or Tesla. GPL-3.0.
