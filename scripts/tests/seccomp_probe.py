"""Independent, network-free kernel probe for the invitation seccomp boundary."""
import ctypes
import errno
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sandbox_seccomp import install_socket_policy, _architecture, METADATA_SYSCALLS, IO_URING_SYSCALLS, SECURITY_SYSCALLS, SENDTO_SYSCALLS

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
if libc.prctl(39, 0, 0, 0, 0) != 1:
    try:
        install_socket_policy()
    except RuntimeError:
        pass
    else:
        raise AssertionError("Installation must refuse missing no_new_privs")
fixture = Path(tempfile.mkdtemp(prefix="seccomp-probe-")) / "own-auth.json"
fixture.write_text("synthetic-credential")
assert libc.prctl(38, 1, 0, 0, 0) == 0
install_socket_policy()

denied = []
for label, domain, kind, protocol in (
    ("pathnameUnix", socket.AF_UNIX, socket.SOCK_STREAM, 0),
    ("abstractUnix", socket.AF_UNIX, socket.SOCK_DGRAM, 0),
    ("udp4", socket.AF_INET, socket.SOCK_DGRAM, 0),
    ("udp6", socket.AF_INET6, socket.SOCK_DGRAM, 0),
    ("raw", socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW),
    ("netlink", socket.AF_NETLINK, socket.SOCK_RAW, 0),
    ("packet", socket.AF_PACKET, socket.SOCK_RAW, 0),
    ("sctp", socket.AF_INET, socket.SOCK_STREAM, 132),
):
    try:
        socket.socket(domain, kind, protocol)
    except OSError as error:
        assert error.errno == errno.EPERM, (label, error.errno)
        denied.append(label)
    else:
        raise AssertionError(label + " transport escaped")

for domain in (socket.AF_INET, socket.AF_INET6):
    for flags in (0, socket.SOCK_CLOEXEC, socket.SOCK_NONBLOCK, socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK):
        with socket.socket(domain, socket.SOCK_STREAM | flags, socket.IPPROTO_TCP):
            pass
left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    left.sendall(b"internal-stream")
    assert right.recv(32) == b"internal-stream"
finally:
    left.close(); right.close()
try:
    socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
except OSError as error:
    assert error.errno == errno.EPERM
else:
    raise AssertionError("Unix datagram socketpair escaped")

machine, _ = _architecture()
for number, label in METADATA_SYSCALLS[machine].items():
    ctypes.set_errno(0)
    assert libc.syscall(ctypes.c_long(number), 0, 0, 0, 0, 0, 0) == -1, label
    assert ctypes.get_errno() == errno.EPERM, label
for number, label in SECURITY_SYSCALLS[machine].items():
    ctypes.set_errno(0)
    assert libc.syscall(ctypes.c_long(number), 0, 0, 0, 0, 0, 0) == -1, label
    assert ctypes.get_errno() == errno.EPERM, label
sendto_nr = SENDTO_SYSCALLS[machine]
for flags, pointer in ((0x20000000, 0), (0, 1), (0, 0x100000000)):
    ctypes.set_errno(0)
    assert libc.syscall(ctypes.c_long(sendto_nr), -1, 0, 0, ctypes.c_long(flags), ctypes.c_void_p(pointer), 16) == -1
    assert ctypes.get_errno() == errno.EPERM
ctypes.set_errno(0)
assert libc.syscall(ctypes.c_long(sendto_nr), -1, 0, 0, 0, 0, 0) == -1
assert ctypes.get_errno() == errno.EBADF  # Permitted policy; invalid fixture fd.
for number in IO_URING_SYSCALLS:
    ctypes.set_errno(0)
    assert libc.syscall(ctypes.c_long(number), 0, 0, 0, 0, 0, 0) == -1
    assert ctypes.get_errno() == errno.ENOSYS
before = fixture.stat()
for operation in (
    lambda: os.chmod(fixture, 0o777),
    lambda: os.chown(fixture, os.getuid(), os.getgid()),
    lambda: os.utime(fixture, (1, 1)),
    lambda: os.setxattr(fixture, "user.escape", b"unsafe"),
):
    try:
        operation()
    except OSError as error:
        assert error.errno == errno.EPERM
    else:
        raise AssertionError("Pathname metadata escaped")
after = fixture.stat()
assert before.st_mode == after.st_mode and before.st_mtime_ns == after.st_mtime_ns
with fixture.open("r+") as file:
    os.fchmod(file.fileno(), 0o600)
assert fixture.stat().st_mode & 0o777 == 0o600

result = subprocess.run([
    sys.executable, "-c",
    "import errno,os,socket;\ntry: socket.socket(socket.AF_UNIX)\nexcept OSError as e: assert e.errno==errno.EPERM\nelse: raise AssertionError('exec escaped')\nfor operation in (lambda:os.setpgid(0,0),os.setsid):\n try: operation()\n except OSError as e: assert e.errno==errno.EPERM\n else: raise AssertionError('process group escaped')\nprint('inherited')",
], capture_output=True, text=True, timeout=10)
assert result.returncode == 0 and result.stdout.strip() == "inherited", result.stderr
node = Path("/opt/smsinvit-bin/node")
if node.exists():
    result = subprocess.run([
        str(node), "-e",
        "const net=require('net');const s=net.createConnection('/tmp/nonexistent-probe.sock');s.on('error',e=>{if(e.code!=='EPERM')process.exit(3);console.log('node-denied')});",
    ], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == "node-denied", result.stderr

print(json.dumps({
    "architecture": platform.machine(), "status": "PASS", "deniedTransports": denied,
    "tcpSocketCreationAllowed": True, "internalStreamSocketpairAllowed": True,
    "pathnameMetadataDenied": list(METADATA_SYSCALLS[machine].values()),
    "ioUringDenied": True, "ownFdPermissionsAllowed": True,
    "brokerDescriptorAndMemoryAccessDenied": list(SECURITY_SYSCALLS[machine].values()),
    "tcpFastOpenDenied": True, "connectedSendtoAllowed": True,
    "forkExecInheritsPolicy": True, "nodeLibuvCompatible": node.exists(),
    "processGroupEscapeDenied": True,
    "noNetworkCalls": True,
}, sort_keys=True))
