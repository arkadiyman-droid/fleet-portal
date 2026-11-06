from app import despike


def test_isolated_spike_removed():
    # the Spruce St. mix glitch: one 250 among a steady ~120 band
    vals = [120, 121, 119, 120, 250, 118, 120, 122]
    assert despike.clean(vals) == [120, 121, 119, 120, None, 118, 120, 122]
    assert despike.drop(vals) == [120, 121, 119, 120, 118, 120, 122]


def test_sustained_change_kept():
    # a boiler firing: supply climbs and stays up — not a spike
    vals = [70, 72, 110, 138, 140, 139]
    assert despike.clean(vals) == vals


def test_two_in_a_row_kept():
    # two wild reads in a row are not an isolated sample; neighbours disagree
    vals = [120, 250, 250, 120]
    assert despike.clean(vals) == vals


def test_small_wiggle_kept():
    vals = [120, 124, 119, 126, 121]
    assert despike.clean(vals) == vals


def test_endpoints_never_removed():
    assert despike.clean([250, 120, 121]) == [250, 120, 121]
    assert despike.clean([120, 121, 250]) == [120, 121, 250]


def test_spike_with_gaps():
    # None entries (missing reads) are skipped when finding neighbours
    vals = [120, None, 250, None, 121]
    assert despike.clean(vals) == [120, None, None, None, 121]


def test_empty_and_allnone():
    assert despike.clean([]) == []
    assert despike.clean([None, None]) == [None, None]
    assert despike.drop([None, None]) == []


def test_fill_carries_previous_value():
    # the spike becomes the previous good value, not a gap
    assert despike.fill([120, 121, 250, 118, 120]) == [120, 121, 121, 118, 120]


def test_fill_leaves_real_gaps_and_keeps_good_data():
    assert despike.fill([120, None, 121, 250, 122]) == [120, None, 121, 121, 122]
    assert despike.fill([70, 72, 110, 138, 140]) == [70, 72, 110, 138, 140]
