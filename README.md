# jlcparts-mcp

An MCP server for choosing parts for [JLCPCB assembly](https://jlcpcb.com/parts). It answers parametric questions
("a basic 10 kΩ 1 % 0402", "a 3.3 V LDO in SOT-23-5 with at least 500 mA") from a local index in a few
milliseconds, and confirms stock and price for the parts you are about to order with JLCPCB directly.

Catalogue data comes from the [yaqwsx/jlcparts](https://github.com/yaqwsx/jlcparts) mirror.

## Two data sources, and why it matters

| | Local mirror index | JLCPCB live |
|---|---|---|
| Used by | `search_parts`, `describe_category`, `list_categories` | `get_part`, `check_bom`, `find_alternatives` (default), `search_parts(live=true)` |
| Speed | milliseconds, offline | about half a second per part |
| Coverage | parts the mirror has seen in stock (~550,000) | everything JLCPCB lists |
| Freshness | per part: the mirror re-fetches each part only every few weeks | now |

The mirror is republished several times a day, but it crawls JLCPCB slowly. In a snapshot from November 2026 only
4 % of in-stock rows had been checked within two weeks and 63 % were more than a month old. Some parts JLCPCB stocks
are missing from it altogether. So:

- every result carries `stock_checked`: either `live` or `mirror <date>`;
- "not in the mirror" never means "out of stock" or "does not exist": `get_part` asks JLCPCB before saying so;
- run `check_bom` before ordering.

The live lookups use the search endpoint behind jlcpcb.com/parts. It is unofficial and unauthenticated; the
server asks about one part per request, at most four at a time and 60 per tool call. Set `JLCPARTS_MCP_LIVE=0` to
turn live lookups off.

## Tools

- `search_parts`: full-text and parametric search. Filters are unit-aware (`Resistance = 10k` or `4k7`,
  `Voltage Rating >= 16V`, `Power >= 1/8W`, `Output Voltage = 3.3V` matches an adjustable `1.2V~5V` part; each
  result's notes say how the value was read) and combine with package,
  manufacturer, library tier, quantity (stock and price tier) and a price ceiling. Ranks basic, then preferred,
  then extended parts, since only extended parts carry JLCPCB's per-part setup fee.
- `describe_category`, `list_categories`: the exact attribute names, value ranges and common packages of a
  category. Attribute names differ between categories, so look before filtering.
- `get_part`: everything about one LCSC number or exact manufacturer part number: price tiers, datasheet link,
  attributes, minimum order.
- `check_bom`: stock, unit price at quantity, library tier and totals for `[{lcsc, quantity}]`. Duplicate lines are
  merged so the stock check and price tier use the total quantity.
- `find_alternatives`: same-category, same-package candidates for a part, with the attributes that differ.
- `database_status`, `update_database`: age of the local index, background refresh.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11+.

    git clone https://github.com/ititti-es/jlcparts-mcp && cd jlcparts-mcp
    uv sync
    uv run jlcparts-mcp build        # about 30 s: downloads ~250 MB, builds a ~500 MB index

Register it with an MCP client, for example Claude Code:

    claude mcp add --scope user jlcparts -- uv run --directory /path/to/jlcparts-mcp jlcparts-mcp

`uv run jlcparts-mcp status` prints the state of the index. `uv run jlcparts-mcp build` refreshes it when the
mirror has a newer snapshot (`--force` rebuilds regardless); the `update_database` tool does the same in the
background.

| Environment variable | Effect |
|---|---|
| `JLCPARTS_MCP_DATA` | directory for the index (default `~/.cache/jlcparts-mcp`) |
| `JLCPARTS_MCP_AUTO_UPDATE_HOURS` | refresh the index in the background when it is older than this (off by default) |
| `JLCPARTS_MCP_LIVE` | `0` disables live lookups |
| `JLCPARTS_DATA_URL` | alternative location of the mirror's `cache.zip` parts |

## Limits

- Search only sees parts the mirror recorded with stock above zero, and ranks by that possibly old stock figure.
- About 30 % of upstream rows have no category. The index takes the category from the end of the description
  where it can (most of them); about 20,000 parts remain uncategorised, so a `category` filter never returns them.
  Free-text `query` and `part_number` still find them.
- Attributes are JLCPCB's own data: sometimes wrong, often incomplete, and named differently per category. Parts of
  different kinds can share a package and a headline attribute (a crystal and an oscillator at the same frequency),
  so read the category of each result and check the datasheet before committing to a part.
- Prices are JLCPCB assembly-library prices in USD, without the extended-part setup fee, attrition or shipping.
- The package filter is a substring match (`SOT-23` also matches `SOT-23-5`).
- Built against the `mcp` Python SDK 1.x.

## Development

    uv run pytest

The tests build a small synthetic catalogue with the production index builder and never touch the network.
