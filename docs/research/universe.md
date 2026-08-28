# Investable universe — Japan tech / robotics / AI / energy for UK retail

Researched 2026-08-28. Every ticker below is a CANDIDATE: exact T212 lines,
fractional support, KID status, and liquidity must be verified against the real
account via `discover-instruments` before entering `config/whitelist.json`.
Country weights drift with prices and rebalancing.

## UCITS ETFs (LSE-listed, PRIIPs-compliant)

| ETF | Exposure | Approx. Japan weight |
|---|---|---|
| iShares Automation & Robotics (RBOT/RBTX lines) | Global automation/robotics | ~25–35% |
| L&G ROBO Global Robotics & Automation (ROBO) | Global robotics/AI/enabling hardware | ~15–25% |
| Global X Robotics & AI UCITS (BOTZ European lines) | Robotics/AI/semis, concentrated | ~25–35% |
| iShares JPX-Nikkei 400 (IJPA) | Broad Japan quality/growth | ~100% |
| Xtrackers / Amundi MSCI Japan, WisdomTree Japan | Broad Japan | ~100% |
| Nikkei 225 UCITS lines | Broad Japan, tech-heavy (~30%+ tech) | ~100% |

The "Japan" slice of the robotics funds is exactly the target names: Fanuc,
Keyence, Yaskawa, SMC, Murata, Omron, Tokyo Electron.

Energy transition (global thematic, modest Japan weight):
- VanEck Hydrogen Economy (HDRO); L&G Hydrogen Economy; WisdomTree Hydrogen
- WisdomTree Uranium & Nuclear (NCLR); iShares Global Clean Energy (INRG)

Note: Global X Japan Robotics & AI (2638.T) is a Japan-domiciled listing, not
automatically a UK-retail UCITS product — check domicile and KID before
assuming availability.

## Japanese ADRs

Only a handful are on major US exchanges; the rest are OTC, which T212
generally does not carry:

| Major exchange (candidates) | OTC only (likely unavailable) |
|---|---|
| Sony — SONY (NYSE) | Nintendo NTDOY, Keyence KYCCF, Fanuc FANUY, Yaskawa YASKY, Tokyo Electron TOELY, SoftBank Group SFTBY, Renesas RNECY, Omron OMRNY, Murata MRAAY, Advantest ATEYY, Disco DSCSY, Lasertec LSRCY, Mitsubishi Heavy MHVIY, Hitachi HTHIY, Kawasaki Heavy KWHIY |
| Toyota — TM (NYSE) | |
| Honda — HMC (NYSE) | |

ADR cautions: USD exposure on top of the equity, depositary fees, thin OTC
liquidity/wide spreads.

## US-listed ETFs are blocked

US-domiciled ETFs (e.g. US BOTZ) provide no PRIIPs KID, so UK retail platforms
refuse new purchases. Only UCITS lines with a current English-language KID
qualify — check the specific LSE line, not just the fund family name.

## Japan energy-transition names (research leads)

Mitsubishi Heavy (nuclear/hydrogen), Hitachi (nuclear/grid), Kawasaki Heavy
(hydrogen transport), Toyota/Honda (fuel cells — NYSE ADRs), ENEOS, Iwatani,
Japan Steel Works, utilities (TEPCO/KEPCO — high regulatory/political risk).
Mostly OTC-only access ⇒ usually reached indirectly via broad-Japan ETFs.

## Reference allocation (mandate structure, not advice)

~£40 Japan core ETF / ~£40 automation-tech ETF / ≤£15 frontier-or-satellite /
≥£5 cash — satisfies the 40% NAV cap, £15 satellite cap, and £5 cash floor.
See `../MANDATE-GUIDANCE.md` for the frontier-sleeve rules.
