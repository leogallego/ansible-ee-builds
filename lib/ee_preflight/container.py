from __future__ import annotations

import shutil
import subprocess


class ContainerRuntime:
    def __init__(self) -> None:
        self.cmd = self._detect()

    def _detect(self) -> str:
        for cmd in ("podman", "docker"):
            if shutil.which(cmd):
                return cmd
        raise RuntimeError(
            "No container runtime found. Install podman or docker for --container-test"
        )

    def pull(self, image: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.cmd, "pull", image],
            capture_output=True,
            text=True,
            timeout=300,
        )

    def run(
        self, image: str, command: str, timeout: int = 300
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.cmd, "run", "--rm", image, "sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    @property
    def available(self) -> bool:
        try:
            self._detect()
            return True
        except RuntimeError:
            return False
