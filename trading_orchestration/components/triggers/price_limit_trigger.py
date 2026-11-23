from task_orchestration.models import Proposal


class PriceLimitTrigger:
    def __init__(self, bridge):
        self.bridge = bridge

    def evaluate(self, now_ns):
        host = self.bridge.host
        # Do not consume a price-limit latch while a planner's captured input is
        # in flight. Reassess the accepted target and latest prices after release.
        if self.bridge.engine.owner_task() in {
                "exposure_correction", "auction_planning", "daily_planning",
                "manual_replan", "scheduled_replan", "price_limit_replan"}:
            return None
        values = host._price_limit_replan_triggers.collect(host, host._clock_date())
        if values:
            return Proposal(repr(values), host._price_limit_replan_triggers.validity,
                            self.bridge.deadline())
        return None

