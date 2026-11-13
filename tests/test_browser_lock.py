import os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import pytest
import browser_lock as bl


def kw(tmp_path, **extra):
    return dict(lock_dir=str(tmp_path / "lk"), log_file=str(tmp_path / "log"), **extra)


def test_acquire_release_and_not_owner(tmp_path):
    assert bl.acquire("A", **kw(tmp_path))
    assert not bl.release("B", **kw(tmp_path))
    assert bl.release("A", **kw(tmp_path))
    assert not os.path.exists(tmp_path / "lk")


def test_timeout(tmp_path):
    bl.acquire("A", **kw(tmp_path))
    with pytest.raises(bl.LockTimeout):
        bl.acquire("B", **kw(tmp_path, poll_s=0.01, wait_s=0.05))


def test_stale_broken(tmp_path):
    bl.acquire("A", **kw(tmp_path))
    assert bl.acquire("B", **kw(tmp_path, stale_s=-1))
    assert "STALE" in open(tmp_path / "log").read()


def test_serializes(tmp_path):
    order = []
    def run(r):
        with bl.held(r, **kw(tmp_path, poll_s=0.01)):
            order.append(r + "+"); time.sleep(0.05); order.append(r + "-")
    ts = [threading.Thread(target=run, args=(r,)) for r in "XY"]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert order[0][0] == order[1][0] and order[2][0] == order[3][0]
