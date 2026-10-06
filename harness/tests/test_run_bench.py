"""The bench runner's GPU discipline.

A cloud cell calls a network endpoint: it needs no GPU, so it must neither wait
for the production lock nor hold it. A local rung must still queue on it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "experiments" / "coder_bench"))

import run_bench  # noqa: E402


def test_local_rung_queues_on_the_production_lock(tmp_path):
    assert run_bench.gpu_lock_for("qwen3-coder:30b", tmp_path) == \
        run_bench.JOBS / ".gpu.lock"


def test_cloud_cell_stays_off_the_production_lock(tmp_path):
    path = run_bench.gpu_lock_for("nvidia/nemotron-3-super-120b-a12b:free",
                                  tmp_path)
    assert path != run_bench.JOBS / ".gpu.lock"
    assert path.parent == tmp_path


def test_the_anthropic_sdk_cell_is_a_cloud_cell_too(tmp_path):
    assert run_bench.gpu_lock_for("claude-haiku-4-5", tmp_path).parent == tmp_path
