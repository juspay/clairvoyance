"""What can go wrong on the way out.

Every one of these is a refusal — no request is made — but callers need to tell
them apart: a blocked address is permanent and must not be retried, a resolver
failure is transient and should be, and a body-dropping redirect is neither (the
destination is fine, the delivery is not).

EgressResolutionError and RedirectDropsBodyError subclass SSRFError so a caller
that only wants "did egress fail" keeps working with one `except`. A caller that
can act on the difference reads ``retryable`` rather than catching the specific
type, so adding a fourth refusal does not silently fall into the wrong branch of
an existing handler.

``outward`` and ``retryable`` live on the class because every call site needed
exactly those two answers and nothing else. str(exc) names the address the host
resolved to — right in a log, an SSRF oracle anywhere else, since a caller can
probe hostnames and read the internal network off the refusals. So the rule is:
log the exception, hand outward the ``outward``.
"""


class SSRFError(ValueError):
    """Raised when a URL fails SSRF egress validation."""

    #: Safe to show a model, a merchant, anyone outside. Never the address.
    outward = "Request blocked by egress policy"
    #: Whether trying the same request again could plausibly succeed.
    retryable = False


class RedirectDropsBodyError(SSRFError):
    """A redirect would discard the request body.

    301/302/303 turn a POST into a bodyless GET by the HTTP spec. For a caller
    whose body IS the delivery — a signed webhook — following that reaches the
    endpoint with nothing in it, and a 200 to that empty GET looks like success.
    """

    outward = "The destination redirects; configure its final URL"
    retryable = False


class EgressResolutionError(SSRFError):
    """The host could not be resolved — a transient failure, not a refusal.

    A resolver hiccup is worth retrying, whereas a blocked address never is,
    and collapsing the two makes a short DNS outage look like policy refused
    the request.
    """

    outward = "Could not reach the server, try again"
    retryable = True
