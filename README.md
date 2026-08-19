# Robinhood Chain NFT Tracker — Telegram Bot

Polls **Robinhood Chain** (Robinhood's Arbitrum-Orbit L2, chain ID `4663`,
explorer at `robinhoodchain.blockscout.com`) for NFT activity — mints,
burns, and transfers/sales across ERC-721 and ERC-1155 collections — and
pushes live alerts to any Telegram chat that subscribes.

## Two independent feeds

**Mint/sale/transfer feed** (`/subscribe`) — every `POLL_INTERVAL_SECONDS`,
the bot calls `eth_getLogs`, filtered to the `Transfer`, `TransferSingle`,
and `TransferBatch` event signatures, and classifies each log as ERC-721 or
ERC-1155 (ERC-721's `Transfer` shares a signature with ERC-20, so the bot
distinguishes them by topic count / empty data payload — a standard,
reliable heuristic). `from == 0x000...0` → mint, `to == 0x000...0` → burn,
otherwise a transfer. By default, with no collections configured, this
tracks **all** NFT activity chain-wide — use `/watch <address>` to narrow
it down. Each alert looks like:

```
🟢 Doodboys
MINT #2107  (ERC-721)

Floor: $1,240.50  |  Chain: Robinhood
Supply: 3,333
Contract: 0xAbC1230000000000000000000000000000dEaD
Buyer: 0xDef4560000000000000000000000000000bEEf  (3 mints)

[Twitter] | [Telegram]
[View transaction]
```

Transfers/sales use the same layout with "To" instead of "Buyer" (no mint
count); burns show "Burner". A couple of fields are best-effort and will
show `N/A` when unavailable rather than guessing:
- **Floor price** comes from OpenSea's API (needs `OPENSEA_API_KEY`),
  converted to USD using the native token's price from CoinGecko — `N/A`
  if the collection isn't OpenSea-listed or no key is configured, or shown
  in the native token if the USD conversion itself fails.
- **Supply** comes from the contract's `totalSupply()` — `N/A` if the
  contract doesn't implement it (not every ERC-721/1155 does).
- **Mint count** is tracked by the bot itself, per buyer per contract,
  starting from whenever the bot began running (no historical backfill).

**`/subscribe` has its own dedicated throttle**, separate from the general
limiter used by the other feeds: up to `MINT_FEED_MAX_PER_WINDOW` alerts
(default 10), with a `MINT_FEED_PER_SEND_DELAY_SECONDS` pause (default 5s)
between each one — then once the window's limit is hit, a full
`MINT_FEED_COOLDOWN_SECONDS` cooldown (default 60s) before it resumes.
The new-contract feed and first-mint follow-ups use a separate, simpler
limiter (`RATE_LIMIT_MAX_MESSAGES` / `RATE_LIMIT_COOLDOWN_SECONDS`,
default 15 alerts then a 10s pause) — busy activity on one feed doesn't
throttle the other.

**New-contract feed** (`/subscribe_new`, or `/subscribenew` — both work)
— a separate scan reads every
block for contract-creation transactions, checks each new contract against
ERC-165 (`supportsInterface`) for ERC-721/1155, and — if it matches —
alerts immediately, before the contract has necessarily minted anything.
It also tries to find the collection's socials (see below), and remembers
the contract so that when it later mints for the first time, subscribed
chats get a follow-up "now live" alert. `/pending` lists everything
tracked that hasn't minted yet.

Both feeds checkpoint their progress in `data/state.json` independently,
so a restart resumes from the last processed block instead of re-scanning
the chain.

### Finding socials & floor price

There's no on-chain standard for a contract's X/Telegram or a live floor
price, so both are best-effort, tried in order:
1. **`contractURI()`** (socials only) — a common (but non-standard)
   convention where the contract points to an off-chain metadata JSON
   file. Free, no API key.
2. **OpenSea API** (socials + floor price) — if you set `OPENSEA_API_KEY`,
   the bot resolves the contract to an OpenSea collection slug and pulls
   its listed socials and floor price. Won't have anything until OpenSea
   has indexed the collection, which can lag right behind a fresh deploy.
   Results are cached per contract for `OPENSEA_CACHE_TTL_SECONDS`
   (default 5 min) to avoid hammering the API during a busy mint.

