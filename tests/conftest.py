import os
import socket

import pytest

os.environ.setdefault("SECRET_ACMEHR_PASSWORD", "ops-pass-123")
os.environ.setdefault("ACMEHR_PASSWORD", "ops-pass-123")

from centralign.config import Settings  # noqa: E402
from centralign.demo import prepare_demo, reset_acmehr, reset_files  # noqa: E402
from centralign.runtime import Runtime  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def settings(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    s = Settings.load(home=home, acmehr_url=f"http://127.0.0.1:{_free_port()}", brain="scripted")
    proc = prepare_demo(s)
    yield s
    if proc:
        proc.terminate()


@pytest.fixture
def fresh(settings):
    """A pristine company: seeded drive, seeded AcmeHR, no injected failures."""
    reset_files(settings)
    reset_acmehr(settings)
    return settings


@pytest.fixture
def rt(fresh):
    return Runtime(fresh)


def approve_all(rt, run_id, iv):
    rt.resolve(iv["id"], approved=True, note="") if iv["kind"] == "approval" else rt.resolve(iv["id"], answer="")


def human(approve=True, answers=None, deny_tools=()):
    """Scriptable human for on_wait: approves/denies approvals, answers questions in order."""
    answers = list(answers or [])

    def on_wait(rt, run_id, iv):
        if iv["kind"] == "approval":
            tool = iv["payload"]["action"]["tool"]
            ok = approve and tool not in deny_tools
            rt.resolve(iv["id"], approved=ok, note="" if ok else "not today")
        else:
            rt.resolve(iv["id"], answer=answers.pop(0) if answers else "stop")

    return on_wait
