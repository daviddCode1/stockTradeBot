"""Transaction-cost model for a GBP account trading USD stocks on Trading 212 Invest/ISA.

Every buy converts GBP->USD and every sell converts USD->GBP (the API does not support
multi-currency balances), so the FX fee is paid on BOTH legs. Verified on the demo account:
a £4,545.68 buy was charged a CURRENCY_CONVERSION_FEE of £6.81 (= 0.15%).
"""
from __future__ import annotations

from dataclasses import dataclass  # immutable parameter bundle
from typing import Any, Mapping  # type hints


@dataclass(frozen=True)
class CostModel:
    """All cost assumptions in one place (fractions unless stated)."""

    fx_fee: float = 0.0015  # FX conversion fee per side
    slippage: float = 0.0005  # spread + impact per side for normal fills
    stop_slippage: float = 0.005  # extra adverse slippage when a stop turns into a market order
    sec_rate: float = 0.0000278  # SEC Section 31 fee on sell value
    finra_per_share: float = 0.000195  # FINRA TAF per share sold (USD)
    dividend_withholding: float = 0.15  # US withholding tax on dividends (W-8BEN)

    @classmethod
    def from_settings(cls, cfg: Mapping[str, Any], slippage_mult: float = 1.0, fx_fee: float | None = None) -> "CostModel":
        """Build from settings.yaml, optionally stressed (higher slippage / FX fee)."""
        c, r = cfg["costs"], cfg["risk"]
        return cls(
            fx_fee=float(fx_fee if fx_fee is not None else c["fx_fee_per_side"]),
            slippage=float(c["slippage_per_side"]) * slippage_mult,
            stop_slippage=float(r["stop_slippage"]) * slippage_mult,
            sec_rate=float(c["sec_fee_rate_on_sells"]),
            finra_per_share=float(c["finra_taf_per_share_sold"]),
            dividend_withholding=float(c["dividend_withholding"]),
        )

    # --- cash flows (GBP) -------------------------------------------------------------
    def buy_cost_gbp(self, qty: float, px_usd: float, fx: float) -> float:
        """GBP debited for buying qty at px (USD), incl. FX fee. fx = USD per GBP."""
        return qty * px_usd / fx * (1.0 + self.fx_fee)

    def sell_proceeds_gbp(self, qty: float, px_usd: float, fx: float) -> float:
        """GBP credited for selling qty at px (USD), after SEC/FINRA fees and FX fee."""
        gross_usd = qty * px_usd  # sale value
        reg_fees = self.sec_rate * gross_usd + self.finra_per_share * qty  # US regulatory fees
        return (gross_usd - reg_fees) / fx * (1.0 - self.fx_fee)  # convert back to GBP

    def round_trip_cost_per_share_usd(self, entry_px: float, exit_px: float) -> float:
        """Explicit costs per share for one round trip, in USD (slippage handled via prices)."""
        return self.fx_fee * entry_px + self.fx_fee * exit_px + self.sec_rate * exit_px + self.finra_per_share
