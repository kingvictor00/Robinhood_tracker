# Robinhood Chain NFT Tracker — Telegram Bot

Polls **Robinhood Chain** (Robinhood's Arbitrum-Orbit L2, chain ID `4663`,
explorer at `robinhoodchain.blockscout.com`) for NFT activity — mints,
burns, and transfers/sales across ERC-721 and ERC-1155 collections — and
pushes live alerts to any Telegram chat that subscribes.

## How it works

1. Every `POLL_INTERVAL_SECONDS`, the bot calls `eth_getLogs` against your
   configured RPC endpoint, filtered to the `Transfer`, `TransferSingle`,
   and `TransferBatch` event signatures.
2. It classifies each log as ERC-721 or ERC-1155 (ERC-721's `Transfer`
   shares a signature with ERC-20, so the bot distinguishes them by topic
   count / empty data payload — a standard, reliable heuristic).
3. `from == 0x000...0` → mint, `to == 0x000...0` → burn, otherwise a
   transfer (which includes marketplace sales — decoding actual sale price
   would require also parsing the specific marketplace contract, e.g.
   Seaport for OpenSea; see "Future work" below).
4. Matching events are formatted and sent to every chat in
   `data/subscribers.json`.
5. Progress is checkpointed in `data/state.json` so a restart resumes from
   the last processed block instead of re-scanning the chain.

By default (no collections configured) it tracks **all** NFT activity
chain-wide. Use `/watch` to narrow it to specific collections.

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
| `/subscribe` | anyone | start receiving alerts in this chat |
| `/unsubscribe` | anyone | stop alerts in this chat |
| `/watching` | anyone | list which collections are tracked |
| `/watch <address> [label]` | admin only | narrow tracking to a specific collection |
| `/unwatch <address>` | admin only | remove a collection from the watch list |
| `/help` | anyone | show command list |

To find contract addresses to `/watch`, browse
[Robinhood Chain collections on OpenSea](https://opensea.io/collections/chain/robinhood)
or the [Blockscout explorer](https://robinhoodchain.blockscout.com).

## Project layout

```
rh-nft-bot/
├── bot.py              # everything: RPC polling, event parsing, Telegram bot
├── requirements.txt
├── .env.example
├── Dockerfile
└── data/                # created at runtime
    ├── state.json        # last processed block
    ├── subscribers.json  # chat IDs to notify
    └── contracts.json    # watched collections (address -> label)
```

## Limitations / future work

- **Sale price** isn't shown — a raw `Transfer` event doesn't carry price.
  To show sale prices you'd additionally decode marketplace contract events
  (e.g. Seaport's `OrderFulfilled` for OpenSea) and correlate them with the
  `Transfer` in the same transaction.
- **No reorg handling** — the bot doesn't re-verify recently "finalized"
  blocks. Robinhood Chain is an L2 with fast soft-confirmations, so deep
  reorgs are unlikely, but for a production feed you may want to lag a few
  blocks behind the tip before broadcasting.
- **Public RPC rate limits** — under heavy chain activity, switch to a paid
  RPC provider and/or raise `POLL_INTERVAL_SECONDS`.
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
