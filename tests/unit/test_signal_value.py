"""_signal_value must reproduce the if/elif chains it replaced, for every key layout."""

import itertools

import pytest

from modules.message_handler import _signal_value

DM_SNR = (("SNR", "snr", "signal_to_noise", "signal_noise_ratio"), ("snr", "SNR"), float)
CHANNEL_SNR = (("SNR", "snr"), ("snr", "SNR"), float)
RSSI = (("RSSI", "rssi", "signal_strength"), ("rssi", "RSSI"), int)


def _original_chain(payload, metadata, payload_keys, metadata_keys, convert):
    # The removed code: elif over payload keys, then `elif metadata:` with an
    # inner if/elif over metadata keys; a present None stops the search.
    for key in payload_keys:
        if key in payload:
            raw = payload.get(key)
            return convert(raw) if raw is not None else None
    if metadata:
        for key in metadata_keys:
            if key in metadata:
                raw = metadata.get(key)
                return convert(raw) if raw is not None else None
    return None


def _layouts(keys):
    # Each key absent, present as None, or present with a value.
    for states in itertools.product((None, "none", "val"), repeat=len(keys)):
        d = {}
        for i, (key, state) in enumerate(zip(keys, states, strict=True)):
            if state == "none":
                d[key] = None
            elif state == "val":
                d[key] = f"{-5 - i}"
        yield d


@pytest.mark.parametrize("spec", [DM_SNR, CHANNEL_SNR, RSSI])
def test_matches_original_chain(spec):
    payload_keys, metadata_keys, convert = spec
    for payload in _layouts(payload_keys):
        for metadata in [None, {}, *_layouts(metadata_keys)]:
            assert _signal_value(payload, metadata, payload_keys, metadata_keys, convert) == _original_chain(
                payload, metadata, payload_keys, metadata_keys, convert
            ), (payload, metadata)
