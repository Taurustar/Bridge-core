"""Character state poll APIs (plan sections 6.4, 15.2, 15.7).

``GET /state`` is a read-only poll: needs ``peek`` projects a snapshot and
never persists (no materialization, no timestamp advance).
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from ..app import error_body


def register_state_routes(app, bridge) -> None:
    @app.get("/state")
    async def get_state():
        if not (bridge.needs.available and bridge.config.STATE_EXPRESSION_ENABLED):
            return JSONResponse(
                status_code=403,
                content=error_body(
                    "feature_disabled",
                    "Needs/state expression is disabled on this deployment.",
                ),
            )
        owner = bridge.config.OWNER_USER_ID
        snapshot = await bridge.needs.peek(owner)
        from ..state_expression import build_state_block

        return {
            "zones": snapshot["zones"],
            "values": snapshot["values"],
            "shutdown": snapshot["shutdown"],
            "state_block": build_state_block(
                snapshot["zones"], bridge._read_identity("state")
            ),
        }
