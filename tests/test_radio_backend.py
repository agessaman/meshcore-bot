import asyncio
import configparser
from pathlib import Path

from shared.radio_backend import BackendEventType
from shared.radio_backend.contacts import contacts_dict_from_iterable, normalize_contact_dict
from shared.radio_backend.pymc_core_backend import PyMcCoreBackend
from shared.radio_backend.results import BackendResult, is_error, is_ok, is_sent


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class _Bot:
    def __init__(self, root: Path):
        self.bot_root = root
        self.logger = _Logger()


class _Db:
    def __init__(self, root: Path):
        self.bot = _Bot(root)


def test_backend_result_type_compares_by_name():
    result = BackendResult.sent()

    assert is_sent(result)
    assert not is_error(result)
    assert result.type == BackendEventType.MSG_SENT

    err = BackendResult.error("no_event_received")
    assert is_error(err)
    assert not is_ok(err)


def test_contact_normalization_from_object():
    class Contact:
        public_key = bytes.fromhex("aa" * 32)
        name = "Node"
        adv_type = 1
        flags = 0
        out_path = b"\x01\x02"
        out_path_len = 2
        last_advert_timestamp = 123
        lastmod = 456
        gps_lat = 1.25
        gps_lon = -2.5

    normalized = normalize_contact_dict(Contact())

    assert normalized["public_key"] == "aa" * 32
    assert normalized["name"] == "Node"
    assert normalized["out_path"] == "0102"
    assert normalized["out_path_len"] == 2


def test_contacts_dict_from_iterable():
    contacts = contacts_dict_from_iterable(
        [
            {"public_key": "aa" * 32, "name": "A"},
            {"public_key": "bb" * 32, "adv_name": "B"},
        ]
    )

    assert sorted(contacts) == ["aa" * 32, "bb" * 32]
    assert contacts["bb" * 32]["name"] == "B"


def test_pymc_identity_file_created(tmp_path):
    config = configparser.ConfigParser()
    config["Connection"] = {"pymc_identity_file": "identity.key"}
    backend = PyMcCoreBackend(config, _Db(tmp_path), _Logger())

    seed = backend._load_or_create_identity_seed()

    assert len(seed) == 32
    assert (tmp_path / "identity.key").read_text(encoding="ascii") == seed.hex()
    assert backend._load_or_create_identity_seed() == seed


def test_pymc_event_type_normalization(tmp_path):
    config = configparser.ConfigParser()
    config["Connection"] = {}
    backend = PyMcCoreBackend(config, _Db(tmp_path), _Logger())
    seen = []

    async def cb(event, metadata=None):
        seen.append(event.payload["text"])

    backend.subscribe(BackendEventType.CONTACT_MSG_RECV, cb)

    asyncio.run(backend._emit(BackendEventType.CONTACT_MSG_RECV, {"text": "hello"}))

    assert seen == ["hello"]
