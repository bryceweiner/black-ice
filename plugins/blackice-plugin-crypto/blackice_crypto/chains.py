"""The chain registry: which networks can be watched, and how to reach each one.

No single explorer covers the top 100 chains, so each entry names the adapter
that can answer for it:

* ``etherscan_v2`` -- one API key, ~60 EVM chains selected by ``chain_id``.
* ``blockscout``   -- keyless, and the *only* free way to enumerate an address's
  whole token list in one request. Preferred over Etherscan where an instance
  is known; Etherscan's ``addresstokenbalance`` is a paid endpoint, so on the
  free tier tokens otherwise have to be derived from transfer history.
* ``evm_rpc``      -- last resort for an EVM chain with neither: native only.
* a native adapter -- ``blockchair`` (eight UTXO chains), ``cosmos`` (the whole
  LCD family), ``solana``, ``tron``, and the rest, one per family.

Two fields are best-effort and self-correcting: ``cg_platform`` and ``cg_native``
are reconciled at runtime against CoinGecko's ``/asset_platforms``, which keys
its platforms by EVM chain id -- see `prices.reconcile`. A wrong guess here
costs a price, never a balance.

Monero is deliberately absent: an address alone cannot reveal its balance
without the view key, so it cannot be monitored the way every other chain here
can be.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

ETHERSCAN_V2 = "etherscan_v2"
BLOCKSCOUT = "blockscout"
EVM_RPC = "evm_rpc"


@dataclass(frozen=True)
class Chain:
    slug: str                       # what the user types: "base", "bitcoin"
    name: str                       # display name
    via: str                        # adapter key
    symbol: str                     # native asset ticker
    decimals: int = 18
    chain_id: int | None = None     # EVM chain id, for Etherscan V2
    api: str | None = None          # Blockscout / native API base URL
    rpc: str | None = None          # JSON-RPC endpoint, for evm_rpc
    cg_platform: str | None = None  # CoinGecko asset platform id (token prices)
    cg_native: str | None = None    # CoinGecko coin id (native price)
    layer: int = 1
    key_env: str | None = None      # extra credential this chain needs, if any

    @property
    def is_evm(self) -> bool:
        return self.chain_id is not None

    @property
    def tokens_supported(self) -> bool:
        """Whether this chain's adapter can enumerate non-native holdings."""
        return self.via in {ETHERSCAN_V2, BLOCKSCOUT} or self.via in {
            "solana", "tron", "cosmos", "aptos", "sui", "near", "stellar", "ton",
            "algorand", "tezos", "multiversx", "starknet",
        }


def _evm(slug, name, chain_id, symbol="ETH", *, layer=1, api=None, cg_platform=None,
         cg_native="ethereum", decimals=18, rpc=None) -> Chain:
    """An EVM chain reachable through Etherscan V2, optionally with a Blockscout
    instance for the token list."""
    return Chain(
        slug=slug, name=name, via=ETHERSCAN_V2, symbol=symbol, decimals=decimals,
        chain_id=chain_id, api=api, rpc=rpc, cg_platform=cg_platform,
        cg_native=cg_native, layer=layer,
    )


# --- EVM: layer 1 ----------------------------------------------------------

