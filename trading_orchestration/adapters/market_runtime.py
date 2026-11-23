from zoneinfo import ZoneInfo


class NautilusMarketRuntime:
    """Infrastructure binding, with runtime dependencies installed by Nautilus."""

    def __init__(self, host):
        self.host = host

    @property
    def clock(self):
        return self.host.clock

    def now_ns(self):
        return self.clock.timestamp_ns()

    def local_now(self):
        return self.clock.utc_now().astimezone(ZoneInfo(self.host.config.timezone_name))

    def fetch(self, source):
        return self.host._schedule_fetch(source)

    def dispatch(self, callback):
        if self.host._async_scheduler.has_running_loop:
            self.host._run_on_loop(callback)
        else:
            # TestClock replay is already on its single owner thread.
            callback()

    def info(self, message):
        self.host.log.info(message)
