"""터미널 PATH 밖의 앱 CLI 발견과 갱신 시 버전 재선택."""
import asyncio

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duet.core import clis, models


class CliResolutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.terminal = self.root / "bin/codex"
        self.bundled = self.root / "Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
        for p in (self.terminal, self.bundled):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)
        self.versions = {str(self.terminal): (0, 156, 1), str(self.bundled): (0, 159, 2)}
        env = dict(os.environ, PATH=str(self.terminal.parent))
        env.pop("DUET_CODEX_PATH", None)
        for p in (
            patch.dict(os.environ, env, clear=True),
            patch.object(clis, "EXTRA_DIRS", []),
            patch.object(clis, "HOME", self.root),
            patch.object(clis, "MAC_APP_ROOTS", [self.root / "Applications"]),
            patch.object(clis.sys, "platform", "darwin"),
            patch.object(clis, "WINDOWS", False),
            patch.object(clis, "_version_of", lambda path: (self.versions[path], str(self.versions[path]))),
        ):
            p.start()
            self.addCleanup(p.stop)
        clis.resolve.cache_clear()
        self.addCleanup(clis.resolve.cache_clear)

    def test_newest_bundle_without_app_in_path(self):
        self.assertEqual(set(clis.candidates("codex")), {str(self.terminal), str(self.bundled)})
        self.assertEqual(clis.which("codex"), str(self.bundled))

    def test_newer_terminal_and_explicit_override(self):
        self.versions[str(self.terminal)] = (0, 160, 0)
        self.assertEqual(clis.which("codex"), str(self.terminal))
        with patch.dict(os.environ, DUET_CODEX_PATH=str(self.bundled)):
            self.assertEqual(clis.which("codex"), str(self.bundled))

    def test_bundles_are_codex_only_and_macos_only(self):
        # Claude SDK 자체 번들은 설치 환경에 따라 후보에 포함될 수 있다.
        self.assertNotIn(str(self.bundled), clis.candidates("claude"))
        with patch.object(clis.sys, "platform", "linux"):
            self.assertEqual(clis.candidates("codex"), [str(self.terminal)])

    def test_model_refresh_reselects_updated_cli(self):
        self.assertEqual(clis.which("codex"), str(self.bundled))
        self.versions[str(self.terminal)] = (0, 160, 0)
        self.assertEqual(clis.which("codex"), str(self.bundled))  # 기존 실행 중 캐시

        async def probe():
            return [{"id": clis.which("codex")}]

        with patch.dict(models.PROBES, codex=probe):
            result = asyncio.run(models.refresh(self.root, ["codex"]))
        self.assertEqual(result["clis"]["codex"]["models"], [{"id": str(self.terminal)}])
        self.assertEqual(models.read_cache(self.root), result)
