"""Policy semantics independently interpreted for both supported syscall ABIs."""
import importlib.util
from pathlib import Path
import struct
import sys

import pytest

path = Path(__file__).resolve().parents[3] / "scripts/sandbox_seccomp.py"
spec = importlib.util.spec_from_file_location("invitation_socket_policy", path)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def decision(machine, number, args=(), arch=None):
    architecture = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}[machine]
    data = struct.pack("<iIQ6Q", number, arch or architecture, 0, *(list(args) + [0] * (6 - len(args))))
    instructions = policy.build_filter(machine)
    pc = accumulator = 0
    for _ in range(1000):
        instruction = instructions[pc]
        if instruction.code == 0x20:
            accumulator = struct.unpack_from("<I", data, instruction.k)[0]
        elif instruction.code == 0x54:
            accumulator &= instruction.k
        elif instruction.code in (0x15, 0x35):
            matched = accumulator == instruction.k if instruction.code == 0x15 else accumulator >= instruction.k
            pc += instruction.jt if matched else instruction.jf
        elif instruction.code == 0x06:
            return instruction.k
        else:
            raise AssertionError("Unexpected classic BPF instruction")
        pc += 1
    raise AssertionError("BPF did not terminate")


@pytest.mark.parametrize("machine,socket_nr,pair_nr", [("x86_64", 41, 53), ("aarch64", 198, 199)])
def test_only_tcp_and_internal_unix_stream_are_permitted(machine, socket_nr, pair_nr):
    for domain in (2, 10):
        for flags in (0, 0x800, 0x80000, 0x80800):
            for protocol in (0, 6):
                assert decision(machine, socket_nr, (domain, 1 | flags, protocol)) == 0x7FFF0000
    for domain in (1, 16, 17, 0, 40):
        assert decision(machine, socket_nr, (domain, 1, 0)) == 0x00050001
    for kind in (2, 3, 5, 2 | 0x80800, 1 | 0x100000):
        assert decision(machine, socket_nr, (2, kind, 0)) == 0x00050001
    for protocol in (17, 132, 255):
        assert decision(machine, socket_nr, (2, 1, protocol)) == 0x00050001
    assert decision(machine, pair_nr, (1, 1 | 0x80800, 0)) == 0x7FFF0000
    for args in ((1, 2, 0), (1, 1, 6), (2, 1, 0), (10, 1, 0)):
        assert decision(machine, pair_nr, args) == 0x00050001


@pytest.mark.parametrize("machine,numbers,fchmod_nr", [
    ("x86_64", (90, 93, 188, 199, 260, 261, 268, 280, 452, 463, 466, 469), 91),
    ("aarch64", (5, 7, 14, 16, 53, 54, 55, 88, 452, 463, 466, 469), 52),
])
def test_metadata_and_uring_cannot_bypass_transport_and_file_policy(machine, numbers, fchmod_nr):
    for number in numbers:
        assert decision(machine, number) == 0x00050001
    for number in (425, 426, 427):
        assert decision(machine, number) == 0x00050026
    assert decision(machine, fchmod_nr) == 0x7FFF0000
    assert decision(machine, 0) == 0x7FFF0000


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_foreign_architecture_is_killed_even_for_otherwise_allowed_syscalls(machine):
    for arch in (0x40000003, 0x40000028):
        assert decision(machine, 0, arch=arch) == 0x80000000


def test_x32_cannot_use_native_or_compat_syscall_number_spaces():
    for number in (0x40000000, 0x40000029, 0x40000200):
        assert decision("x86_64", number) == 0x80000000


@pytest.mark.parametrize("machine,sendto_nr,numbers", [
    ("x86_64", 44, (46, 101, 109, 112, 307, 310, 311, 438)),
    ("aarch64", 206, (117, 154, 157, 211, 269, 270, 271, 438)),
])
def test_task_cannot_steal_broker_fds_or_use_tcp_fast_open(machine, sendto_nr, numbers):
    for number in numbers:
        assert decision(machine, number) == 0x00050001
    for pointer in (1, 0x100000000, 0xFFFFFFFFFFFFFFFF):
        assert decision(machine, sendto_nr, (3, 0, 0, 0, pointer, 16)) == 0x00050001
    for flags in (0x20000000, 0x20004000):
        assert decision(machine, sendto_nr, (3, 0, 0, flags, 0, 0)) == 0x00050001
    assert decision(machine, sendto_nr, (3, 0, 0, 0x4000, 0, 0)) == 0x7FFF0000


def test_unsupported_platform_and_architecture_fail_closed(monkeypatch):
    for machine in ("i686", "armv7l", "ppc64le"):
        with pytest.raises(RuntimeError):
            policy.build_filter(machine)
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(RuntimeError):
        policy.install_socket_policy()
