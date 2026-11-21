"""Whom a limit counts as one client."""

import throttle


def test_an_ipv6_client_is_its_slash_64():
    one = throttle.one_client("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    assert one == throttle.one_client("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert one != throttle.one_client("2001:db8:1:3::1")


def test_other_addresses_are_counted_as_given():
    assert throttle.one_client("203.0.113.7") == "203.0.113.7"
    assert throttle.one_client("::ffff:203.0.113.7") == "::ffff:203.0.113.7"
    assert throttle.one_client("unknown") == "unknown"
