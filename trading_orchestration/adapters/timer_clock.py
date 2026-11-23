import pandas as pd


class NautilusTimerClock:
    def __init__(self, host):
        self.host = host

    def now_ns(self):
        return self.host.clock.timestamp_ns()

    def arm(self, name, at_ns, callback):
        self.host.clock.set_time_alert(name=name, alert_time=pd.Timestamp(at_ns, unit='ns', tz='UTC'),
                                       callback=lambda _: callback(), override=True)

    def cancel(self, name):
        if name in self.host.clock.timer_names:
            self.host.clock.cancel_timer(name)
