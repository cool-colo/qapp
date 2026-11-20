from task_orchestration.models import Proposal


class PriceLimitTrigger:
    def __init__(self, bridge):
        self.bridge = bridge

    def evaluate(self, now_ns):
        host = self.bridge.host
        if self.bridge.engine.owner_task() == "exposure_correction":
            return None
        values = host._price_limit_replan_triggers.collect(host, host._clock_date())
        if values:
            return Proposal(repr(values), host._price_limit_replan_triggers.validity,
                            self.bridge.deadline())
        return None

