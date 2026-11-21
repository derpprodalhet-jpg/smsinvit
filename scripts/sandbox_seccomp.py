"""Mandatory socket restrictions for the invitation's outer Linux sandbox.

Landlock controls TCP ports, but does not prohibit UDP or pathname Unix sockets
on every supported ABI. This filter closes those transports before any untrusted
CLI/SDK code runs. Pathname metadata changes are also denied because Landlock
does not mediate them; fchmod on an already permitted regular file stays usable.
It does not filter destination IPs or authenticate TLS peers.
No native library, privilege, shell, or less restrictive fallback is required.
"""
import ctypes
import errno
import platform
import sys


BPF_LD_W_ABS = 0x20
BPF_JMP_JEQ_K = 0x15
BPF_JMP_JGE_K = 0x35
BPF_ALU_AND_K = 0x54
BPF_RET_K = 0x06
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
LINUX_EPERM = 1
LINUX_ENOSYS = 38
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_FILTER_FLAG_TSYNC = 1
PR_GET_NO_NEW_PRIVS = 39
SOCK_STREAM = 1
SOCK_NONBLOCK = 0x800
SOCK_CLOEXEC = 0x80000
AF_UNIX = 1
AF_INET = 2
AF_INET6 = 10
IPPROTO_TCP = 6

# audit architecture, socket, socketpair, seccomp syscall numbers. Unsupported
# architecture/compatibility ABIs are killed rather than interpreting a foreign
# syscall number through this architecture's allow path.
ARCHITECTURES = {
    "x86_64": (0xC000003E, 41, 53, 317),
    "aarch64": (0xC00000B7, 198, 199, 277),
}
ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}
# Keep synchronized with the Linux syscall tables, including the new *xattrat
# and file_setattr interfaces. fchmod is deliberately absent: acquiring an
# outside regular file is denied by Landlock, and fchmod rejects O_PATH handles.
# https://github.com/torvalds/linux/blob/master/arch/x86/entry/syscalls/syscall_64.tbl
# https://github.com/torvalds/linux/blob/master/include/uapi/asm-generic/unistd.h
METADATA_SYSCALLS = {
    "x86_64": {
        90: "chmod", 92: "chown", 93: "fchown", 94: "lchown",
        132: "utime", 188: "setxattr", 189: "lsetxattr", 190: "fsetxattr",
        197: "removexattr", 198: "lremovexattr", 199: "fremovexattr",
        235: "utimes", 260: "fchownat", 261: "futimesat", 268: "fchmodat",
        280: "utimensat", 452: "fchmodat2", 463: "setxattrat",
        466: "removexattrat", 469: "file_setattr",
    },
    "aarch64": {
        5: "setxattr", 6: "lsetxattr", 7: "fsetxattr", 14: "removexattr",
        15: "lremovexattr", 16: "fremovexattr", 53: "fchmodat",
        54: "fchownat", 55: "fchown", 88: "utimensat", 452: "fchmodat2",
        463: "setxattrat", 466: "removexattrat", 469: "file_setattr",
    },
}
IO_URING_SYSCALLS = (425, 426, 427)
# A task must neither obtain its trusted network broker's descriptors/memory nor
# create a connection through sendmsg/sendmmsg TCP Fast Open without connect.
SECURITY_SYSCALLS = {
    "x86_64": {46: "sendmsg", 101: "ptrace", 109: "setpgid", 112: "setsid", 307: "sendmmsg", 310: "process_vm_readv", 311: "process_vm_writev", 438: "pidfd_getfd"},
    "aarch64": {117: "ptrace", 154: "setpgid", 157: "setsid", 211: "sendmsg", 269: "sendmmsg", 270: "process_vm_readv", 271: "process_vm_writev", 438: "pidfd_getfd"},
}
SENDTO_SYSCALLS = {"x86_64": 44, "aarch64": 206}
MSG_FASTOPEN = 0x20000000


class SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32),
    ]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]


class _Program:
    def __init__(self):
        self.instructions = []
        self.labels = {}

    def label(self, name):
        if name in self.labels:
            raise ValueError("Duplicate socket policy label")
        self.labels[name] = len(self.instructions)

    def emit(self, code, k, yes=None, no=None):
        self.instructions.append((code, k, yes, no))

    def build(self):
        result = []
        for offset, (code, k, yes, no) in enumerate(self.instructions):
            jumps = []
            for label in (yes, no):
                jump = 0 if label is None else self.labels[label] - offset - 1
                if not 0 <= jump <= 255:
                    raise ValueError("Invalid socket policy branch")
                jumps.append(jump)
            result.append(SockFilter(code, jumps[0], jumps[1], k))
        return result


def _architecture(machine=None):
    machine = machine or platform.machine()
    machine = ALIASES.get(machine, machine)
    if machine not in ARCHITECTURES:
        raise RuntimeError("Invitation seccomp requires a verified 64-bit architecture")
    return machine, ARCHITECTURES[machine]