_EVM_L1 = [
    _evm("ethereum", "Ethereum", 1, api="https://eth.blockscout.com",
         cg_platform="ethereum"),
    _evm("bsc", "BNB Smart Chain", 56, "BNB", cg_platform="binance-smart-chain",
         cg_native="binancecoin"),
    _evm("polygon", "Polygon PoS", 137, "POL", cg_platform="polygon-pos",
         cg_native="matic-network"),
    _evm("avalanche", "Avalanche C-Chain", 43114, "AVAX", cg_platform="avalanche",
         cg_native="avalanche-2"),
    _evm("gnosis", "Gnosis", 100, "XDAI", api="https://gnosis.blockscout.com",
         cg_platform="xdai", cg_native="xdai"),
    _evm("cronos", "Cronos", 25, "CRO", cg_platform="cronos",
         cg_native="crypto-com-chain"),
    _evm("fantom", "Fantom", 250, "FTM", cg_platform="fantom", cg_native="fantom"),
    _evm("sonic", "Sonic", 146, "S", cg_platform="sonic", cg_native="sonic-3"),
    _evm("celo", "Celo", 42220, "CELO", api="https://explorer.celo.org",
         cg_platform="celo", cg_native="celo"),
    _evm("kaia", "Kaia (Klaytn)", 8217, "KAIA", cg_platform="klay-token",
         cg_native="klay-token"),
    _evm("core", "Core", 1116, "CORE", cg_platform="core", cg_native="coredaoorg"),
    _evm("xdc", "XDC Network", 50, "XDC", cg_platform="xdc-network",
         cg_native="xdce-crowd-sale"),
    _evm("flare", "Flare", 14, "FLR", api="https://flare-explorer.flare.network",
         cg_platform="flare-network", cg_native="flare-networks"),
    _evm("songbird", "Songbird", 19, "SGB", cg_native="songbird"),
    _evm("rootstock", "Rootstock", 30, "RBTC", api="https://rootstock.blockscout.com",
         cg_platform="rootstock", cg_native="rootstock-rbtc"),
    _evm("ethereum-classic", "Ethereum Classic", 61, "ETC",
         cg_native="ethereum-classic"),
    _evm("moonbeam", "Moonbeam", 1284, "GLMR", cg_platform="moonbeam",
         cg_native="moonbeam"),
    _evm("moonriver", "Moonriver", 1285, "MOVR", cg_platform="moonriver",
         cg_native="moonriver"),
    _evm("astar", "Astar", 592, "ASTR", cg_platform="astar", cg_native="astar"),
    _evm("kava", "Kava EVM", 2222, "KAVA", cg_platform="kava", cg_native="kava"),
    _evm("evmos", "Evmos", 9001, "EVMOS", cg_platform="evmos", cg_native="evmos"),
    _evm("canto", "Canto", 7700, "CANTO", cg_platform="canto", cg_native="canto"),
    _evm("telos", "Telos EVM", 40, "TLOS", cg_platform="telos", cg_native="telos"),
    _evm("fuse", "Fuse", 122, "FUSE", api="https://explorer.fuse.io",
         cg_platform="fuse", cg_native="fuse-network-token"),
    _evm("aurora", "Aurora", 1313161554, cg_platform="aurora"),
    _evm("harmony", "Harmony", 1666600000, "ONE", cg_platform="harmony-shard-0",
         cg_native="harmony"),
    _evm("oasis-emerald", "Oasis Emerald", 42262, "ROSE",
         cg_platform="oasis-emerald", cg_native="oasis-network"),
    _evm("iotex", "IoTeX", 4689, "IOTX", cg_platform="iotex", cg_native="iotex"),
    _evm("conflux", "Conflux eSpace", 1030, "CFX", cg_platform="conflux",
         cg_native="conflux-token"),
    _evm("zetachain", "ZetaChain", 7000, "ZETA", cg_platform="zetachain",
         cg_native="zetachain"),
    _evm("shibarium", "Shibarium", 109, "BONE", cg_native="bone-shibaswap"),
    _evm("pulsechain", "PulseChain", 369, "PLS", cg_platform="pulsechain",
         cg_native="pulsechain"),
    _evm("xlayer", "X Layer", 196, "OKB", cg_platform="x-layer", cg_native="okb"),
    _evm("berachain", "Berachain", 80094, "BERA", cg_platform="berachain",
         cg_native="berachain-bera"),
    _evm("hyperevm", "HyperEVM", 999, "HYPE", cg_platform="hyperevm",
         cg_native="hyperliquid"),
    _evm("sei", "Sei EVM", 1329, "SEI", cg_platform="sei-v2", cg_native="sei-network"),
    _evm("ronin", "Ronin", 2020, "RON", cg_platform="ronin", cg_native="ronin"),
    _evm("story", "Story", 1514, "IP", cg_platform="story", cg_native="story-2"),
    _evm("plume", "Plume", 98866, "PLUME", cg_platform="plume-network",
         cg_native="plume"),
    _evm("vana", "Vana", 1480, "VANA", cg_native="vana"),
    _evm("bitlayer", "Bitlayer", 200901, "BTC", cg_native="bitcoin"),
    _evm("merlin", "Merlin", 4200, "BTC", cg_platform="merlin-chain",
         cg_native="bitcoin"),
    _evm("bouncebit", "BounceBit", 6001, "BB", cg_native="bouncebit"),
]

# --- EVM: layer 2 ----------------------------------------------------------

