from datetime import time

import pytest

from aus_cart_mcp.watch.policy import Policy, in_window


def test_defaults_and_overrides(store):
    assert Policy.load(store) == Policy()
    Policy.save(store, refresh_times="06:00,12:00", discount_threshold="0.25", jitter_minutes="5")
    store.set_setting("policy.max_searches_per_run", "not a number")  # ignored on load
    policy = Policy.load(store)
    assert policy.refresh_clocks == [time(6), time(12)]
    assert policy.discount_threshold == 0.25 and policy.jitter_minutes == 5
    assert policy.max_searches_per_run == Policy().max_searches_per_run
    assert policy.clock("digest_time") == time(7, 30)


@pytest.mark.parametrize(
    "values",
    [{"nope": "1"}, {"jitter_minutes": "x"}, {"quiet_start": "25:99x"}, {"refresh_times": "06:00,lunch"}],
)
def test_bad_values_are_refused(store, values):
    with pytest.raises(ValueError):
        Policy.save(store, **values)


def test_windows_across_midnight():
    assert in_window(time(23, 30), time(23), time(5))
    assert in_window(time(4, 59), time(23), time(5))
    assert not in_window(time(5), time(23), time(5))
    assert in_window(time(12), time(9), time(17))
    assert not in_window(time(17), time(9), time(17))