Socials are parsed with a schema-agnostic scanner that looks for known
field names *and* regex-matches any embedded `twitter.com`/`x.com`/`t.me`
URLs — so it keeps working even if an API's exact field names change. If
nothing turns up, the alert says so explicitly (e.g. "Socials: none found
(contractURI: not implemented by this contract / OpenSea: skipped (no
OPENSEA_API_KEY configured))") instead of just staying quiet about it.

## 1. Create your bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` →
   follow the prompts → copy the token it gives you.
2. Message [@userinfobot](https://t.me/userinfobot) to get your own numeric
   Telegram user ID (used for admin-only commands).

## 2. Configure

```bash
cd rh-nft-bot
cp .env.example .env
# edit .env: paste TELEGRAM_BOT_TOKEN and ADMIN_CHAT_ID
```

The default `RPC_URL` is Robinhood Chain's public endpoint. It's free but
rate-limited — fine for testing, but for 24/7 production use swap in a
provider Robinhood officially recommends: **Alchemy, Quicknode, Blockdaemon,
dRPC, or Chainstack** (all have a free tier). Just replace `RPC_URL` in
`.env`.

## 3. Run it

### Locally
```bash
pip install -r requirements.txt
python bot.py
```

### With Docker
```bash
docker build -t rh-nft-bot .
docker run -d --name rh-nft-bot --env-file .env -v $(pwd)/data:/app/data rh-nft-bot
```

### Keeping it running 24/7
For a VPS without Docker, run it under `systemd` or `pm2`/`supervisor` so it
restarts automatically on crash or reboot. A minimal systemd unit:

```ini
[Unit]
Description=Robinhood Chain NFT Tracker
After=network.target

[Service]
WorkingDirectory=/opt/rh-nft-bot
ExecStart=/opt/rh-nft-bot/venv/bin/python bot.py
Restart=always
EnvironmentFile=/opt/rh-nft-bot/.env

[Install]
WantedBy=multi-user.target
```

## 4. Use it in Telegram

Add the bot to a group (or DM it directly), then:

| Command | Who | What it does |
|---|---|---|
| `/subscribe` | anyone | start receiving mint/sale/transfer alerts in this chat |
| `/unsubscribe` | anyone | stop mint/sale/transfer alerts in this chat |
| `/watching` | anyone | list which collections the mint feed is tracking |
| `/watch <address> [label]` | admin only | narrow the mint feed to a specific collection |
| `/unwatch <address>` | admin only | remove a collection from the watch list |
| `/subscribe_new` (or `/subscribenew`) | anyone | get alerted the moment a new NFT contract is deployed, plus a follow-up on its first mint |
| `/unsubscribe_new` (or `/unsubscribenew`) | anyone | stop new-contract alerts in this chat |
| `/pending` | anyone | list deployed contracts that haven't minted yet |
| `/trending` | anyone | top 10 collections by mint activity in the last 24h |
| `/help` | anyone | show command list |

These two feeds are independent — you can subscribe to either, both, or neither in a given chat.

To find contract addresses to `/watch`, browse
[Robinhood Chain collections on OpenSea](https://opensea.io/collections/chain/robinhood)
or the [Blockscout explorer](https://robinhoodchain.blockscout.com).

## Project layout

```
rh-nft-bot/
├── bot.py              # everything: RPC polling, event parsing, Telegram bot
├── watchdog.py          # independent crash/downtime detector (see below)
├── requirements.txt
├── .env.example
├── Dockerfile
└── data/                    # created at runtime
    ├── state.json            # last processed block, per feed
    ├── subscribers.json      # chat IDs subscribed to the mint feed
    ├── contracts.json        # watched collections (address -> label)
    ├── new_subscribers.json  # chat IDs subscribed to the new-contract feed
    ├── candidates.json       # auto-discovered contracts + socials + mint status
    ├── opensea_cache.json    # cached socials/floor-price lookups (TTL'd)
    ├── mint_counts.json      # per-buyer, per-contract mint counts
    ├── trending.json         # per-contract mint timestamps + seen holders (feeds /trending)
    ├── live_trending.json    # which /trending messages are being kept live-updated
    ├── heartbeat.txt         # timestamp bot.py updates while alive
    └── watchdog_state.json   # whether watchdog.py has already alerted
```

## /trending

Every mint the bot observes (across the whole chain, or just watched
collections if you've configured `/watch`) feeds a running tally — this
happens regardless of whether anyone's subscribed to the mint feed, so
data keeps accumulating in the background either way. `/trending` ranks
the top 10 by mint count within `TRENDING_WINDOW_SECONDS` (default 24h):

```
🔥 Robinhood

1: Doodboys | floor: $13.00 | supply: 2,000 | holders: 1,500+
2: TOADLAYER | floor: $84.10 | supply: 766,857 | holders: 340+
...
```

**It's a live table, not a one-shot reply.** Once sent, that message keeps
editing itself in place every `LIVE_TRENDING_REFRESH_SECONDS` (default
60s) — no need to re-run `/trending` to see updated numbers. Running
`/trending` again in the same chat just retargets the live updates to the
newest message. If a message stops being editable (deleted, or the bot's
been removed from the chat), the bot notices on the next refresh attempt
and stops trying — it doesn't retry forever.

Two honest caveats baked into the design:
- **This is "trending since the bot started," not all-time.** There's no
  historical indexer here — the mint-time window and holder counts only
  reflect what's happened while the bot has been running.
- **Holder counts are a lower bound**, shown with a `+`. It's the number
  of distinct addresses the bot has seen mint from that contract — not a
  true current-holder count (which would need tracking every subsequent
  transfer too), and it can't count anyone who minted before the bot
  started.

## Online / hibernating notifications

Two layers, because the bot obviously can't report on itself once it's
actually dead:

- **Graceful stop/restart** — Ctrl+C, `docker stop`, `systemctl stop`.
  bot.py sends "🟢 Bot is online" on startup and "😴 Bot is currently
  hibernating (shutting down)" right before it exits. Admin-only by
  default; set `NOTIFY_ALL_SUBSCRIBERS_ON_STATUS=true` to broadcast to
  everyone subscribed to either feed instead.
- **Crashes, OOM kills, server reboots** — anything that kills the process
  without warning. bot.py writes a heartbeat file every
  `HEARTBEAT_INTERVAL_SECONDS` (default 30s) while it's alive. A separate
  script, `watchdog.py`, checks whether that heartbeat has gone stale and
  sends the hibernating alert itself — it makes a raw call to the
  Telegram Bot API rather than depending on bot.py or python-telegram-bot,
  so a bug in the main process can't take the alerting down with it. It
  also announces recovery once the heartbeat resumes, and only alerts
  once per outage rather than spamming on every check.

  Run it on a schedule via cron:
  ```bash
  crontab -e
  # add:
  */5 * * * * cd /path/to/rh-nft-bot && /path/to/venv/bin/python watchdog.py
  ```
  Set `HEARTBEAT_STALE_SECONDS` comfortably above `HEARTBEAT_INTERVAL_SECONDS`
  (default 120s vs 30s) so one slow tick doesn't trigger a false alarm.

## Limitations / future work

- **Sale price** isn't shown — a raw `Transfer` event doesn't carry price.
  To show sale prices you'd additionally decode marketplace contract events
  (e.g. Seaport's `OrderFulfilled` for OpenSea) and correlate them with the
  `Transfer` in the same transaction.
- **New-contract detection is forward-looking only** — it finds contracts
  deployed *after* the bot starts running, not a historical backlog.
  Catching pre-existing deployed-but-not-minting contracts would need a
  one-off backfill scan of the chain's full history, which isn't included.
- **New-contract scan is heavier on the RPC** than the mint feed, since it
  has to fetch every block (not just filtered logs) to find
  contract-creation transactions. Keep `NEW_CONTRACT_BLOCK_SPAN` small on
  the free public RPC, or use a paid provider if the chain is busy.
- **Socials coverage is best-effort** — plenty of brand-new contracts won't
  have `contractURI()` set and won't be OpenSea-indexed yet, so some
  `/subscribe_new` alerts will show "socials: none found yet."
- **No reorg handling** — the bot doesn't re-verify recently "finalized"
  blocks. Robinhood Chain is an L2 with fast soft-confirmations, so deep
  reorgs are unlikely, but for a production feed you may want to lag a few
  blocks behind the tip before broadcasting.
- **Public RPC rate limits** — under heavy chain activity, switch to a paid
  RPC provider and/or raise the poll intervals.
- Metadata (collection name/symbol) is fetched on-chain via `name()`/
  `symbol()` and cached in memory; some contracts may not implement these
  and will just show a shortened address instead.

## Notes on this build

I wasn't able to execute this against live Robinhood Chain infrastructure
while building it — my current environment doesn't have outbound network
access — so I couldn't do an end-to-end live test. I did verify the code
compiles cleanly and double-checked the event-signature math and the
ERC-721-vs-ERC-20 disambiguation logic, which is a well-established pattern.
Test it against the public RPC (or better, a paid provider) before relying
on it, and shout if you hit an error — happy to debug.
