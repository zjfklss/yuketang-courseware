"""量「取一次数据我们自己花了多少时间」——不含平台网络延迟。

    python3 tests/bench_fetch.py [次数]

判据：假服务端把网络延迟设成 0，那么每次取数的耗时就是纯开销。
超过 5ms/次 说明每次都在重复做「找端口、找页面」这类本可以缓存的事。
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_devtools import FakeDevTools, patch_as_browser  # noqa: E402

from yuketang_courseware import browser as browser_mod  # noqa: E402


def run(rounds: int = 60) -> dict:
    fake = FakeDevTools()
    patch_as_browser(browser_mod, Path("/tmp/fake-profile"))
    api = browser_mod.CoursewareBrowser(data_dir=Path("/tmp/fake-profile"))
    try:
        # 预热：第一次总要做完整发现
        api.fetch_json(fake.origin, fake.host, "/v2/api/web/courses/list?identity=2", None, None)
        start_counts = fake.counts()
        samples = []
        for _ in range(rounds):
            began = time.perf_counter()
            api.fetch_json(
                fake.origin, fake.host, "/v2/api/web/courses/list?identity=2", None, None
            )
            samples.append((time.perf_counter() - began) * 1000)
        end_counts = fake.counts()
        return {
            "per_call_ms": statistics.median(samples),
            "p95_ms": sorted(samples)[int(len(samples) * 0.95) - 1],
            "http_per_call": (end_counts["http"] - start_counts["http"]) / rounds,
            "evals": end_counts["evaluate"] - start_counts["evaluate"],
            "conns": end_counts["connections"] - start_counts["connections"],
            "rounds": rounds,
        }
    finally:
        api.terminate_profile() if False else None
        fake.close()


if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    got = run(rounds)
    print(f"每次取数    中位 {got['per_call_ms']:.2f} ms   P95 {got['p95_ms']:.2f} ms")
    print(f"每次 HTTP   {got['http_per_call']:.2f} 次（/json/version + /json/list）")
    print(f"共 {got['evals']} 次求值 / {got['conns']} 条新连接 / {got['rounds']} 次调用")
