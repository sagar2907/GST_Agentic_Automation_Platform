"""The pages a reviewer sees.

Every value that reaches a page passes through ``html.tag`` or ``html.text``
and is escaped there. Nothing in this module calls ``html.raw``, which is the
property to check when reading it: the only trusted markup in the whole surface
is the stylesheet, and it lives in ``html.py``.

What is shown is chosen by what a person needs in order to disagree. A card
that only said "the agent proposes REJECT, confidence 0.94" would be a button
with a number next to it, and clicking it would be assent rather than review.
So the rationale is shown, and every cited claim is shown beside the tool call
it came from, because that citation is the one part of a Finding that can be
checked mechanically -- and a reviewer who cannot see it cannot check anything.
"""

from __future__ import annotations

from decimal import Decimal

from gst_recon.api.html import Safe, document, join, tag
from gst_recon.api.queue import OVERRIDES, ReviewItem, ReviewQueue
from gst_recon.domain.taxonomy import ImsAction


def _money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def _action_tag(action: ImsAction) -> Safe:
    classes = "tag reject" if action is ImsAction.REJECT else "tag"
    return tag("span", action.value, class_=classes)


def _cutoff_note(days: int) -> Safe:
    if days <= 0:
        return tag("strong", "the cut-off has passed")
    word = "day" if days == 1 else "days"
    return tag(
        "span",
        f"{days} {word} to the cut-off. ",
        tag("strong", "Anything left unactioned then is treated as accepted."),
    )


def queue_page(review: ReviewQueue) -> Safe:
    """The worklist, highest exposure first."""
    pending = review.pending()
    if pending:
        body = tag(
            "table",
            tag(
                "tr",
                *[
                    tag("th", heading)
                    for heading in ("Document", "Class", "Supplier", "Proposed", "At risk", "")
                ],
            ),
            join(
                [
                    tag(
                        "tr",
                        tag("td", tag("a", item.exception_id, href=f"/review/{item.exception_id}")),
                        tag("td", item.exception_class),
                        tag("td", item.supplier_gstin),
                        tag("td", _action_tag(item.proposed_action)),
                        tag("td", _money(item.amount_at_risk), class_="num"),
                        tag("td", tag("a", "review", href=f"/review/{item.exception_id}")),
                    )
                    for item in pending
                ]
            ),
        )
    else:
        body = tag("p", "Nothing is waiting on a person.", class_="empty")

    return document(
        "Review queue",
        tag("h1", f"{len(pending)} awaiting review"),
        tag(
            "p",
            f"Period {review.return_period}. ",
            f"₹{_money(review.amount_awaiting_review)} at risk. ",
            _cutoff_note(review.days_to_cutoff),
            class_="sub",
        ),
        body,
        # Ten seconds: long enough not to interrupt someone reading a card,
        # short enough that a second reviewer's decision shows up before a
        # colleague starts working the same case.
        refresh=10,
    )


def _evidence(item: ReviewItem) -> Safe:
    if not item.evidence:
        return tag("p", "No evidence was cited.", class_="empty")
    return join(
        [
            tag(
                "div",
                tag("p", cited.claim, class_="claim"),
                tag("p", f"{cited.tool_name} · {cited.tool_call_id}", class_="cite"),
                class_="card",
            )
            for cited in item.evidence
        ]
    )


