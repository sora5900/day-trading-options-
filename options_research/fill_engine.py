"""The paper fill engine — the component that decides whether this is real.

Spec §5, non-negotiable rules:
1. Buying pays the ASK; selling receives the BID. Never the mid.
2. Configurable slippage on top (default 1 tick).
3. Unfillable contracts are rejected, not filled: spread% > SPREAD_MAX,
   volume == 0, or open_interest < min. Logged as UNFILLABLE.
4. Commissions modelled explicitly, both ways.
5. Mid recorded alongside fill so spread cost is always quantifiable.
6. No look-ahead: quotes must be timestamped <= decision time (enforced by
   the db.chain_at/quote_for_leg query layer; asserted in tests).

Most backtests lie by filling at mid. This engine refuses to.
"""

from dataclasses import dataclass, field

from .config import Config

CONTRACT_MULT = 100  # equity options multiplier


@dataclass
class Leg:
    expiry: str
    strike: float
    right: str          # 'C' / 'P'
    qty: int            # +N = long (buy to open), -N = short (sell to open)


@dataclass
class LegFill:
    leg: Leg
    fill: float         # per-share price actually paid/received
    mid: float
    spread_pct: float


@dataclass
class StructureFill:
    ok: bool
    reason: str = ""                 # empty if ok; else why UNFILLABLE
    legs: list = field(default_factory=list)   # list[LegFill]
    net_fill: float = 0.0            # signed per-share: >0 = debit paid, <0 = credit received
    net_mid: float = 0.0
    spread_pct: float = 0.0          # worst single-leg spread% in the structure
    commissions: float = 0.0         # dollars, this side only


class PaperFillEngine:
    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or Config()

    def slippage_model(self) -> str:
        return f"bid-ask+{self.cfg.slippage_ticks}tick"

    def _check_fillable(self, q) -> str:
        """Return "" if fillable, else the rejection reason."""
        bid, ask = q["bid"], q["ask"]
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return "no_quote"
        if ask < bid:
            return "crossed_book"
        mid = (bid + ask) / 2
        if mid <= 0:
            return "no_quote"
        if (ask - bid) / mid > self.cfg.spread_max_pct:
            return f"spread_{(ask - bid) / mid:.1%}_gt_max"
        if (q["volume"] or 0) == 0:
            return "zero_volume"
        if (q["open_interest"] or 0) < self.cfg.min_open_interest:
            return f"oi_{q['open_interest'] or 0}_lt_{self.cfg.min_open_interest}"
        return ""

    def fill_leg(self, quote, buying: bool, force: bool = False) -> LegFill | str:
        """Fill one leg or return a rejection-reason string.

        `buying` is the direction of THIS transaction (opening a long or
        closing a short = buying; opening a short or closing a long = selling).

        `force=True` (exits only): skip the liquidity gates — you can always
        cross the spread to get OUT, and paying a horrible spread on exit is
        exactly the cost this engine exists to record. A quote must still
        exist and not be crossed.
        """
        reason = self._check_fillable(quote)
        if reason:
            if not (force and reason not in ("no_quote", "crossed_book")
                    and not reason.startswith("crossed")):
                return reason
        bid, ask = quote["bid"], quote["ask"]
        mid = (bid + ask) / 2
        slip = self.cfg.slippage_ticks * self.cfg.tick_size
        if buying:
            price = ask + slip
        else:
            price = max(bid - slip, self.cfg.tick_size)
        return LegFill(leg=None, fill=round(price, 4), mid=round(mid, 4),
                       spread_pct=round((ask - bid) / mid, 6))

    def fill_structure(self, legs_with_quotes, closing: bool = False,
                       force: bool = False) -> StructureFill:
        """Fill a multi-leg structure all-or-none.

        legs_with_quotes: list of (Leg, quote_row). Any unfillable leg rejects
        the whole structure — partial fills of a spread are not a thing we
        pretend to get.

        Sign convention: net_fill > 0 means cash out (debit), < 0 cash in
        (credit), per share of one spread unit.
        """
        fills, net_fill, net_mid, worst_spread, n_contracts = [], 0.0, 0.0, 0.0, 0
        for leg, quote in legs_with_quotes:
            if quote is None:
                return StructureFill(ok=False, reason="missing_quote")
            buying = (leg.qty > 0) != closing   # closing flips every leg
            res = self.fill_leg(quote, buying=buying, force=force)
            if isinstance(res, str):
                return StructureFill(ok=False, reason=res)
            res.leg = leg
            fills.append(res)
            qty = abs(leg.qty)
            sign = 1 if buying else -1
            net_fill += sign * res.fill * qty
            net_mid += sign * res.mid * qty
            worst_spread = max(worst_spread, res.spread_pct)
            n_contracts += qty
        commissions = n_contracts * self.cfg.commission_per_contract
        return StructureFill(ok=True, legs=fills,
                             net_fill=round(net_fill, 4),
                             net_mid=round(net_mid, 4),
                             spread_pct=worst_spread,
                             commissions=round(commissions, 2))

    @staticmethod
    def mark_structure(legs_with_quotes, closing: bool = True) -> float | None:
        """Mid-based mark of the structure's CLOSE value (for MAE/MFE tracking
        only — never for fills). Positive = closing costs cash, negative =
        closing brings cash in. Returns None if any quote missing/invalid."""
        net = 0.0
        for leg, quote in legs_with_quotes:
            if quote is None or quote["bid"] is None or quote["ask"] is None \
                    or quote["ask"] <= 0:
                return None
            mid = (quote["bid"] + quote["ask"]) / 2
            buying = (leg.qty > 0) != closing
            net += (1 if buying else -1) * mid * abs(leg.qty)
        return round(net, 4)
