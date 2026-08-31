def test_context_phone_is_normalized_for_the_send_path() -> None:
    # resolve() normalizes what it PROBES on; context is a separate copy,
    # and it is what the call and send nodes actually dial. Unnormalized,
    # identity would resolve to +919876543210 while the node dialled the
    # bare form — and a suppression stored in E.164 would not match it.
    from app.crm.outreach.entry import _phone_from_payload

    assert (
        _phone_from_payload({"customer_mobile_number": "9876543210"}) == "+919876543210"
    )
    assert _phone_from_payload({"phone": "+91 98765 43210"}) == "+919876543210"
    assert (
        _phone_from_payload({"customer": {"phone": "09876543210"}}) == "+919876543210"
    )
    # Unparseable is handed through rather than dropped: a node that cannot
    # use it parks with a clear reason, which beats losing the number here.
    assert _phone_from_payload({"phone": "n/a"}) == "n/a"
    assert _phone_from_payload({}) is None
