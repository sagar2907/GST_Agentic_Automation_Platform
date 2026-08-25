"""HTML where escaping is structural rather than remembered.

This page renders text a model wrote, and in Tier 3 that model has read free
text a supplier sent. So the rationale on a review card is, in the strict
sense, attacker-influenced content, and rendering it into a page is the
classic injection surface. The reviewer's browser holds their session with
this system; a script running in it acts as them.

The usual answer is "remember to escape". That is a discipline, and
disciplines fail silently at exactly the interpolation nobody looked at twice.

So escaping is enforced by type instead. ``tag`` escapes every child string it
is given, and the only way to get raw markup into a page is to hold a ``Safe``,
which only ``tag`` and ``raw`` produce. Forgetting to escape is not a mistake
you can make here: the mistake would have to be an explicit ``raw`` call, which
is greppable in a way an f-string is not.

This is the same move as the agent's tool surface having no mutating tool. Make
the dangerous thing structurally unavailable rather than conditionally guarded.
"""

from __future__ import annotations

from html import escape

# Tags that carry no children and take no closing tag.
VOID = frozenset({"br", "hr", "img", "input", "link", "meta"})


class Safe(str):
    """Markup that is already known to be safe to emit verbatim.

    A plain ``str`` reaching ``tag`` is always escaped. Only this type passes
    through, and nothing constructs it except the helpers below.
    """

    __slots__ = ()


def raw(markup: str) -> Safe:
    """Declare a literal as trusted markup.

    Reserved for markup written in this file. Never call it on anything that
    came from a model, a file, a form field or a supplier -- the point of the
    type is that these calls are few enough to read.
    """
    return Safe(markup)


def text(value: object) -> Safe:
    """Escape any value into safe markup."""
    return Safe(escape(str(value), quote=False))


def _attribute(name: str, value: object) -> str:
    # class_ -> class, data_id -> data-id. Python keywords and hyphens cannot
    # be written as keyword arguments any other way.
    key = name.rstrip("_").replace("_", "-")
    if value is True:
        return f" {escape(key, quote=True)}"
    if value is False or value is None:
        return ""
    return f' {escape(key, quote=True)}="{escape(str(value), quote=True)}"'


def tag(name: str, /, *children: object, **attributes: object) -> Safe:
    """Build one element. Every child that is not already ``Safe`` is escaped.

    The tag name is positional-only, because ``name`` is also an HTML attribute
    and every form field on the review page carries one. Leaving it an ordinary
    parameter made ``tag("input", name="approver")`` a TypeError rather than an
    input element.
    """
    rendered = "".join(_attribute(key, value) for key, value in attributes.items())
    if name in VOID:
        return Safe(f"<{name}{rendered}>")
    inner = "".join(
        child if isinstance(child, Safe) else escape(str(child), quote=False) for child in children
    )
    return Safe(f"<{name}{rendered}>{inner}</{name}>")


def join(children: object) -> Safe:
    """Concatenate a sequence, escaping anything not already safe."""
    return Safe(
        "".join(
            child if isinstance(child, Safe) else escape(str(child), quote=False)
            for child in children  # type: ignore[union-attr]
        )
    )


STYLE = """
:root { color-scheme: light dark; --line: #d6d8dd; --muted: #5b6068; --warn: #a2331b; }
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem 1.5rem 4rem; font: 15px/1.55 ui-sans-serif, system-ui, sans-serif;
       max-width: 62rem; margin-inline: auto; }
h1 { font-size: 1.35rem; margin: 0 0 .25rem; }
h2 { font-size: 1rem; margin: 1.75rem 0 .5rem; }
.sub { color: var(--muted); margin: 0 0 1.75rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .55rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-weight: 600; font-size: .8rem; letter-spacing: .02em; text-transform: uppercase;
     color: var(--muted); }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.tag { display: inline-block; padding: .1rem .45rem; border: 1px solid var(--line);
       border-radius: 3px; font-size: .78rem; }
.reject { color: var(--warn); border-color: var(--warn); }
.card { border: 1px solid var(--line); border-radius: 5px; padding: 1rem 1.15rem; margin: 1rem 0; }
.claim { margin: .35rem 0; }
.cite { color: var(--muted); font-size: .82rem; font-family: ui-monospace, monospace; }
form { display: flex; gap: .6rem; align-items: center; flex-wrap: wrap; margin-top: 1rem; }
input, select, button { font: inherit; padding: .35rem .5rem; }
button { cursor: pointer; }
.empty { color: var(--muted); padding: 2rem 0; }
.err { border: 1px solid var(--warn); color: var(--warn); padding: .8rem 1rem; border-radius: 5px; }
a { color: inherit; }
"""


def document(title: str, *children: object, refresh: int = 0) -> Safe:
    """Wrap content in a complete page.

    There is no script tag, and that is a security decision rather than
    minimalism. This page renders text a model wrote; a page that executes no
    script of its own gives an injected one nothing to hide among, and lets the
    response carry a content-security policy with no exceptions to carve out.

    Liveness costs nothing here either. ``refresh`` emits a meta refresh, which
    keeps a queue page current without a line of JavaScript. The SSE stream at
    ``/events`` exists for programs, which is what a stream is good for.
    """
    head = [
        tag("meta", charset="utf-8"),
        tag("meta", name="viewport", content="width=device-width, initial-scale=1"),
        tag("title", title),
        tag("style", raw(STYLE)),
    ]
    if refresh > 0:
        head.insert(2, tag("meta", http_equiv="refresh", content=str(refresh)))
    return Safe(
        "<!doctype html>"
        + tag(
            "html",
            tag("head", *head),
            tag("body", *children),
            lang="en",
        )
    )
