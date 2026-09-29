import os
import threading

import pytest

from duet.core.agreement import fingerprint


@pytest.mark.fifo
def test_fingerprint_skips_fifo_without_blocking(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    os.mkfifo(tmp_path / "pipe")  # 쓰는 쪽이 없는 named pipe: 열면 영원히 대기한다
    result = {}
    t = threading.Thread(target=lambda: result.update(fp=fingerprint(tmp_path)), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "fingerprint 가 FIFO 에서 멈췄습니다"
    values, errors = result["fp"]
    assert "a.txt" in values and "pipe" not in values and not errors


def test_fingerprint_detects_change_after_cache(tmp_path):
    f = tmp_path / "b.txt"
    f.write_text("one")
    before, _ = fingerprint(tmp_path)
    f.write_text("two!")
    after, _ = fingerprint(tmp_path)
    assert before["b.txt"] != after["b.txt"]
