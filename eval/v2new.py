"""Two scenarios written for Nabd v2, AFTER tag nabd-v2 and pre-registered on main in eval/V2_SCENARIOS.md
(commit 1985d8c) before this file existed. Not held-out in the v1.1 sense: same builder, after v2.

Run:  python eval/run_eval.py run --set v2new --reps 5 --out eval/results_v2.csv --frozen eval/FROZEN_V2.json
"""

from dataclasses import dataclass, replace

import chaos
from heldout import HeldoutScenario


@dataclass
class V2Scenario(HeldoutScenario):
    recover_or_escalate: bool = False  # pass if recovered OR escalated (V2_SCENARIOS.md, partial_api_errors)
    url_cause: bool = False            # the provider URL is a cause: "payments-v1", or "provider" + "url"

    def for_grading(self, root_cause_text: str | None) -> "V2Scenario":
        """The scenario as the frozen grader should see it for this run. The grader's cause check is
        "any word of each group appears"; the pre-registered URL rule is "payments-v1, or provider AND url",
        so "provider" joins the group only when "url" is in the text too."""
        if not self.url_cause:
            return self
        text = (root_cause_text or "").lower()
        url_words = ["payments-v1"] + (["provider"] if "provider" in text and "url" in text else [])
        return replace(self, causes=[*self.causes, url_words])


# ---------------------------------------------------------------- 1. two_silent_causes
def two_silent_causes() -> None:
    """v1.4 (ledger SAR x100, with a deploy entry) plus a hand-edited provider URL (no deploy entry)."""
    chaos.set_config("version", "v1.4")
    chaos.add_deploy_entry("ledger sync job: batch size tuning", by="ci")
    chaos.set_config("payment_provider_url", chaos.BAD_PROVIDER_URL)  # by hand: nothing in deploy history


# ---------------------------------------------------------------- 2. partial_api_errors
def partial_api_errors() -> None:
    """Deploy v1.7: about 30% of POST /orders return 500 (hook in service/app.py)."""
    chaos.set_config("version", "v1.7")
    chaos.add_deploy_entry("order API: request rate limiter", by="ci")


V2NEW = {s.name: s for s in [
    V2Scenario("two_silent_causes", two_silent_causes, "recover", [["v1.4"]],
               version_is_cause=True, url_cause=True),
    V2Scenario("partial_api_errors", partial_api_errors, "recover", [["v1.7"]],
               version_is_cause=True, no_config_changes=True, recover_or_escalate=True),
]}
