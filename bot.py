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
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from eth_abi import decode as abi_decode
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
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
EXPLORER_ADDR_URL = os.environ.get(
    "EXPLORER_ADDR_URL", "https://robinhoodchain.blockscout.com/address/"
)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "8"))
MAX_BLOCK_SPAN = int(os.environ.get("MAX_BLOCK_SPAN", "500"))

# --- new-contract detection ---
NEW_CONTRACT_BLOCK_SPAN = int(os.environ.get("NEW_CONTRACT_BLOCK_SPAN", "50"))
NEW_CONTRACT_POLL_INTERVAL = int(
    os.environ.get("NEW_CONTRACT_POLL_INTERVAL_SECONDS", "15")
)

# --- socials + floor-price lookup ---
OPENSEA_API_KEY = os.environ.get("OPENSEA_API_KEY", "")
OPENSEA_CHAIN_SLUG = os.environ.get("OPENSEA_CHAIN_SLUG", "robinhood")
OPENSEA_CACHE_TTL = int(os.environ.get("OPENSEA_CACHE_TTL_SECONDS", "300"))
SUPPLY_CACHE_TTL = int(os.environ.get("SUPPLY_CACHE_TTL_SECONDS", "30"))
NATIVE_CURRENCY_SYMBOL = os.environ.get("NATIVE_CURRENCY_SYMBOL", "ETH")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = DATA_DIR / "state.json"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CONTRACTS_FILE = DATA_DIR / "contracts.json"
NEW_SUBSCRIBERS_FILE = DATA_DIR / "new_subscribers.json"
CANDIDATES_FILE = DATA_DIR / "candidates.json"
OPENSEA_CACHE_FILE = DATA_DIR / "opensea_cache.json"
MINT_COUNTS_FILE = DATA_DIR / "mint_counts.json"

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
    {
        "constant": True,
        "inputs": [],
        "name": "contractURI",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]

ERC165_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "interfaceId", "type": "bytes4"}],
        "name": "supportsInterface",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    }
]
IFACE_ERC721 = bytes.fromhex("80ac58cd")
IFACE_ERC1155 = bytes.fromhex("d9b67a26")

SUPPLY_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    }
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


def load_new_subscribers() -> set:
    return set(_load_json(NEW_SUBSCRIBERS_FILE, []))


def save_new_subscribers(subs: set) -> None:
    _save_json(NEW_SUBSCRIBERS_FILE, sorted(subs))


def load_candidates() -> dict:
    """address(lowercase) -> {standard, label, deployer, tx_hash, block, minted, socials}"""
    return _load_json(CANDIDATES_FILE, {})


def save_candidates(candidates: dict) -> None:
    _save_json(CANDIDATES_FILE, candidates)


def load_opensea_cache() -> dict:
    return _load_json(OPENSEA_CACHE_FILE, {})


def save_opensea_cache(cache: dict) -> None:
    _save_json(OPENSEA_CACHE_FILE, cache)


def load_mint_counts() -> dict:
    """contract(lowercase) -> {buyer(lowercase) -> count}"""
    return _load_json(MINT_COUNTS_FILE, {})


def save_mint_counts(counts: dict) -> None:
    _save_json(MINT_COUNTS_FILE, counts)


def bump_mint_count(contract: str, buyer: str) -> int:
    """Tracks how many times this address has minted from this contract
    since the bot started running (no historical backfill)."""
    counts = load_mint_counts()
    c_key, b_key = contract.lower(), buyer.lower()
    counts.setdefault(c_key, {})
    counts[c_key][b_key] = counts[c_key].get(b_key, 0) + 1
    save_mint_counts(counts)
    return counts[c_key][b_key]


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


def detect_token_standard(address: str) -> str | None:
    """Best-effort ERC-165 check for ERC-721 / ERC-1155. Returns None if
    neither interface is confirmed (includes contracts that don't
    implement ERC-165 at all, e.g. plain scripts, EOAs-that-aren't, etc.)."""
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC165_ABI)
    except Exception:
        return None
    try:
        if c.functions.supportsInterface(IFACE_ERC721).call():
            return "ERC-721"
    except Exception:
        pass
    try:
        if c.functions.supportsInterface(IFACE_ERC1155).call():
            return "ERC-1155"
    except Exception:
        pass
    return None


