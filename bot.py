"""
Robinhood Chain NFT Activity Tracker — Telegram Bot
=====================================================

Polls Robinhood Chain (Arbitrum-Orbit L2, chain ID 4663) for ERC-721 and
ERC-1155 transfer events (mints, burns, transfers/sales) and pushes
formatted alerts to any Telegram chat that subscribes.

Run:
    python bot.py

Config is read from environment variables / a .env file — see .env.example.
"""

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from eth_abi import decode as abi_decode
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from web3 import Web3

load_dotenv()

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
RPC_URL = os.environ.get("RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
EXPLORER_TX_URL = os.environ.get(
    "EXPLORER_TX_URL", "https://robinhoodchain.blockscout.com/tx/"
)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "8"))
MAX_BLOCK_SPAN = int(os.environ.get("MAX_BLOCK_SPAN", "500"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "state.json"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CONTRACTS_FILE = DATA_DIR / "contracts.json"

ZERO_ADDR = "0x0000000000000000000000000000000000000000"

ERC721_TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
ERC1155_SINGLE_TOPIC = Web3.keccak(
    text="TransferSingle(address,address,address,uint256,uint256)"
).hex()
ERC1155_BATCH_TOPIC = Web3.keccak(
    text="TransferBatch(address,address,address,uint256[],uint256[])"
).hex()

ERC_MIN_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("rh-nft-bot")

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 20}))

_metadata_cache: dict[str, str] = {}

# --------------------------------------------------------------------------
# Tiny JSON-file persistence (no DB needed for this scale)
# --------------------------------------------------------------------------
import json


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning("Could not parse %s, starting fresh", path)
    return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def load_state() -> dict:
    return _load_json(STATE_FILE, {"last_block": None})


def save_state(state: dict) -> None:
    _save_json(STATE_FILE, state)


def load_subscribers() -> set:
    return set(_load_json(SUBSCRIBERS_FILE, []))


def save_subscribers(subs: set) -> None:
    _save_json(SUBSCRIBERS_FILE, sorted(subs))


def load_contracts() -> dict:
    """address(lowercase) -> label"""
    return _load_json(CONTRACTS_FILE, {})


def save_contracts(contracts: dict) -> None:
    _save_json(CONTRACTS_FILE, contracts)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def topic_to_address(topic_hex: str) -> str:
    return Web3.to_checksum_address("0x" + topic_hex[-40:])


def topic_to_int(topic_hex: str) -> int:
    return int(topic_hex, 16)


def get_collection_label(address: str, contracts: dict) -> str:
    address_l = address.lower()
    if address_l in contracts and contracts[address_l]:
        return contracts[address_l]
    if address_l in _metadata_cache:
        return _metadata_cache[address_l]
    label = short_addr(address)
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC_MIN_ABI)
        name = c.functions.name().call()
        try:
            symbol = c.functions.symbol().call()
        except Exception:
            symbol = None
        if name:
            label = f"{name} ({symbol})" if symbol else name
    except Exception:
        pass
    _metadata_cache[address_l] = label
    return label


def classify_and_parse(entry) -> list[dict]:
    """Turn a raw log entry into 0+ normalized NFT-transfer event dicts."""
    topics = [t.hex() for t in entry["topics"]]
    if not topics:
        return []
    topic0 = topics[0]
    address = entry["address"]

    raw_data = entry["data"]
    data_bytes = bytes.fromhex(raw_data[2:]) if isinstance(raw_data, str) else bytes(raw_data)

    events = []

    if topic0 == ERC721_TRANSFER_TOPIC and len(topics) == 4 and len(data_bytes) == 0:
        # ERC-721 Transfer: from/to/tokenId are all indexed, no data payload.
        # (ERC-20 Transfer shares the same signature but only has 3 topics
        # and a non-empty data field carrying the amount, so this check
        # reliably separates the two.)
        events.append(
            {
                "standard": "ERC-721",
                "contract": address,
                "from": topic_to_address(topics[1]),
                "to": topic_to_address(topics[2]),
                "token_id": topic_to_int(topics[3]),
                "amount": 1,
            }
        )
    elif topic0 == ERC1155_SINGLE_TOPIC and len(topics) == 4:
        token_id, amount = abi_decode(["uint256", "uint256"], data_bytes)
        events.append(
            {
                "standard": "ERC-1155",
                "contract": address,
                "from": topic_to_address(topics[2]),
                "to": topic_to_address(topics[3]),
                "token_id": token_id,
                "amount": amount,
            }
        )
    elif topic0 == ERC1155_BATCH_TOPIC and len(topics) == 4:
        ids, amounts = abi_decode(["uint256[]", "uint256[]"], data_bytes)
        for token_id, amount in zip(ids, amounts):
            events.append(
                {
                    "standard": "ERC-1155",
                    "contract": address,
                    "from": topic_to_address(topics[2]),
                    "to": topic_to_address(topics[3]),
                    "token_id": token_id,
                    "amount": amount,
                }
            )

    return events


def fetch_logs(from_block: int, to_block: int, addresses: list[str] | None):
    params = {
        "fromBlock": from_block,
        "toBlock": to_block,
        "topics": [[ERC721_TRANSFER_TOPIC, ERC1155_SINGLE_TOPIC, ERC1155_BATCH_TOPIC]],
    }
    if addresses:
        params["address"] = addresses
    return w3.eth.get_logs(params)


