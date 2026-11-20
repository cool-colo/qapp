from trading_orchestration.contexts.exposure_context import ExposurePlan


class ExposureOutcomePolicy:
    def decide(self, result: ExposurePlan) -> str:
        return {"replan": "request_risk_manager", "planned": "execute",
                "satisfied": "complete", "unknown": "wait", "waiting": "wait"}[result.status]

