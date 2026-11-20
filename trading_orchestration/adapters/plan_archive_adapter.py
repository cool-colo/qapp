from datetime import date
from strategies.model_target_planners.base import ModelTargetPlan, TargetInfo, TargetContext
from nautilus_trader.model.enums import MarketStatusAction


def plan_from_archive(archive):
    raw = archive.get("plan")
    if not isinstance(raw, dict) or "targets" not in raw:
        raise ValueError("invalid committed target payload")
    targets = []
    for row in raw["targets"]:
        context = dict(row["target_context"])
        if context.get("recent_target_date"):
            context["recent_target_date"] = date.fromisoformat(context["recent_target_date"])
        if context.get("market_status"):
            context["market_status"] = MarketStatusAction[context["market_status"]]
        targets.append(TargetInfo(
            stock_code=row["stock_code"], weight=row["weight"], quantity=row["quantity"],
            target_context=TargetContext(**context), target_version=row["target_version"],
            instrument_id=row["instrument_id"], is_locked=row["is_locked"]))
    return ModelTargetPlan(
        trading_date=date.fromisoformat(raw["trading_date"]),
        signal_date=date.fromisoformat(raw["signal_date"]) if raw["signal_date"] else None,
        targets=targets, reason=raw["reason"], request_id=raw.get("request_id"),
        total_asset=raw.get("total_asset"), investable_asset=raw.get("investable_asset"),
        persistable=raw.get("persistable", True), degraded_reason=raw.get("degraded_reason"),
        parent_version=raw.get("parent_version"))