# --------------------------------------------------------------------------
# Broadcasting
# --------------------------------------------------------------------------
async def broadcast_event(app: Application, ev: dict, entry, contracts: dict) -> None:
    subs = load_subscribers()
    if not subs:
        return

    label = get_collection_label(ev["contract"], contracts)

    if ev["from"].lower() == ZERO_ADDR:
        action = "🟢 MINT"
    elif ev["to"].lower() == ZERO_ADDR:
        action = "🔴 BURN"
    else:
        action = "🔁 TRANSFER"

    tx_hash = entry["transactionHash"]
    tx_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else tx_hash
    tx_link = f"{EXPLORER_TX_URL}{tx_hash}"

    amount_txt = f" x{ev['amount']}" if ev["amount"] != 1 else ""
    text = (
        f"{action} — *{label}*\n"
        f"Token #{ev['token_id']}{amount_txt}  ({ev['standard']})\n"
        f"From: `{short_addr(ev['from'])}`\n"
        f"To: `{short_addr(ev['to'])}`\n"
        f"[View transaction]({tx_link})"
    )

    for chat_id in subs:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Failed to deliver message to chat %s", chat_id)


# --------------------------------------------------------------------------
# Poll loop
# --------------------------------------------------------------------------
async def poll_loop(app: Application) -> None:
    state = load_state()
    if state.get("last_block") is None:
        try:
            state["last_block"] = w3.eth.block_number
            save_state(state)
            log.info("Starting from current block %s", state["last_block"])
        except Exception:
            log.exception("Could not reach RPC at startup, will retry")
            state["last_block"] = 0

    while True:
        try:
            latest = w3.eth.block_number
            contracts = load_contracts()
            addr_filter = (
                [Web3.to_checksum_address(a) for a in contracts.keys()]
                if contracts
                else None
            )

            from_block = state["last_block"] + 1
            while from_block <= latest:
                to_block = min(from_block + MAX_BLOCK_SPAN - 1, latest)
                logs = fetch_logs(from_block, to_block, addr_filter)
                for entry in logs:
                    for ev in classify_and_parse(entry):
                        await broadcast_event(app, ev, entry, contracts)

                state["last_block"] = to_block
                save_state(state)
                from_block = to_block + 1

        except Exception:
            log.exception("Poll loop error, will retry after backoff")

        await asyncio.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------
# Telegram commands
# --------------------------------------------------------------------------
HELP_TEXT = (
    "👋 *Robinhood Chain NFT Tracker*\n\n"
    "/subscribe — get live NFT mint/sale/transfer alerts in this chat\n"
    "/unsubscribe — stop alerts here\n"
    "/watching — show which collections are being tracked\n"
    "/watch <address> [label] — (admin) track a specific collection\n"
    "/unwatch <address> — (admin) stop tracking a collection\n"
    "/help — show this message"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subs = load_subscribers()
    subs.add(update.effective_chat.id)
    save_subscribers(subs)
    await update.message.reply_text(
        "✅ Subscribed — you'll get NFT activity here as it happens."
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subs = load_subscribers()
    subs.discard(update.effective_chat.id)
    save_subscribers(subs)
    await update.message.reply_text("🛑 Unsubscribed.")


def _is_admin(update: Update) -> bool:
    return bool(ADMIN_CHAT_ID) and str(update.effective_chat.id) == str(ADMIN_CHAT_ID)


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /watch <contract_address> [label]")
        return
    try:
        address = Web3.to_checksum_address(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid address.")
        return

    label = " ".join(context.args[1:]) if len(context.args) > 1 else None
    contracts = load_contracts()
    contracts[address.lower()] = label or get_collection_label(address, {})
    save_contracts(contracts)
    await update.message.reply_text(
        f"👀 Now watching {contracts[address.lower()]} ({short_addr(address)})"
    )


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unwatch <contract_address>")
        return
    address = context.args[0].lower()
    contracts = load_contracts()
    if address in contracts:
        removed = contracts.pop(address)
        save_contracts(contracts)
        await update.message.reply_text(f"Removed {removed}.")
    else:
        await update.message.reply_text("That address isn't being watched.")


async def cmd_watching(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contracts = load_contracts()
    if not contracts:
        await update.message.reply_text(
            "Tracking ALL NFT activity chain-wide (no specific collections configured "
            "with /watch yet)."
        )
        return
    lines = [f"• {label} — `{addr}`" for addr, label in contracts.items()]
    await update.message.reply_text(
        "Watching:\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN
    )


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    asyncio.create_task(poll_loop(app))


def main() -> None:
    try:
        connected = w3.is_connected()
    except Exception:
        connected = False
    if not connected:
        log.warning(
            "Could not confirm RPC connection at startup (%s) — will keep retrying "
            "once the poll loop starts.",
            RPC_URL,
        )

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("watching", cmd_watching))

    log.info("Bot starting — polling %s every %ss", RPC_URL, POLL_INTERVAL)
    app.run_polling()


if __name__ == "__main__":
    main()
