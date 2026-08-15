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
otherwise a transfer (which includes marketplace sales — decoding actual
sale price would require also parsing the specific marketplace contract,
e.g. Seaport for OpenSea; see "Future work"). By default, with no
collections configured, this tracks **all** NFT activity chain-wide —
use `/watch <address>` to narrow it down.

**New-contract feed** (`/subscribe_new`) — a separate scan reads every
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

### Finding socials

There's no on-chain standard for a contract's X/Telegram, so this is
best-effort, tried in order:
1. **`contractURI()`** — a common (but non-standard) convention where the
   contract points to an off-chain metadata JSON file. Free, no API key.
2. **OpenSea API** — if you set `OPENSEA_API_KEY`, the bot resolves the
   contract to an OpenSea collection slug and pulls its listed socials.
   Won't have anything until OpenSea has indexed the collection, which can
   lag right behind a fresh deploy.

Both are parsed with a schema-agnostic scanner that looks for known field
names *and* regex-matches any embedded `twitter.com`/`x.com`/`t.me` URLs —
so it keeps working even if an API's exact field names change. If nothing
turns up, the alert just says so; you'll often need to check the deployer
address's own recent activity or the chain explorer manually for brand
new, unlisted collections.

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
| `/subscribe_new` | anyone | get alerted the moment a new NFT contract is deployed, plus a follow-up on its first mint |
| `/unsubscribe_new` | anyone | stop new-contract alerts in this chat |
| `/pending` | anyone | list deployed contracts that haven't minted yet |
| `/help` | anyone | show command list |

These two feeds are independent — you can subscribe to either, both, or neither in a given chat.

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
└── data/                    # created at runtime
    ├── state.json            # last processed block, per feed
    ├── subscribers.json      # chat IDs subscribed to the mint feed
    ├── contracts.json        # watched collections (address -> label)
    ├── new_subscribers.json  # chat IDs subscribed to the new-contract feed
    └── candidates.json       # auto-discovered contracts + socials + mint status
```

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
