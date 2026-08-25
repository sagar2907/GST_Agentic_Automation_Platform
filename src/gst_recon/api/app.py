"""The HTTP surface over the review queue.

Small on purpose. Every state change goes through ``ReviewQueue.resolve``,
which is ordinary testable code with no request object in sight; this module
turns HTTP into arguments and outcomes into pages, and does nothing else. The
properties worth trusting are properties of the queue, not of a route.

Three decisions about the surface itself.

**GET never changes anything.** Not merely by convention -- there is no
mutating handler behind a GET, so a prefetching browser, a link scanner or a
reviewer refreshing after a decision cannot approve anything.

**Forms are read without a form-parsing dependency.** A urlencoded body is a
query string; the standard library already parses those. It saves a package
whose only job here would be to split on ``&``.

**The response says the page runs no script.** A content-security policy of
``default-src 'none'`` with styles inlined is easy to state honestly because
the pages genuinely load nothing and execute nothing, and this page renders
model-written text.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sse_starlette.sse import EventSourceResponse

from gst_recon.api import views
from gst_recon.api.queue import (
    AlreadyResolvedError,
    ReviewAction,
    ReviewError,
    ReviewQueue,
    StaleViewError,
    UnknownItemError,
    UnnamedApproverError,
)
from gst_recon.domain.taxonomy import ImsAction

# A page that loads nothing and runs nothing can say so without exceptions.
# style-src 'unsafe-inline' covers the single inline stylesheet; there is no
# script-src allowance at all, so an injected tag has nothing to execute under.
CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'"

STATUS_FOR: dict[type[Exception], int] = {
    UnknownItemError: 404,
    AlreadyResolvedError: 409,
    StaleViewError: 409,
    UnnamedApproverError: 400,
    ReviewError: 400,
}


def _page(markup: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        markup,
        status_code=status,
        headers={"content-security-policy": CSP, "referrer-policy": "no-referrer"},
    )


async def _form(request: Request) -> dict[str, str]:
    """Read a urlencoded body without a form-parsing dependency."""
    body = (await request.body()).decode("utf-8", errors="replace")
    return dict(parse_qsl(body, keep_blank_values=True))


def _status_for(error: Exception) -> int:
    for kind, status in STATUS_FOR.items():
        if isinstance(error, kind):
            return status
    return 400


def create_app(review: ReviewQueue, *, clock=None) -> FastAPI:
    """Build the app over one queue.

    ``clock`` is injected rather than called from inside a handler. Audit
    timestamps are the ordering an auditor reads, and an ordering that cannot
    be frozen in a test is an ordering nobody has tested.
    """
    now = clock or (lambda: datetime.now(UTC))
    app = FastAPI(title="GST review queue", docs_url=None, redoc_url=None)
    changed = asyncio.Event()

    def touched() -> None:
        changed.set()
        changed.clear()

    @app.get("/", response_class=HTMLResponse)
    async def queue_page() -> Response:
        return _page(views.queue_page(review))

    @app.get("/review/{exception_id}", response_class=HTMLResponse)
    async def review_page(exception_id: str, error: str = "") -> Response:
        if exception_id in review.resolved:
            return _page(views.resolved_page(review, exception_id))
        try:
            item = review.get(exception_id)
        except UnknownItemError as exc:
            return _page(views.error_page(str(exc), 404), 404)
        return _page(views.review_page(review, item, error))

    async def _resolve(
        request: Request,
        exception_id: str,
        review_action: ReviewAction,
    ) -> Response:
        form = await _form(request)
        override = form.get("override", "")
        try:
            review.resolve(
                exception_id,
                approver=form.get("approver", ""),
                review_action=review_action,
                confirmation=form.get("confirmation", ""),
                recorded_at=now(),
                override=ImsAction(override) if override else None,
                note=form.get("note", ""),
            )
        except (ReviewError, ValueError) as exc:
            # Both, and not one: the queue's refusals are ReviewErrors, while
            # ImsAction() raises a plain ValueError for a name that is not an
            # action at all -- which is what a hand-made request carries. They
            # are the same refusal from a caller's point of view.
            status = _status_for(exc)
            if isinstance(exc, UnknownItemError):
                return _page(views.error_page(str(exc), status), status)
            item = review.items.get(exception_id)
            if item is None:
                return _page(views.error_page(str(exc), 404), 404)
            if exception_id in review.resolved:
                return _page(views.resolved_page(review, exception_id), status)
            return _page(views.review_page(review, item, str(exc)), status)

        touched()
        # See-other, so a refresh after deciding re-reads a page rather than
        # re-posting a decision.
        return RedirectResponse(f"/review/{exception_id}", status_code=303)

    @app.post("/review/{exception_id}/approve")
    async def approve(request: Request, exception_id: str) -> Response:
        return await _resolve(request, exception_id, ReviewAction.APPROVE)

    @app.post("/review/{exception_id}/override")
    async def override(request: Request, exception_id: str) -> Response:
        return await _resolve(request, exception_id, ReviewAction.OVERRIDE)

    @app.post("/review/{exception_id}/hold")
    async def hold(request: Request, exception_id: str) -> Response:
        return await _resolve(request, exception_id, ReviewAction.HOLD)

    @app.get("/api/queue")
    async def queue_json() -> JSONResponse:
        """The queue as data, for anything that is not a browser."""
        return JSONResponse(
            {
                "period": review.return_period,
                "days_to_cutoff": review.days_to_cutoff,
                "pending": len(review.pending()),
                "amount_at_risk": f"{review.amount_awaiting_review:.2f}",
                "items": [
                    {
                        "exception_id": item.exception_id,
                        "exception_class": item.exception_class,
                        "supplier_gstin": item.supplier_gstin,
                        "proposed_action": item.proposed_action.value,
                        "amount_at_risk": f"{item.amount_at_risk:.2f}",
                        "confidence": item.confidence,
                        "irreversible": item.is_irreversible,
                    }
                    for item in review.pending()
                ],
            }
        )

    @app.get("/api/audit")
    async def audit_state() -> JSONResponse:
        """Whether the decision chain still verifies, and how long it is."""
        intact, detail = review.audit.verify_chain()
        return JSONResponse({"entries": len(review.audit), "intact": intact, "detail": detail})

    @app.get("/events")
    async def events() -> EventSourceResponse:
        """Depth of the queue, pushed when it changes.

        Deliberately carries no case content. A stream a browser holds open is
        the wrong place for supplier names and amounts; anything wanting detail
        can ask for it and be answered under the same policy as a page.
        """

        async def stream() -> AsyncIterator[dict[str, str]]:
            while True:
                yield {
                    "event": "queue",
                    "data": json.dumps(
                        {
                            "pending": len(review.pending()),
                            "resolved": len(review.resolved),
                            "amount_at_risk": f"{review.amount_awaiting_review:.2f}",
                        }
                    ),
                }
                await changed.wait()

        return EventSourceResponse(stream())

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app
