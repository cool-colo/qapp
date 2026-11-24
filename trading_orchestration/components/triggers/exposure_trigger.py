from task_orchestration.models import Proposal
from trading_orchestration.components.assessors.exposure_assessor import ExposureAssessor


class ExposureTrigger:
    def __init__(self, bridge):
        self.bridge = bridge
        self.assessor = ExposureAssessor(bridge.config.exposure.tolerance)

    def evaluate(self, now_ns):
        if not self.bridge.exposure_allowed():
            return None
        snapshot = self.bridge.snapshot_provider.capture_positions()
        result = self.assessor.assess(snapshot)
        self.bridge.last_exposure_assessment = result.evidence()
        if result.status == "correction":
            return Proposal(f"{snapshot.trading_date}:{snapshot.target.version}:{result.direction}",
                            expires_ns=self.bridge.deadline())
        return None

