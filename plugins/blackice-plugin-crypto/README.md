# blackice-plugin-crypto

Watches cryptocurrency addresses across the top 100 L1 and L2 chains, reports
deposits and withdrawals, and raises a high-severity alert when too much leaves
an address too quickly.

```bash
uv pip install -e plugins/blackice-plugin-crypto
```

## What it watches

One sensor, `crypto.balances`, covers every chain. A *watch* is a
`(network, address)` pair, added from the dashboard or by the assistant. Each
poll records the native coin and every token the address holds, valued in USD.

## The two rules

**`drain` — the security rule.** Holdings fell more than **X%** against the
balance **Y hours** ago. Both sides of that comparison are valued at the *same
current prices*, so the number reflects coins leaving the address and not the
market moving. A 90% crash with no outflow raises nothing; moving 60% of your
coins out raises a `HIGH` event. Where nothing on the chain can be priced at
all, the native quantity stands in — it is the one holding every chain reports.

**`value_drop` — the portfolio rule.** The USD total fell more than X% over Y
hours, whatever the cause, market included. It never exceeds `LOW`, and it is
**off by default**: switch it on with the `set_value_rule` command. A plugin
cannot read core's alarm arm state, so this rule gates itself on its own
setting rather than pretending `default_armed=False` will silence it.

Both rules stay quiet for the length of their own window after firing, so a
drained address alerts once rather than on every poll until the window slides.

Defaults: 20% / 24h for `drain`, 30% / 24h for `value_drop`, both settable
globally or per address. Movements under $1 are treated as dust and stay off
the timeline — on unpriced chains, so does any change under 1%.

## Configuration

Everything is optional; the plugin degrades rather than failing.

| Variable | Effect if unset |
|---|---|
| `ETHERSCAN_API_KEY` | EVM chains fall back to keyless Blockscout, where an instance is known |
| `COINGECKO_API_KEY` | Prices still work, on the anonymous rate limit |
| `BLOCKFROST_PROJECT_ID` | Cardano reports that it needs a key, rather than reading zero |
| `SUBSCAN_API_KEY` | As above, for Polkadot and Kusama |
| `BLACKICE_CRYPTO_POLL_SECONDS` | 300; the floor is 30 |

Polling cost scales with the number of addresses watched, not with the 100
chains in the registry.

## How chains are reached

`chains.py` names an adapter per chain:

- **`etherscan_v2`** — one key, ~60 EVM chains selected by `chain_id`.
- **`blockscout`** — keyless, and the only free way to list an address's whole
  token holdings in one request. Tried *before* Etherscan where an instance is
  known, because Etherscan's equivalent is a paid endpoint; on the free tier
  its token list has to be reconstructed from transfer history, one request per
  contract, capped at 20.
- **`blockchair`** — eight UTXO chains through one adapter.
- **`cosmos`** — the whole Cosmos SDK LCD family through one adapter.
- one adapter each for Solana, TRON, XRPL, TON, NEAR, Aptos, Sui, Stellar,
  Algorand, Tezos, Hedera, Stacks, MultiversX, Kaspa, Arweave, Filecoin,
  VeChain, Starknet, Cardano and the Substrate chains.

Monero is deliberately absent: an address alone cannot reveal its balance
without the view key.

The registry's CoinGecko ids are a best guess corrected at runtime —
`prices.reconcile` reads `/asset_platforms`, which keys platforms by EVM chain
id, and replaces the guesses with the real ids. A wrong guess there costs a
price, never a balance.

## Health

One flaky public endpoint among a hundred chains is news about that chain, not
a fault in the plugin. Unreachable chains, bad credentials and rejected
addresses are recorded against the individual watch, shown in the **Status**
column of the watchlist and in the polling badge, and the plugin stays healthy.

## What the assistant can do

`list_networks`, `list_addresses`, `check_balance`, `add_address`,
`set_thresholds`.

**Removal is deliberately not a tool.** It is the one action that blinds the
sensor, and this plugin ingests text — token names and symbols — chosen by
whoever deployed the token. Stopping a watch is a dashboard button, reached
through the same supervisor as everything else. Removal is a soft delete:
polling stops, history is kept, and re-adding the address resumes its series.
