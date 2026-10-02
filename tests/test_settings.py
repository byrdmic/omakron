"""settings.json is validated when the service starts."""

import json

import pytest

from omakron.service import Settings


def test_queue_max_wait_defaults_to_four_hours_and_accepts_a_number(tmp_path):
    assert Settings.load(tmp_path).queue_max_wait_s == 14400.0
    (tmp_path / "settings.json").write_text(json.dumps({"queue_max_wait_s": 7200}))
    assert Settings.load(tmp_path).queue_max_wait_s == 7200.0


@pytest.mark.parametrize("value", [0, -1, "3600", True, None])
def test_bad_queue_max_wait_is_rejected_at_load(tmp_path, value):
    (tmp_path / "settings.json").write_text(json.dumps({"queue_max_wait_s": value}))
    with pytest.raises(ValueError, match="queue_max_wait_s must be a positive number"):
        Settings.load(tmp_path)
