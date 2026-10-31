"""Shared test helpers: path setup and a fake subprocess.Popen."""

import io
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(ROOT, "addon", "globalPlugins", "askGPT")
if PACKAGE_DIR not in sys.path:
	sys.path.insert(0, PACKAGE_DIR)


class CapturingBytesIO(io.BytesIO):
	"""BytesIO that remembers its content when closed (stdin of the fake process)."""

	captured = b""

	def close(self):
		self.captured = self.getvalue()
		super().close()


class FakeProcess:
	def __init__(self, argv, stdout=b"", stderr=b"", returncode=0, outputText=None, hang=False):
		self.argv = argv
		self.pid = 4242
		self._handle = 777  # Windows process handle, used by Job Objects.
		self.stdin = CapturingBytesIO()
		self.stdout = io.BytesIO(stdout)
		self.stderr = io.BytesIO(stderr)
		self._finalReturnCode = returncode
		self.returncode = None if hang else returncode
		self.hang = hang
		self.killed = False
		if outputText is not None and isinstance(argv, list) and "-o" in argv:
			with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as f:
				f.write(outputText)

	def wait(self, timeout=None):
		if self.hang and not self.killed:
			time.sleep(min(timeout or 0.05, 0.05))
			raise subprocess.TimeoutExpired(self.argv, timeout)
		return self.returncode

	def poll(self):
		return None if (self.hang and not self.killed) else self.returncode

	def kill(self):
		self.killed = True
		self.returncode = -9


class FakePopen:
	"""Callable standing in for subprocess.Popen; records every call."""

	def __init__(self, **processOptions):
		self.processOptions = processOptions
		self.calls = []
		self.processes = []

	def __call__(self, argv, **kwargs):
		self.calls.append((argv, kwargs))
		process = FakeProcess(argv, **self.processOptions)
		self.processes.append(process)
		return process

	@property
	def lastProcess(self):
		return self.processes[-1]

	@property
	def lastArgv(self):
		return self.calls[-1][0]


class FakeJob:
	"""Stands in for codexClient.WindowsJob."""

	instances = []

	def __init__(self, assignResult=True, terminateResult=True):
		self.assignResult = assignResult
		self.terminateResult = terminateResult
		self.assigned = []
		self.terminated = False
		self.closed = False
		FakeJob.instances.append(self)

	def assign(self, proc):
		self.assigned.append(proc)
		return self.assignResult

	def terminate(self):
		self.terminated = True
		if self.terminateResult:
			for proc in self.assigned:
				proc.kill()
		return self.terminateResult

	def close(self):
		self.closed = True