_EVM_L2 = [
    _evm("arbitrum", "Arbitrum One", 42161, layer=2, cg_platform="arbitrum-one"),
    _evm("arbitrum-nova", "Arbitrum Nova", 42170, layer=2, cg_platform="arbitrum-nova"),
    _evm("optimism", "OP Mainnet", 10, layer=2, api="https://optimism.blockscout.com",
         cg_platform="optimistic-ethereum"),
    _evm("base", "Base", 8453, layer=2, api="https://base.blockscout.com",
         cg_platform="base"),
    _evm("zksync", "zkSync Era", 324, layer=2, api="https://zksync.blockscout.com",
         cg_platform="zksync"),
    _evm("linea", "Linea", 59144, layer=2, cg_platform="linea"),
    _evm("scroll", "Scroll", 534352, layer=2, cg_platform="scroll"),
    _evm("polygon-zkevm", "Polygon zkEVM", 1101, layer=2,
         cg_platform="polygon-zkevm"),
    _evm("mantle", "Mantle", 5000, "MNT", layer=2, cg_platform="mantle",
         cg_native="mantle"),
    _evm("blast", "Blast", 81457, layer=2, api="https://blast.blockscout.com",
         cg_platform="blast"),
    _evm("mode", "Mode", 34443, layer=2, api="https://explorer.mode.network",
         cg_platform="mode"),
    _evm("fraxtal", "Fraxtal", 252, "frxETH", layer=2, cg_platform="fraxtal",
         cg_native="frax-ether"),
    _evm("zora", "Zora", 7777777, layer=2, api="https://explorer.zora.energy",
         cg_platform="zora"),
    _evm("world-chain", "World Chain", 480, layer=2, cg_platform="world-chain"),
    _evm("unichain", "Unichain", 130, layer=2, cg_platform="unichain"),
    _evm("ink", "Ink", 57073, layer=2, cg_platform="ink"),
    _evm("soneium", "Soneium", 1868, layer=2, cg_platform="soneium"),
    _evm("abstract", "Abstract", 2741, layer=2, cg_platform="abstract"),
    _evm("taiko", "Taiko", 167000, layer=2, cg_platform="taiko"),
    _evm("metis", "Metis", 1088, "METIS", layer=2, cg_platform="metis-andromeda",
         cg_native="metis-token"),
    _evm("manta", "Manta Pacific", 169, layer=2, cg_platform="manta-pacific"),
    _evm("opbnb", "opBNB", 204, "BNB", layer=2, cg_platform="opbnb",
         cg_native="binancecoin"),
    _evm("boba", "Boba Network", 288, layer=2, cg_platform="boba"),
    _evm("kroma", "Kroma", 255, layer=2, cg_platform="kroma"),
    _evm("xai", "Xai", 660279, "XAI", layer=2, cg_platform="xai",
         cg_native="xai-blockchain"),
    _evm("apechain", "ApeChain", 33139, "APE", layer=2, cg_platform="apechain",
         cg_native="apecoin"),
    _evm("degen", "Degen Chain", 666666666, "DEGEN", layer=2,
         cg_platform="degen", cg_native="degen-base"),
    _evm("lisk", "Lisk", 1135, layer=2, cg_platform="lisk"),
    _evm("cyber", "Cyber", 7560, layer=2, cg_platform="cyber"),
    _evm("gravity", "Gravity Alpha", 1625, "G", layer=2, cg_platform="gravity-alpha",
         cg_native="g-token"),
    _evm("zircuit", "Zircuit", 48900, layer=2, cg_platform="zircuit"),
    _evm("morph", "Morph", 2818, layer=2, cg_platform="morph-l2"),
    _evm("mint", "Mint", 185, layer=2, cg_platform="mint"),
    _evm("superseed", "Superseed", 5330, layer=2, cg_platform="superseed"),
    _evm("swellchain", "Swellchain", 1923, layer=2, cg_platform="swellchain"),
    _evm("redstone", "Redstone", 690, layer=2, cg_platform="redstone"),
    _evm("sanko", "Sanko", 1996, "DMT", layer=2, cg_native="dream-machine-token"),
    _evm("b3", "B3", 8333, layer=2, cg_platform="b3"),
    _evm("ancient8", "Ancient8", 888888888, layer=2, cg_platform="ancient8"),
    _evm("immutable-zkevm", "Immutable zkEVM", 13371, "IMX", layer=2,
         cg_platform="immutable", cg_native="immutable-x"),
]

# --- non-EVM ---------------------------------------------------------------

def _blockchair(slug, name, symbol, decimals, cg_native, path) -> Chain:
    """One adapter, eight UTXO chains. `path` is Blockchair's own chain slug."""
    return Chain(slug=slug, name=name, via="blockchair", symbol=symbol,
                 decimals=decimals, api=path, cg_native=cg_native)


def _cosmos(slug, name, symbol, denom, cg_native, api, decimals=6) -> Chain:
    """Cosmos SDK LCD. `rpc` carries the base denom, e.g. "uatom"."""
    return Chain(slug=slug, name=name, via="cosmos", symbol=symbol,
                 decimals=decimals, api=api, rpc=denom, cg_native=cg_native)


