from task_orchestration.models import Decision, RunState
from task_orchestration.components.processes.task_process import BusinessTask
from trading_orchestration.components.executors.auction_preparation import AuctionExecutionPreparation


class AuctionExecutionTask(BusinessTask):
    def __init__(self, bridge):
        self.bridge = bridge
        self.preparation = AuctionExecutionPreparation(bridge)

    def advance(self, context):
        host = self.bridge.host
        if not host._within_auction_submit_time():
            return Decision(RunState.SUCCEEDED, 'auction_submit_window_closed')
        prepared = self.preparation.prepare(context)
        if prepared.state != RunState.SUCCEEDED:
            return prepared
        with self.bridge.operations():
            host.execute_opening_auction()
        if host.opening_auction_orders_accepted():
            return Decision(RunState.SUCCEEDED, 'auction_orders_accepted', prepared.evidence)
        return Decision(RunState.RUNNING, 'auction_orders_tracked', prepared.evidence)

    def interrupt(self, context):
        self.preparation.abort()
