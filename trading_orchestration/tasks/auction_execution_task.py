from task_orchestration.models import Decision, RunState
from trading_orchestration.components.stabilizers.nautilus_state_stabilizer import NautilusStateStabilizer


class AuctionExecutionTask:
    def __init__(self, bridge):
        self.bridge = bridge
        self.stabilizer = NautilusStateStabilizer(bridge)

    def advance(self, context):
        host = self.bridge.host
        if not host._within_auction_submit_time():
            return Decision(RunState.SUCCEEDED, "auction_submit_window_closed")
        with self.bridge.operations():
            host.process_opening_auction_execution()
        return Decision(RunState.RUNNING, "auction_orders_tracked")

    def quiesce(self, context):
        return self.stabilizer.stabilize(context)