def ipfs_to_http(uri: str) -> str:
    if uri.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + uri[len("ipfs://") :]
    return uri


def fetch_json_url(url: str) -> dict | None:
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "rh-nft-bot/1.0"})
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


_TWITTER_RE = re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/[A-Za-z0-9_]+")
_TELEGRAM_RE = re.compile(r"https?://(?:t(?:elegram)?\.me)/[A-Za-z0-9_+]+")

# Known field names across OpenSea v1/v2 and common contractURI conventions.
_KNOWN_SOCIAL_KEYS = {
    "twitter": {"twitter_username", "twitter", "twitter_url"},
    "telegram": {"telegram_url", "telegram", "chat_url"},
    "website": {"external_url", "external_link", "website"},
}


def extract_socials_generic(data) -> dict:
    """Walk an arbitrary JSON structure and pull out anything that looks
    like a social link — both via known field names and by regex-matching
    URLs, so this keeps working even if an API's schema shifts."""
    found: dict = {}

    def visit(node):
        if isinstance(node, dict):
            for k, v in node.items():
                kl = k.lower()
                if isinstance(v, str) and v:
                    for social, keys in _KNOWN_SOCIAL_KEYS.items():
                        if kl in keys and social not in found:
                            val = v
                            if social == "twitter" and not val.startswith("http"):
                                val = f"https://x.com/{val.lstrip('@')}"
                            if social == "telegram" and not val.startswith("http"):
                                val = f"https://t.me/{val.lstrip('@')}"
                            found[social] = val
                    if "twitter" not in found:
                        m = _TWITTER_RE.search(v)
                        if m:
                            found["twitter"] = m.group(0)
                    if "telegram" not in found:
                        m = _TELEGRAM_RE.search(v)
                        if m:
                            found["telegram"] = m.group(0)
                visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(data)
    return found


def get_contract_uri_socials(address: str) -> tuple[dict, str]:
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC_MIN_ABI)
        uri = c.functions.contractURI().call()
    except Exception:
        return {}, "contractURI: not implemented by this contract"
    if not uri:
        return {}, "contractURI: empty"
    data = fetch_json_url(ipfs_to_http(uri))
    if not data:
        return {}, "contractURI: set, but metadata couldn't be fetched"
    found = extract_socials_generic(data)
    if found:
        return found, "contractURI: found socials"
    return {}, "contractURI: metadata has no social links"


def _opensea_headers() -> dict:
    return {"X-API-KEY": OPENSEA_API_KEY, "Accept": "application/json"}


def _opensea_resolve_slug(address: str) -> tuple[str | None, str]:
    try:
        r = requests.get(
            f"https://api.opensea.io/api/v2/chain/{OPENSEA_CHAIN_SLUG}/contract/{address}",
            headers=_opensea_headers(),
            timeout=10,
        )
    except Exception as exc:
        return None, f"OpenSea: contract lookup failed ({exc.__class__.__name__})"
    if r.status_code == 404:
        return None, "OpenSea: contract not indexed yet"
    if r.status_code != 200:
        return None, f"OpenSea: contract lookup failed (HTTP {r.status_code})"
    slug = r.json().get("collection")
    if not slug:
        return None, "OpenSea: contract indexed but no collection slug returned"
    return slug, "OpenSea: slug resolved"


def _opensea_collection_socials(slug: str) -> tuple[dict, str]:
    try:
        r = requests.get(
            f"https://api.opensea.io/api/v2/collections/{slug}",
            headers=_opensea_headers(),
            timeout=10,
        )
    except Exception as exc:
        return {}, f"OpenSea: collection lookup failed ({exc.__class__.__name__})"
    if r.status_code != 200:
        return {}, f"OpenSea: collection lookup failed (HTTP {r.status_code})"
    found = extract_socials_generic(r.json())
    if found:
        return found, "OpenSea: found socials"
    return {}, "OpenSea: collection found, but no social links listed"


def _find_numeric_field(data, keys: set) -> float | None:
    """Same idea as extract_socials_generic but hunts for a numeric field
    (e.g. floor_price) anywhere in the JSON, tolerant of schema changes."""
    result = {"value": None}

    def visit(node):
        if result["value"] is not None:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() in keys and isinstance(v, (int, float)) and not isinstance(v, bool):
                    result["value"] = v
                    return
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(data)
    return result["value"]


