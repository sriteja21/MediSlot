import json
import os
import time


def emit_metric(name: str, value: float = 1, unit: str = "Count") -> None:
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "MediSlot",
                "Dimensions": [["Environment"]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        "Environment": os.environ.get("ENVIRONMENT", "dev"),
        name: value,
    }))
