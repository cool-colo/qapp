import threading


class OperationState(threading.local):
    """An operation permit belongs to its callback thread, never the whole process."""

    def __init__(self):
        self.driving = False
        self.mode = None
        self.execution_depth = 0
        self.operations_open = False
        self.profile_windows = None
        self.active_profile = None
        self.installing = False