def _decision_form(item: ReviewItem) -> Safe:
    """The reviewer's whole vocabulary, as three buttons and one select.

    There is no field here that names an action in free text. A reviewer can
    approve what was proposed, substitute one of a fixed set, or hold -- and
    nothing else is expressible, which is the same argument the agent's tool
    surface makes about mutation.
    """
    confirmation = tag("input", type="hidden", name="confirmation", value=item.confirmation())
    approver = tag("input", type="text", name="approver", placeholder="your name", required=True)
    note = tag("input", type="text", name="note", placeholder="note (optional)", size="34")
    return join(
        [
            tag(
                "form",
                confirmation,
                approver,
                note,
                tag("button", f"Approve {item.proposed_action.value}", type="submit"),
                method="post",
                action=f"/review/{item.exception_id}/approve",
            ),
            tag(
                "form",
                tag("input", type="hidden", name="confirmation", value=item.confirmation()),
                tag("input", type="text", name="approver", placeholder="your name", required=True),
                tag(
                    "select",
                    *[tag("option", action.value, value=action.value) for action in OVERRIDES],
                    name="override",
                ),
                tag("button", "Override", type="submit"),
                method="post",
                action=f"/review/{item.exception_id}/override",
            ),
            tag(
                "form",
                tag("input", type="hidden", name="confirmation", value=item.confirmation()),
                tag("input", type="text", name="approver", placeholder="your name", required=True),
                tag("button", "Hold", type="submit"),
                method="post",
                action=f"/review/{item.exception_id}/hold",
            ),
        ]
    )


def review_page(review: ReviewQueue, item: ReviewItem, error: str = "") -> Safe:
    """One case, with everything needed to disagree with the proposal."""
    warning = (
        tag(
            "p",
            tag("strong", "This is irreversible within the cycle. "),
            "A reject purges the invoice value from GSTR-2B for the period.",
            class_="err",
        )
        if item.is_irreversible
        else Safe("")
    )
    confidence = "not stated" if item.confidence is None else f"{item.confidence:.2f}"
    return document(
        f"Review {item.exception_id}",
        tag("p", tag("a", "← queue", href="/"), class_="sub"),
        tag("h1", item.exception_id),
        tag(
            "p",
            f"{item.exception_class} · {item.supplier_gstin} · invoice {item.invoice_number} · ",
            f"₹{_money(item.amount_at_risk)} at risk",
            class_="sub",
        ),
        # The deadline belongs on the page where the decision is made, not
        # only on the list. Whether a case can wait until tomorrow is part of
        # judging it.
        tag("p", _cutoff_note(review.days_to_cutoff), class_="sub"),
        tag("p", error, class_="err") if error else Safe(""),
        warning,
        tag("h2", "Proposed"),
        tag("p", _action_tag(item.proposed_action), f" at confidence {confidence}"),
        tag("p", item.detail) if item.detail else Safe(""),
        tag("h2", "Why the gate held it"),
        tag("ul", join([tag("li", reason) for reason in item.reasons])),
        tag("h2", "The agent's reasoning"),
        tag("p", item.rationale or "The agent produced no rationale."),
        tag("h2", "Evidence"),
        _evidence(item),
        tag("h2", "Decide"),
        _decision_form(item),
        tag(
            "p",
            "An approval is recorded against your name before anything is sent to the portal.",
            class_="sub",
        ),
    )


def resolved_page(review: ReviewQueue, exception_id: str) -> Safe:
    """What was done, after the fact."""
    outcome = review.resolved[exception_id]
    portal = outcome.submission.outcome.value if outcome.submission is not None else "not submitted"
    return document(
        f"Decided {exception_id}",
        tag("p", tag("a", "← queue", href="/"), class_="sub"),
        tag("h1", f"{exception_id} · {outcome.final_action.value}"),
        tag(
            "table",
            tag("tr", tag("th", "Reviewer"), tag("td", outcome.approver)),
            tag("tr", tag("th", "Review"), tag("td", outcome.review_action.value)),
            tag("tr", tag("th", "Action"), tag("td", outcome.final_action.value)),
            tag("tr", tag("th", "Portal"), tag("td", portal)),
            tag("tr", tag("th", "Audit entry"), tag("td", str(outcome.entry.sequence))),
            tag("tr", tag("th", "Digest"), tag("td", outcome.entry.digest())),
        ),
    )


def error_page(message: str, status: int) -> Safe:
    return document(
        f"{status}",
        tag("h1", str(status)),
        tag("p", message, class_="err"),
        tag("p", tag("a", "← queue", href="/"), class_="sub"),
    )
