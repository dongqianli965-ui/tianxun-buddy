"""Bound offline converters by wall time, CPU, memory and captured output.

No shell is involved. Windows Job Objects also terminate child processes when
the job handle closes. Failure to establish the resource boundary is fatal.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import threading
from typing import Mapping, Sequence


class ProcessLimitError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class _WindowsJob:
    def __init__(self, process, memory_bytes, cpu_seconds):
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [('PerProcessUserTimeLimit', c.c_longlong), ('PerJobUserTimeLimit', c.c_longlong),
                        ('LimitFlags', w.DWORD), ('MinimumWorkingSetSize', c.c_size_t),
                        ('MaximumWorkingSetSize', c.c_size_t), ('ActiveProcessLimit', w.DWORD),
                        ('Affinity', c.c_size_t), ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(n, c.c_ulonglong) for n in ('ReadOperationCount', 'WriteOperationCount',
                'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]
        class Extended(c.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO), ('ProcessMemoryLimit', c.c_size_t),
                        ('JobMemoryLimit', c.c_size_t), ('PeakProcessMemoryUsed', c.c_size_t),
                        ('PeakJobMemoryUsed', c.c_size_t)]
        self.k = c.WinDLL('kernel32', use_last_error=True)
        self.k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.k.CreateJobObjectW.restype = w.HANDLE
        self.k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.k.SetInformationJobObject.restype = w.BOOL
        self.k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.k.AssignProcessToJobObject.restype = w.BOOL
        self.k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ProcessLimitError('RESOURCE_BOUNDARY_FAILED: CreateJobObject')
        limits = Extended()
        # KILL_ON_JOB_CLOSE | PROCESS_MEMORY | JOB_MEMORY | JOB_TIME | ACTIVE_PROCESS
        limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x100 | 0x200 | 0x4 | 0x8
        limits.BasicLimitInformation.PerJobUserTimeLimit = int(cpu_seconds * 10_000_000)
        limits.BasicLimitInformation.ActiveProcessLimit = 4
        limits.ProcessMemoryLimit = int(memory_bytes)
        limits.JobMemoryLimit = int(memory_bytes * 2)
        ok = self.k.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits))
        if not ok:
            self.close()
            raise ProcessLimitError('RESOURCE_BOUNDARY_FAILED: SetInformationJobObject')
        if not self.k.AssignProcessToJobObject(self.handle, int(process._handle)):
            # A very short command can already have exited; it has no running
            # process left to constrain. Any live assignment failure is closed.
            if process.poll() is None:
                self.close()
                raise ProcessLimitError('RESOURCE_BOUNDARY_FAILED: AssignProcessToJobObject')

    def terminate(self):
        if self.handle:
            self.k.TerminateJobObject(self.handle, 137)

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


def bounded_run(argv: Sequence[str], *, timeout: float = 30,
                memory_bytes: int = 256 * 1024 * 1024, cpu_seconds: float = 25,
                max_stdout: int = 16 * 1024 * 1024, max_stderr: int = 64 * 1024,
                env: Mapping[str, str] | None = None, cwd=None) -> ProcessResult:
    if timeout <= 0 or memory_bytes <= 0 or max_stdout < 0 or max_stderr < 0:
        raise ValueError('Invalid process resource limits')
    options = {}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    else:
        def set_limits():
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            seconds = max(1, int(cpu_seconds))
            resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
            resource.setrlimit(resource.RLIMIT_FSIZE, (max_stdout + max_stderr, max_stdout + max_stderr))
        options['preexec_fn'] = set_limits
    process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False, env=env, cwd=cwd, **options)
    job = None
    violations = []
    captured = [bytearray(), bytearray()]
    def terminate():
        if job is not None:
            job.terminate()
        try:
            process.kill()
        except OSError:
            pass
    def collect(stream, index, cap):
        while True:
            data = stream.read(65536)
            if not data:
                break
            remaining = cap - len(captured[index])
            captured[index].extend(data[:max(0, remaining)])
            if len(data) > remaining:
                violations.append('STDOUT_LIMIT' if index == 0 else 'STDERR_LIMIT')
                terminate()
                break
        stream.close()
    threads = []
    try:
        if os.name == 'nt':
            job = _WindowsJob(process, memory_bytes, cpu_seconds)
        for i, (stream, cap) in enumerate(((process.stdout, max_stdout), (process.stderr, max_stderr))):
            thread = threading.Thread(target=collect, args=(stream, i, cap), daemon=True)
            thread.start()
            threads.append(thread)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate()
            process.wait(timeout=5)
            raise ProcessLimitError(f'PROCESS_TIMEOUT: exceeded {timeout:g}s') from None
        for thread in threads:
            thread.join(timeout=5)
        if any(t.is_alive() for t in threads):
            terminate()
            raise ProcessLimitError('PROCESS_PIPE_TIMEOUT')
        if violations:
            raise ProcessLimitError(violations[0])
        return ProcessResult(process.returncode, bytes(captured[0]), bytes(captured[1]))
    finally:
        if process.poll() is None:
            terminate()
            process.wait(timeout=5)
        if job is not None:
            job.close()
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