def _opensea_floor_price(slug: str) -> tuple[float | None, str]:
    try:
        r = requests.get(
            f"https://api.opensea.io/api/v2/collections/{slug}/stats",
            headers=_opensea_headers(),
            timeout=10,
        )
    except Exception as exc:
        return None, f"OpenSea: floor lookup failed ({exc.__class__.__name__})"
    if r.status_code != 200:
        return None, f"OpenSea: floor lookup failed (HTTP {r.status_code})"
    floor = _find_numeric_field(r.json(), {"floor_price"})
    if floor is None:
        return None, "OpenSea: no floor price listed"
    return floor, "OpenSea: floor found"


def get_opensea_bundle(address: str) -> dict:
    """Socials + floor price for a contract, cached with a TTL so a burst
    of events for the same collection doesn't hammer OpenSea's API.
    contractURI is always checked live first (free, no rate limit worry);
    OpenSea is only queried if contractURI didn't give us everything."""
    address_l = address.lower()
    cache = load_opensea_cache()
    entry = cache.get(address_l)
    now = time.time()
    if entry and (now - entry.get("checked_at", 0)) < OPENSEA_CACHE_TTL:
        return entry

    socials, note1 = get_contract_uri_socials(address)
    notes = [note1]
    slug = None
    floor_price = None

    if not OPENSEA_API_KEY:
        notes.append("OpenSea: skipped (no OPENSEA_API_KEY configured)")
    else:
        slug, slug_note = _opensea_resolve_slug(address)
        notes.append(slug_note)
        if slug:
            if len(socials) < 3:
                os_socials, os_note = _opensea_collection_socials(slug)
                notes.append(os_note)
                for k, v in os_socials.items():
                    socials.setdefault(k, v)
            floor_price, floor_note = _opensea_floor_price(slug)
            notes.append(floor_note)

    result = {
        "checked_at": now,
        "slug": slug,
        "socials": socials,
        "socials_notes": notes,
        "floor_price": floor_price,
    }
    cache[address_l] = result
    save_opensea_cache(cache)
    return result


_SUPPLY_CACHE: dict[str, tuple[float, str]] = {}


def get_total_supply(address: str) -> str:
    """totalSupply() isn't part of every ERC-721/1155 (only Enumerable
    extensions guarantee it), so this degrades to 'N/A' when unavailable.
    Cached briefly in memory since it's called on every event for the
    same contract."""
    key = address.lower()
    now = time.time()
    cached = _SUPPLY_CACHE.get(key)
    if cached and now - cached[0] < SUPPLY_CACHE_TTL:
        return cached[1]

    display = "N/A"
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(address), abi=SUPPLY_ABI)
        supply = c.functions.totalSupply().call()
        display = f"{supply:,}"
    except Exception:
        pass

    _SUPPLY_CACHE[key] = (now, display)
    return display


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
ACTION_STYLE = {
    # kind -> (emoji, headline word, label for the address line)
    "mint": ("🟢", "MINT", "Buyer"),
    "burn": ("🔴", "BURN", "Burner"),
    "transfer": ("🔁", "TRANSFER", "To"),
}