_NON_EVM = [
    # UTXO family -- all through Blockchair.
    _blockchair("bitcoin", "Bitcoin", "BTC", 8, "bitcoin", "bitcoin"),
    _blockchair("litecoin", "Litecoin", "LTC", 8, "litecoin", "litecoin"),
    _blockchair("dogecoin", "Dogecoin", "DOGE", 8, "dogecoin", "dogecoin"),
    _blockchair("bitcoin-cash", "Bitcoin Cash", "BCH", 8, "bitcoin-cash",
                "bitcoin-cash"),
    _blockchair("dash", "Dash", "DASH", 8, "dash", "dash"),
    _blockchair("zcash", "Zcash", "ZEC", 8, "zcash", "zcash"),
    _blockchair("ecash", "eCash", "XEC", 2, "ecash", "ecash"),
    _blockchair("bitcoin-sv", "Bitcoin SV", "BSV", 8, "bitcoin-cash-sv",
                "bitcoin-sv"),

    # Cosmos SDK family -- all through the LCD adapter.
    _cosmos("cosmos", "Cosmos Hub", "ATOM", "uatom", "cosmos",
            "https://rest.cosmos.directory/cosmoshub"),
    _cosmos("osmosis", "Osmosis", "OSMO", "uosmo", "osmosis",
            "https://rest.cosmos.directory/osmosis"),
    _cosmos("celestia", "Celestia", "TIA", "utia", "celestia",
            "https://rest.cosmos.directory/celestia"),
    _cosmos("dydx", "dYdX Chain", "DYDX", "adydx", "dydx-chain",
            "https://rest.cosmos.directory/dydx", decimals=18),
    _cosmos("injective", "Injective", "INJ", "inj", "injective-protocol",
            "https://rest.cosmos.directory/injective", decimals=18),
    _cosmos("akash", "Akash", "AKT", "uakt", "akash-network",
            "https://rest.cosmos.directory/akash"),
    _cosmos("juno", "Juno", "JUNO", "ujuno", "juno-network",
            "https://rest.cosmos.directory/juno"),
    _cosmos("neutron", "Neutron", "NTRN", "untrn", "neutron-3",
            "https://rest.cosmos.directory/neutron"),
    _cosmos("stargaze", "Stargaze", "STARS", "ustars", "stargaze",
            "https://rest.cosmos.directory/stargaze"),
    _cosmos("secret", "Secret Network", "SCRT", "uscrt", "secret",
            "https://rest.cosmos.directory/secretnetwork"),

    # One family, one adapter each.
    Chain("solana", "Solana", "solana", "SOL", 9,
          api="https://api.mainnet-beta.solana.com", cg_platform="solana",
          cg_native="solana"),
    Chain("tron", "TRON", "tron", "TRX", 6, api="https://api.trongrid.io",
          cg_platform="tron", cg_native="tron"),
    Chain("xrp", "XRP Ledger", "xrpl", "XRP", 6, api="https://s1.ripple.com:51234",
          cg_native="ripple"),
    Chain("ton", "TON", "ton", "TON", 9, api="https://toncenter.com",
          cg_platform="the-open-network", cg_native="the-open-network"),
    Chain("near", "NEAR", "near", "NEAR", 24, api="https://rpc.mainnet.near.org",
          cg_platform="near-protocol", cg_native="near"),
    Chain("aptos", "Aptos", "aptos", "APT", 8, api="https://api.mainnet.aptoslabs.com",
          cg_platform="aptos", cg_native="aptos"),
    Chain("sui", "Sui", "sui", "SUI", 9, api="https://fullnode.mainnet.sui.io",
          cg_platform="sui", cg_native="sui"),
    Chain("stellar", "Stellar", "stellar", "XLM", 7,
          api="https://horizon.stellar.org", cg_platform="stellar",
          cg_native="stellar"),
    Chain("algorand", "Algorand", "algorand", "ALGO", 6,
          api="https://mainnet-idx.algonode.cloud", cg_platform="algorand",
          cg_native="algorand"),
    Chain("tezos", "Tezos", "tezos", "XTZ", 6, api="https://api.tzkt.io",
          cg_platform="tezos", cg_native="tezos"),
    Chain("hedera", "Hedera", "hedera", "HBAR", 8,
          api="https://mainnet-public.mirrornode.hedera.com",
          cg_platform="hedera-hashgraph", cg_native="hedera-hashgraph"),
    Chain("stacks", "Stacks", "stacks", "STX", 6, api="https://api.hiro.so",
          cg_platform="stacks", cg_native="blockstack"),
    Chain("multiversx", "MultiversX", "multiversx", "EGLD", 18,
          api="https://api.multiversx.com", cg_platform="elrond",
          cg_native="elrond-erd-2"),
    Chain("kaspa", "Kaspa", "kaspa", "KAS", 8, api="https://api.kaspa.org",
          cg_native="kaspa"),
    Chain("arweave", "Arweave", "arweave", "AR", 12, api="https://arweave.net",
          cg_native="arweave"),
    Chain("filecoin", "Filecoin", "filecoin", "FIL", 18,
          api="https://filfox.info/api/v1", cg_native="filecoin"),
    Chain("starknet", "Starknet", "starknet", "ETH", 18, layer=2,
          api="https://starknet-mainnet.public.blastapi.io",
          cg_platform="starknet", cg_native="ethereum"),
    Chain("vechain", "VeChain", "vechain", "VET", 18,
          api="https://mainnet.vechain.org", cg_native="vechain"),

    # These need a credential of their own; without it they report as such
    # rather than silently reading zero.
    Chain("cardano", "Cardano", "cardano", "ADA", 6,
          api="https://cardano-mainnet.blockfrost.io/api/v0",
          cg_platform="cardano", cg_native="cardano", key_env="BLOCKFROST_PROJECT_ID"),
    Chain("polkadot", "Polkadot", "subscan", "DOT", 10,
          api="https://polkadot.api.subscan.io", cg_native="polkadot",
          key_env="SUBSCAN_API_KEY"),
    Chain("kusama", "Kusama", "subscan", "KSM", 12,
          api="https://kusama.api.subscan.io", cg_native="kusama",
          key_env="SUBSCAN_API_KEY"),
]


