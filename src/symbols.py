"""
# WHY: ------------------------------------------------------------------------
# Turning an exchange symbol into an asset you can look up elsewhere.
#
# This module exists because of one trap that silently breaks market-cap joins:
#
#   Binance lists 1000PEPEUSDT, 1000SHIBUSDT, 1000BONKUSDT. The contract prices
#   *one thousand* tokens. If you strip only "USDT" and look up "1000PEPE" on
#   CoinGecko, you get nothing -- and a missing market cap does not look like an
#   error, it looks like a coin with mcap = 0, which then sails through or fails
#   a threshold for entirely the wrong reason.
#
# So: strip the quote asset, strip a leading numeric multiplier, and keep BOTH
# the exchange symbol and the normalised base asset, plus the multiplier so
# notional maths stays correct.
#
# The second trap here is ticker collision, handled in coingecko.py rather than
# in this module: symbols are NOT unique across CoinGecko, and a $20M token can
# share a ticker with a $2B one.
# -----------------------------------------------------------------------------
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Multipliers Binance actually uses as a symbol prefix. Matching a general
# leading-digits pattern would corrupt real tickers that begin with a number.
# Longest first, so '1000000' is tried before '1000'. '1M' is Binance's short
# form for a million (1MBABYDOGEUSDT); without it the base kept its prefix and
# never matched a market-cap source.
_KNOWN_MULTIPLIER_PREFIXES: tuple[tuple[str, int], ...] = (
    ("1000000", 1_000_000),
    ("100000", 100_000),
    ("10000", 10_000),
    ("1000", 1_000),
    ("1M", 1_000_000),
)

# Quote assets we may encounter. Ordered longest-first so 'USDC' is stripped
# before a shorter suffix could partially match.
_QUOTE_ASSETS = ("USDT", "USDC", "BUSD", "TUSD", "FDUSD", "USD", "BTC", "ETH", "BNB")

# Observed 2026-09-09: Binance lists perps whose symbol contains CJK
# characters (e.g. a meme perp quoted in USDT with a Chinese-character base).
# An A-Z-only pattern silently DROPPED those from the universe snapshot, which
# would understate the funnel and hide them from the rejection wall. They are
# admitted here and disqualified honestly by L1_NO_MCAP instead, since no
# market-cap source resolves such a ticker. Reject only whitespace and control
# characters, which indicate a genuinely malformed payload.
def _is_wellformed(symbol: str) -> bool:
    """True when a symbol is usable: non-empty and free of whitespace.

    Deliberately permissive about the alphabet. A stricter A-Z rule was
    tried first and silently dropped every CJK-named perp from the universe.
    """
    return bool(symbol) and not any(ch.isspace() for ch in symbol)


@dataclass(frozen=True)
class ParsedSymbol:
    """An exchange symbol, decomposed.

    Attributes:
        symbol:          exactly as the exchange lists it, e.g. '1000PEPEUSDT'
        base_asset:      normalised for cross-source lookup, e.g. 'PEPE'
        quote_asset:     e.g. 'USDT'
        price_multiplier: 1000 for a 1000-prefixed contract, else 1
    """

    symbol: str
    base_asset: str
    quote_asset: str
    price_multiplier: int

    @property
    def is_multiplied(self) -> bool:
        return self.price_multiplier != 1


def parse_symbol(symbol: str, quote_asset: str | None = None) -> ParsedSymbol:
    """Decompose an exchange symbol into its parts.

    Args:
        symbol: the exchange's symbol, e.g. '1000PEPEUSDT'.
        quote_asset: the quote asset if the exchange already told us (Binance's
            exchangeInfo does). Passing it avoids guessing from the suffix.

    Returns:
        ParsedSymbol with the multiplier stripped from base_asset.
    """
    raw = (symbol or "").strip().upper()  # .upper() is a no-op for CJK, harmlessly
    if not _is_wellformed(raw):
        raise ValueError(f"not a usable exchange symbol: {symbol!r}")

    if quote_asset:
        quote = quote_asset.strip().upper()
        base = raw[: -len(quote)] if raw.endswith(quote) else raw
    else:
        quote = ""
        base = raw
        for candidate in _QUOTE_ASSETS:
            if raw.endswith(candidate) and len(raw) > len(candidate):
                quote = candidate
                base = raw[: -len(candidate)]
                break

    multiplier = 1
    for prefix, value in _KNOWN_MULTIPLIER_PREFIXES:
        if base.startswith(prefix) and len(base) > len(prefix):
            multiplier = value
            base = base[len(prefix) :]
            break

    if not base:
        raise ValueError(f"symbol {symbol!r} reduced to an empty base asset")

    return ParsedSymbol(
        symbol=raw, base_asset=base, quote_asset=quote, price_multiplier=multiplier
    )


def base_asset_of(symbol: str, quote_asset: str | None = None) -> str:
    """Convenience wrapper: just the normalised base asset."""
    return parse_symbol(symbol, quote_asset).base_asset


def resolve_collisions(parsed: Iterable[ParsedSymbol]) -> dict[str, ParsedSymbol]:
    """Make base assets unique across one venue's symbol list. Keyed by symbol.

    Stripping a multiplier is safe only while nothing else in the list reduces
    to the same name. Observed 2026-09-09: BOBUSDT (Build on Bitcoin) and
    1000000BOBUSDT (an unrelated meme coin) both reduced to BOB, and every
    table keyed on base_asset mixed them -- price_daily held the meme coin's
    price beside the other token's market cap (D-046).

    On a collision every MULTIPLIED contract keeps its prefix as its base
    (1000000BOB, multiplier 1); an unmultiplied contract keeps the plain name.
    The prefixed base matches no market-cap source and fails L1_NO_MCAP, which
    is the honest outcome: nothing here can say which token it is.
    """
    items = list(parsed)
    owners: dict[str, int] = {}
    for p in items:
        owners[p.base_asset] = owners.get(p.base_asset, 0) + 1

    out: dict[str, ParsedSymbol] = {}
    for p in items:
        if p.is_multiplied and owners[p.base_asset] > 1:
            unstripped = (
                p.symbol[: -len(p.quote_asset)]
                if p.quote_asset and p.symbol.endswith(p.quote_asset)
                else p.symbol
            )
            p = ParsedSymbol(
                symbol=p.symbol,
                base_asset=unstripped,
                quote_asset=p.quote_asset,
                price_multiplier=1,
            )
        out[p.symbol] = p
    return out


def parse_universe(
    symbols: Iterable[str], quote_asset: str | None = None
) -> dict[str, ParsedSymbol]:
    """Parse a venue's whole symbol list at once, collisions resolved.

    Keyed by the symbol exactly as passed in. Unparseable symbols are left out;
    a caller that must report them parses individually first.
    """
    parsed: dict[str, ParsedSymbol] = {}
    for symbol in symbols:
        try:
            parsed[symbol] = parse_symbol(symbol, quote_asset)
        except ValueError:
            continue
    resolved = resolve_collisions(parsed.values())
    return {symbol: resolved[p.symbol] for symbol, p in parsed.items()}


def funding_apr(rate: float, interval_hours: float) -> float:
    """Annualise a funding rate using THIS symbol's settlement interval.

    Never `rate * 3 * 365`. Binance settlement is no longer uniformly 8h -- it
    varies per symbol (8h, 4h, sometimes 1h). Assuming 8h everywhere mis-ranks
    the entire universe, because a 4h-interval symbol accrues funding twice as
    often as the naive formula assumes.

    Args:
        rate: the raw funding rate for one settlement period.
        interval_hours: that symbol's settlement interval, fetched not assumed.
    """
    if not interval_hours or interval_hours <= 0:
        raise ValueError(
            f"funding interval must be positive, got {interval_hours!r}. "
            "Refusing to guess 8h: that is the mis-ranking this function exists to prevent."
        )
    periods_per_year = 8760.0 / float(interval_hours)
    return float(rate) * periods_per_year


__all__ = [
    "ParsedSymbol",
    "base_asset_of",
    "funding_apr",
    "parse_symbol",
    "parse_universe",
    "resolve_collisions",
]
