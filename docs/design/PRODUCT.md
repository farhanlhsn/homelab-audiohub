# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack
Plain static HTML, CSS and vanilla JS, served by the hub itself (Python aiohttp). No build step. The mockup is a single self-contained HTML file.

## Users
One person, the owner of the homelab, Indonesian-speaking and technical. Opens the dashboard from phone and desktop PC about equally (confirmed). It is also embedded as a panel inside Home Assistant (the hub's CSP already allows that frame).

Most frequent jobs (confirmed): see what is playing and through which path; change the volume; connect a new device.

## Product Purpose
Control and observe a homelab audio hub that sends audio from many devices to one Sony ULT Field 1 speaker. Success: at a glance know what is playing, from which device and over which connection; reach volume instantly; connect a new device without reading documentation.

## Positioning
One speaker, every device: Bluetooth, AirPlay 2, DLNA and WFAS all land on the same speaker, the source switches automatically to whatever starts playing, and the latency of each hop is shown with where the number comes from.

## Operating Context
Home LAN only, no login by choice (the owner considers the network trusted). The page polls the hub every 3 seconds. Real status data available per poll: speaker connection, A2DP codec, battery, volume, selected source, route active, connected devices with connection type, now-playing title/artist/album when the sender provides it, latency legs (player to hub, hub to speaker), Bluetooth pairing state and countdown, settings (auto source, auto reconnect, WFAS buffer, PipeWire quantum).

## Capabilities and Constraints
- Sources: Bluetooth in (phone/Mac/PC pairs to "Homelab Audio"), AirPlay 2, DLNA ("Homelab Audio"), WFAS (needs an app on the sender, least preferred), Spotify Connect (non-functional for this account: Spotify refuses the audio key, so it must not be presented as working).
- One source plays at a time; the source that most recently started playing takes over; a toggle locks the selected source.
- Only one Bluetooth phone is used at a time (decided).
- The Sony reports its A2DP delay; every other latency figure is a setting or a protocol figure, and end-to-end latency cannot be measured from the hub. Numbers must carry their provenance.
- AirPlay pause/resume lag is about 2 s by protocol; Bluetooth has no such lag.
- Title metadata only appears when the sender provides it (a Linux PC over AirPlay does not).
- Interface language is Indonesian.

## Brand Commitments
Name: "Audio rumah" with the sub-label "Homelab". Indonesian, short, plain copy. The owner gave full freedom on the visual look ("bebas sebagusnya aja, kalau mau rombak total juga silahkan").

## Evidence on Hand
Screenshots of the current dashboard: /Users/farhanlhsn/audiohub-mockup/shots/current-desktop.png and current-mobile.png. No logos, no photos. Demonstration data in a mockup must be labelled synthetic where it could be mistaken for the real thing.

## Product Principles
1. Status before settings: what is playing, from where, through which path comes first; configuration is secondary.
2. Honest numbers: every figure says whether it was reported by a device, set by the hub, or defined by a protocol.
3. Automatic by default, manual as an exception: choosing a source by hand is a lock, not the main control.
4. No dead ends: a method that does not work is labelled as such with the reason, never shown as available.
5. Phone and desktop are equal citizens; volume and the playing state are reachable without scrolling on a phone.