CHAINS: dict[str, Chain] = {c.slug: c for c in (*_EVM_L1, *_EVM_L2, *_NON_EVM)}


def get(slug: str) -> Chain | None:
    """Look up a chain, tolerating the spellings a user or model will produce."""
    if not slug:
        return None
    key = slug.strip().lower().replace(" ", "-").replace("_", "-")
    if key in CHAINS:
        return CHAINS[key]
    return _ALIASES.get(key)


_ALIAS_NAMES = {
    "eth": "ethereum", "mainnet": "ethereum", "erc20": "ethereum",
    "btc": "bitcoin", "xbt": "bitcoin", "ltc": "litecoin", "doge": "dogecoin",
    "bch": "bitcoin-cash", "sol": "solana", "trx": "tron", "trc20": "tron",
    "ripple": "xrp", "xlm": "stellar", "atom": "cosmos", "cosmoshub": "cosmos",
    "cosmos-hub": "cosmos", "matic": "polygon", "polygon-pos": "polygon",
    "bnb": "bsc", "binance": "bsc", "binance-smart-chain": "bsc", "bep20": "bsc",
    "avax": "avalanche", "arb": "arbitrum", "arbitrum-one": "arbitrum",
    "op": "optimism", "op-mainnet": "optimism", "optimistic-ethereum": "optimism",
    "zksync-era": "zksync", "klaytn": "kaia", "elrond": "multiversx",
    "the-open-network": "ton", "near-protocol": "near", "hbar": "hedera",
    "etc": "ethereum-classic", "rsk": "rootstock", "xtz": "tezos",
    "ada": "cardano", "dot": "polkadot", "ksm": "kusama", "fil": "filecoin",
    "hyperliquid": "hyperevm", "okx": "xlayer", "x-layer": "xlayer",
}
_ALIASES: dict[str, Chain] = {
    alias: CHAINS[target] for alias, target in _ALIAS_NAMES.items() if target in CHAINS
}


def by_chain_id(chain_id: int) -> Chain | None:
    return next((c for c in CHAINS.values() if c.chain_id == chain_id), None)


def apply_price_ids(slug: str, cg_platform: str | None, cg_native: str | None) -> None:
    """Correct a chain's CoinGecko ids from live data. See `prices.reconcile`."""
    chain = CHAINS.get(slug)
    if chain is None:
        return
    CHAINS[slug] = replace(
        chain,
        cg_platform=cg_platform or chain.cg_platform,
        cg_native=cg_native or chain.cg_native,
    )


def listing() -> list[dict]:
    """The registry as plain data, for the dashboard and the assistant."""
    return [
        {
            "network": c.slug,
            "name": c.name,
            "symbol": c.symbol,
            "layer": c.layer,
            "kind": "evm" if c.is_evm else c.via,
            "tokens": c.tokens_supported,
            "needs_key": c.key_env,
        }
        for c in sorted(CHAINS.values(), key=lambda c: (c.layer, c.name.lower()))
    ]
