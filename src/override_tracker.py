import os
import json
import logging

import boto3

logger = logging.getLogger(__name__)


class OverrideTracker:
    """Remembers the setpoint WE last applied, via an SSM String parameter.

    This is the only cross-invocation memory the reconciler keeps (everything
    else is recomputed fresh each run). It's what lets `reconcile()` tell "the
    schedule moved on since our last write" apart from "something else (a
    manual boost via the Tado app) changed the setpoint since our last
    write" -- the Tado API itself gives us no such signal (see main.py).

    Schema (all keys optional/absent when unset):
        last_applied_temp: float  -- setpoint we last wrote via the API
        last_applied_at:   str    -- ISO timestamp of that write
        override_since:    str    -- ISO timestamp we first noticed the
                                      current override (unset once cleared)
        override_temp:     float  -- the overridden setpoint being tracked
    """

    def __init__(self, param_name=None, ssm_client=None):
        self.param_name = param_name or os.environ.get("LAST_APPLIED_PARAM_NAME", "/tado/last_applied")
        self.ssm = ssm_client or boto3.client("ssm")

    def load(self):
        try:
            resp = self.ssm.get_parameter(Name=self.param_name)
            return json.loads(resp["Parameter"]["Value"])
        except self.ssm.exceptions.ParameterNotFound:
            return {}
        except (json.JSONDecodeError, KeyError):
            logger.warning("Override-tracker parameter %s present but unreadable/corrupt. Treating as empty.", self.param_name)
            return {}

    def save(self, state):
        self.ssm.put_parameter(
            Name=self.param_name,
            Value=json.dumps(state),
            Type="String",
            Overwrite=True,
        )