def build_filter(machine=None):
    """Return a small classic-BPF program; useful for independent policy tests."""
    machine, (audit_arch, socket_nr, socketpair_nr, _) = _architecture(machine)
    program = _Program()
    program.emit(BPF_LD_W_ABS, 4)  # struct seccomp_data.arch
    program.emit(BPF_JMP_JEQ_K, audit_arch, "architecture_ok", "kill")
    program.label("kill")
    program.emit(BPF_RET_K, SECCOMP_RET_KILL_PROCESS)
    program.label("architecture_ok")
    program.emit(BPF_LD_W_ABS, 0)  # struct seccomp_data.nr
    if machine == "x86_64":
        # x32 shares AUDIT_ARCH_X86_64 but has a distinct syscall-number ABI.
        program.emit(BPF_JMP_JGE_K, 0x40000000, "kill_x32", "native_nr")
        program.label("kill_x32")
        program.emit(BPF_RET_K, SECCOMP_RET_KILL_PROCESS)
        program.label("native_nr")
    program.emit(BPF_JMP_JEQ_K, socket_nr, "socket", "check_socketpair")
    program.label("check_socketpair")
    program.emit(BPF_JMP_JEQ_K, socketpair_nr, "socketpair", "check_metadata")
    program.label("check_metadata")
    for number in sorted(set(METADATA_SYSCALLS[machine]) | set(SECURITY_SYSCALLS[machine])):
        program.emit(BPF_JMP_JEQ_K, number, "deny")
    # io_uring operations do not pass through the corresponding socket/chmod
    # syscall filter. Disabling the complete interface avoids an alternate
    # transport and metadata mutation route. ENOSYS lets libuv use normal I/O.
    for number in IO_URING_SYSCALLS:
        program.emit(BPF_JMP_JEQ_K, number, "deny_io_uring")
    program.emit(BPF_JMP_JEQ_K, SENDTO_SYSCALLS[machine], "sendto", "allow_other")
    program.label("allow_other")
    program.emit(BPF_RET_K, SECCOMP_RET_ALLOW)

    program.label("socket")
    program.emit(BPF_LD_W_ABS, 16)  # args[0]: domain (kernel casts to int)
    program.emit(BPF_JMP_JEQ_K, AF_INET, "socket_type", "check_inet6")
    program.label("check_inet6")
    program.emit(BPF_JMP_JEQ_K, AF_INET6, "socket_type", "deny")
    program.label("socket_type")
    program.emit(BPF_LD_W_ABS, 24)  # args[1]: type, possibly NONBLOCK/CLOEXEC
    program.emit(BPF_ALU_AND_K, 0xFFFFFFFF ^ (SOCK_NONBLOCK | SOCK_CLOEXEC))
    program.emit(BPF_JMP_JEQ_K, SOCK_STREAM, "socket_protocol", "deny")
    program.label("socket_protocol")
    program.emit(BPF_LD_W_ABS, 32)  # args[2]: protocol
    program.emit(BPF_JMP_JEQ_K, 0, "allow_socket", "check_tcp")
    program.label("check_tcp")
    program.emit(BPF_JMP_JEQ_K, IPPROTO_TCP, "allow_socket", "deny")

    program.label("socketpair")
    program.emit(BPF_LD_W_ABS, 16)
    program.emit(BPF_JMP_JEQ_K, AF_UNIX, "socketpair_type", "deny")
    program.label("socketpair_type")
    program.emit(BPF_LD_W_ABS, 24)
    program.emit(BPF_ALU_AND_K, 0xFFFFFFFF ^ (SOCK_NONBLOCK | SOCK_CLOEXEC))
    program.emit(BPF_JMP_JEQ_K, SOCK_STREAM, "socketpair_protocol", "deny")
    program.label("socketpair_protocol")
    program.emit(BPF_LD_W_ABS, 32)
    program.emit(BPF_JMP_JEQ_K, 0, "allow_socket", "deny")

    program.label("sendto")
    # A null 64-bit destination pointer is required, including its high word.
    # Otherwise TCP Fast Open could connect without triggering the broker.
    program.emit(BPF_LD_W_ABS, 48)  # args[4], low word of destination pointer
    program.emit(BPF_JMP_JEQ_K, 0, "sendto_pointer_high", "deny")
    program.label("sendto_pointer_high")
    program.emit(BPF_LD_W_ABS, 52)
    program.emit(BPF_JMP_JEQ_K, 0, "sendto_flags", "deny")
    program.label("sendto_flags")
    program.emit(BPF_LD_W_ABS, 40)  # args[3]: flags
    program.emit(BPF_ALU_AND_K, MSG_FASTOPEN)
    program.emit(BPF_JMP_JEQ_K, 0, "allow_socket", "deny")

    program.label("deny")
    program.emit(BPF_RET_K, SECCOMP_RET_ERRNO | LINUX_EPERM)
    program.label("deny_io_uring")
    program.emit(BPF_RET_K, SECCOMP_RET_ERRNO | LINUX_ENOSYS)
    program.label("allow_socket")
    program.emit(BPF_RET_K, SECCOMP_RET_ALLOW)
    return program.build()


def install_socket_policy():
    """Install the inherited filter, or raise before untrusted execution starts.

The caller must already set no_new_privs. TSYNC applies the policy to every
existing thread; its nonzero failure TID must be treated as a failed install.
"""
    if sys.platform != "linux":
        raise RuntimeError("Invitation seccomp requires Linux")
    _, (_, _, _, seccomp_nr) = _architecture()
    instructions = build_filter()
    array = (SockFilter * len(instructions))(*instructions)
    program = SockFprog(len(instructions), array)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise RuntimeError("Invitation seccomp requires no_new_privs first")
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(
        ctypes.c_long(seccomp_nr), ctypes.c_long(SECCOMP_SET_MODE_FILTER),
        ctypes.c_long(SECCOMP_FILTER_FLAG_TSYNC), ctypes.byref(program),
    )
    if result != 0:
        code = ctypes.get_errno() if result < 0 else errno.EPERM
        raise OSError(code, "Kernel rejected mandatory invitation socket policy")
