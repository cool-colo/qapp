from trading_orchestration.contexts.exposure_context import ExposureContext, ExposurePlan


class ExposureValidator:
    def validate(self, context: ExposureContext, goal: ExposurePlan) -> bool:
        if (context.target is None or goal.status != "planned" or context.pending_orders
                or context.quality_error or goal.parent_version != context.target.version
                or goal.execution_revision != context.execution_revision):
            return False
        for iid, quantity in goal.quantities.items():
            item = context.instruments.get(iid)
            if item is None:
                return False
            target = context.target.quantities.get(iid, 0)
            if goal.direction == "buy":
                if not item.quantity <= quantity <= max(item.quantity, target):
                    return False
            elif goal.direction == "sell":
                if not min(item.quantity, target) <= quantity <= item.quantity:
                    return False
            else:
                return False
        return True