async def broadcast_event(app: Application, ev: dict, entry, contracts: dict) -> None:
    await mark_candidate_minted_if_needed(app, ev)

    subs = load_subscribers()
    if not subs:
        return

    if ev["from"].lower() == ZERO_ADDR:
        kind = "mint"
    elif ev["to"].lower() == ZERO_ADDR:
        kind = "burn"
    else:
        kind = "transfer"
    emoji, action_word, actor_label = ACTION_STYLE[kind]

    label = get_collection_label(ev["contract"], contracts)
    amount_txt = f" x{ev['amount']}" if ev["amount"] != 1 else ""

    bundle = await asyncio.to_thread(get_opensea_bundle, ev["contract"])
    floor_price = bundle.get("floor_price")
    floor_txt = (
        f"{floor_price:g} {NATIVE_CURRENCY_SYMBOL}"
        if isinstance(floor_price, (int, float))
        else "N/A"
    )
    supply_txt = await asyncio.to_thread(get_total_supply, ev["contract"])

    tx_hash = entry["transactionHash"]
    tx_hash = tx_hash.hex() if hasattr(tx_hash, "hex") else tx_hash
    tx_link = f"{EXPLORER_TX_URL}{tx_hash}"

    actor_addr = ev["from"] if kind == "burn" else ev["to"]

    lines = [
        f"{emoji} *{label}*",
        f"{action_word} #{ev['token_id']}{amount_txt}  ({ev['standard']})",
        "",
        f"Floor: {floor_txt}  |  Chain: Robinhood",
        f"Supply: {supply_txt}",
        f"Contract: `{ev['contract']}`",
    ]

    if kind == "mint":
        mint_no = bump_mint_count(ev["contract"], actor_addr)
        times_txt = "mint" if mint_no == 1 else "mints"
        lines.append(f"{actor_label}: `{actor_addr}`  ({mint_no} {times_txt})")
    else:
        lines.append(f"{actor_label}: `{actor_addr}`")

    socials = bundle.get("socials") or {}
    social_links = []
    if socials.get("twitter"):
        social_links.append(f"[Twitter]({socials['twitter']})")
    if socials.get("telegram"):
        social_links.append(f"[Telegram]({socials['telegram']})")
    if socials.get("website"):
        social_links.append(f"[Website]({socials['website']})")

    lines.append("")
    lines.append(" | ".join(social_links) if social_links else "Socials: none found")
    lines.append(f"[View transaction]({tx_link})")

    text = "\n".join(lines)

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
# New-contract detection (deployed, possibly not minting yet)
# --------------------------------------------------------------------------
async def mark_candidate_minted_if_needed(app: Application, ev: dict) -> None:
    """If this mint belongs to a contract we auto-discovered pre-mint,
    flip it to 'minted' and let /subscribe_new chats know it's live."""
    if ev["from"].lower() != ZERO_ADDR:
        return
    key = ev["contract"].lower()
    candidates = load_candidates()
    if key not in candidates or candidates[key].get("minted"):
        return

    candidates[key]["minted"] = True
    save_candidates(candidates)

    subs = load_new_subscribers()
    if not subs:
        return
    label = candidates[key]["label"]
    text = (
        f"🚀 *{label}* just minted its first token — no longer pending.\n"
        f"[View contract]({EXPLORER_ADDR_URL}{ev['contract']})"
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
            log.exception("Failed to notify %s of first mint", chat_id)


async def broadcast_new_contract(app: Application, address: str, info: dict) -> None:
    subs = load_new_subscribers()
    if not subs:
        return

    lines = [
        f"🆕 *New NFT contract deployed* — {info['label']}",
        f"Standard: {info['standard']}",
        f"Contract: `{address}`",
        f"[View on explorer]({EXPLORER_ADDR_URL}{address})",
        "Status: no mints yet",
    ]
    socials = info.get("socials") or {}
    social_links = []
    if socials.get("twitter"):
        social_links.append(f"[Twitter]({socials['twitter']})")
    if socials.get("telegram"):
        social_links.append(f"[Telegram]({socials['telegram']})")
    if socials.get("website"):
        social_links.append(f"[Website]({socials['website']})")
    if social_links:
        lines.append(" | ".join(social_links))
    else:
        notes = info.get("socials_notes") or []
        reason = " / ".join(notes) if notes else "not found"
        lines.append(f"Socials: none found ({reason})")

    text = "\n".join(lines)
    for chat_id in subs:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Failed to notify %s of new contract", chat_id)


async def handle_possible_new_contract(app: Application, tx) -> None:
    tx_hash = tx["hash"]
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        return
    address = receipt.get("contractAddress")
    if not address:
        return  # not a contract-creating tx after all

    candidates = load_candidates()
    if address.lower() in candidates:
        return

    standard = await asyncio.to_thread(detect_token_standard, address)
    if not standard:
        return  # not an NFT contract (or doesn't implement ERC-165)

    label = await asyncio.to_thread(get_collection_label, address, {})
    bundle = await asyncio.to_thread(get_opensea_bundle, address)

    tx_hash_str = tx_hash.hex() if hasattr(tx_hash, "hex") else tx_hash
    info = {
        "standard": standard,
        "label": label,
        "deployer": tx.get("from"),
        "tx_hash": tx_hash_str,
        "block": receipt.get("blockNumber"),
        "minted": False,
        "socials": bundle.get("socials", {}),
        "socials_notes": bundle.get("socials_notes", []),
    }
    candidates[address.lower()] = info
    save_candidates(candidates)

    log.info("New %s contract detected: %s (%s)", standard, label, address)
    await broadcast_new_contract(app, address, info)


async def new_contract_loop(app: Application) -> None:
    state = load_state()
    if state.get("last_block_new_contracts") is None:
        try:
            state["last_block_new_contracts"] = w3.eth.block_number
            save_state(state)
        except Exception:
            log.exception("Could not reach RPC at startup, will retry")
            state["last_block_new_contracts"] = 0

    while True:
        try:
            latest = w3.eth.block_number
            from_block = state["last_block_new_contracts"] + 1
            to_block = min(from_block + NEW_CONTRACT_BLOCK_SPAN - 1, latest)

            if from_block <= to_block:
                for block_num in range(from_block, to_block + 1):
                    block = w3.eth.get_block(block_num, full_transactions=True)
                    for tx in block["transactions"]:
                        if tx.get("to") is None:
                            await handle_possible_new_contract(app, tx)

                state["last_block_new_contracts"] = to_block
                save_state(state)

        except Exception:
            log.exception("New-contract scan error, will retry after backoff")

        await asyncio.sleep(NEW_CONTRACT_POLL_INTERVAL)


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
    "*Mint/sale/transfer feed*\n"
    "/subscribe — get live NFT mint/sale/transfer alerts in this chat\n"
    "/unsubscribe — stop alerts here\n"
    "/watching — show which collections are being tracked\n"
    "/watch <address> [label] — (admin) track a specific collection\n"
    "/unwatch <address> — (admin) stop tracking a collection\n\n"
    "*New-contract feed*\n"
    "/subscribe_new — get alerted the moment a new NFT contract is deployed "
    "(plus a follow-up when it mints for the first time)\n"
    "/unsubscribe_new — stop new-contract alerts here\n"
    "/pending — list deployed contracts that haven't minted yet\n\n"
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


async def cmd_subscribe_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subs = load_new_subscribers()
    subs.add(update.effective_chat.id)
    save_new_subscribers(subs)
    await update.message.reply_text(
        "✅ Subscribed to new-contract alerts — deploys + first-mint updates."
    )


async def cmd_unsubscribe_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    subs = load_new_subscribers()
    subs.discard(update.effective_chat.id)
    save_new_subscribers(subs)
    await update.message.reply_text("🛑 Unsubscribed from new-contract alerts.")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    candidates = load_candidates()
    pending = {a: c for a, c in candidates.items() if not c.get("minted")}
    if not pending:
        await update.message.reply_text(
            "No pending (deployed-but-not-minting) contracts tracked right now.\n"
            "Note: this only covers contracts deployed since the bot started running."
        )
        return
    lines = []
    for addr, info in list(pending.items())[:25]:
        lines.append(f"• {info['label']} ({info['standard']}) — `{addr}`")
    await update.message.reply_text(
        "Pending (deployed, not minting yet):\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )


# --------------------------------------------------------------------------
# Error handling — nothing should fail silently
# --------------------------------------------------------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "⚠️ Something went wrong handling that command — check the bot's "
                "logs for details.",
            )
        except Exception:
            log.exception("Also failed to notify the chat about the error")


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Catches typos / unregistered commands (e.g. a missing underscore) so
    # they never fail completely silently.
    await update.message.reply_text(
        "Unrecognized command — try /help to see everything I support."
    )


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
async def post_init(app: Application) -> None:
    asyncio.create_task(poll_loop(app))
    asyncio.create_task(new_contract_loop(app))


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
    app.add_handler(CommandHandler("subscribe_new", cmd_subscribe_new))
    app.add_handler(CommandHandler("unsubscribe_new", cmd_unsubscribe_new))
    app.add_handler(CommandHandler("pending", cmd_pending))
    # Catch-all for unmatched commands — must be added last. filters.COMMAND
    # matches any "/xyz" text that no CommandHandler above already claimed.
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_error_handler(on_error)

    log.info("Bot starting — polling %s every %ss", RPC_URL, POLL_INTERVAL)
    app.run_polling()


if __name__ == "__main__":
    main()
