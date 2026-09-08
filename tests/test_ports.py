import socket

import pytest

from launcher import config, db, ports


@pytest.fixture
def store(data_dir, monkeypatch):
    monkeypatch.setattr(config, "PORT_RANGE_START", 21000)
    monkeypatch.setattr(config, "PORT_RANGE_END", 21009)
    db.init()
    return data_dir


def test_allocates_within_range(store):
    app_id = db.create_app("alpha")
    port = ports.allocate(app_id)
    assert config.PORT_RANGE_START <= port <= config.PORT_RANGE_END


def test_port_is_stable_across_redeploys(store):
    app_id = db.create_app("alpha")
    assert ports.allocate(app_id) == ports.allocate(app_id)


def test_apps_never_share_a_port(store):
    assigned = {ports.allocate(db.create_app(f"app{i}")) for i in range(8)}
    assert len(assigned) == 8


def test_skips_ports_held_by_other_processes(store, monkeypatch):
    monkeypatch.setattr(config, "PORT_RANGE_START", 21100)
    monkeypatch.setattr(config, "PORT_RANGE_END", 21101)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("0.0.0.0", 21100))
    blocker.listen(1)
    try:
        assert ports.allocate(db.create_app("alpha")) == 21101
    finally:
        blocker.close()


def test_raises_when_range_exhausted(store):
    for i in range(10):
        ports.allocate(db.create_app(f"app{i}"))
    with pytest.raises(ports.NoPortsAvailable):
        ports.allocate(db.create_app("one-too-many"))


def test_release_frees_the_port(store):
    app_id = db.create_app("alpha")
    port = ports.allocate(app_id)
    ports.release(app_id)
    assert ports.allocated_port(app_id) is None
    assert port not in ports.in_use()


def test_public_and_backend_ports_come_from_different_ranges(store, monkeypatch):
    monkeypatch.setattr(config, "BACKEND_PORT_START", 31000)
    monkeypatch.setattr(config, "BACKEND_PORT_END", 31009)
    app_id = db.create_app("alpha")

    public = ports.allocate(app_id, ports.PUBLIC)
    backend = ports.allocate(app_id, ports.BACKEND)

    assert config.PORT_RANGE_START <= public <= config.PORT_RANGE_END
    assert 31000 <= backend <= 31009
    assert public != backend


def test_both_roles_are_stable_across_restarts(store, monkeypatch):
    monkeypatch.setattr(config, "BACKEND_PORT_START", 31100)
    monkeypatch.setattr(config, "BACKEND_PORT_END", 31109)
    app_id = db.create_app("alpha")

    assert ports.allocate(app_id, ports.PUBLIC) == ports.allocate(app_id, ports.PUBLIC)
    assert ports.allocate(app_id, ports.BACKEND) == ports.allocate(app_id, ports.BACKEND)


def test_release_frees_every_role(store, monkeypatch):
    monkeypatch.setattr(config, "BACKEND_PORT_START", 31200)
    monkeypatch.setattr(config, "BACKEND_PORT_END", 31209)
    app_id = db.create_app("alpha")
    ports.allocate(app_id, ports.PUBLIC)
    ports.allocate(app_id, ports.BACKEND)

    ports.release(app_id)

    assert ports.allocated_port(app_id, ports.PUBLIC) is None
    assert ports.allocated_port(app_id, ports.BACKEND) is None
