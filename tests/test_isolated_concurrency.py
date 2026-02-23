"""E2E concurrency test: verify isolated mode multi-session with own Chrome per session.

Creates 3 sessions simultaneously, each should get its own Chrome process
on a unique port. Then interacts with all 3 in parallel.
"""

import asyncio
import sys

sys.path.insert(0, "/var/html/personal/useful-mcp/src")

from wsl_chrome_mcp.chrome_pool import ChromePoolManager


async def main() -> None:
    pool = ChromePoolManager(
        port_min=9230,  # Avoid conflict with any existing Chrome on 9222
        headless=False,
    )

    sessions = ["iso_A", "iso_B", "iso_C"]
    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        status = "PASS" if condition else "FAIL"
        if condition:
            passed += 1
        else:
            failed += 1
        suffix = f" ({detail})" if detail else ""
        print(f"  [{status}] {name}{suffix}")

    try:
        # --- Step 1: Create 3 sessions concurrently ---
        print("\n=== Step 1: Create 3 isolated sessions concurrently ===")
        instances = await asyncio.gather(
            pool.get_or_create(sessions[0]),
            pool.get_or_create(sessions[1]),
            pool.get_or_create(sessions[2]),
        )
        inst_a, inst_b, inst_c = instances

        for label, inst in [("A", inst_a), ("B", inst_b), ("C", inst_c)]:
            print(
                f"  Session {label}: port={inst.port}, "
                f"tab={inst.current_target_id[:12]}, "
                f"pid={inst.pid}, "
                f"connected={inst.is_connected}, "
                f"owns_chrome={inst.owns_chrome}"
            )

        # --- Step 2: Verify isolation ---
        print("\n=== Step 2: Verify isolation ===")
        ports = {inst.port for inst in instances}
        check("All sessions on different ports", len(ports) == 3, f"ports={ports}")

        pids = {inst.pid for inst in instances}
        check("All sessions have different PIDs", len(pids) == 3, f"pids={pids}")

        check("Session A owns Chrome", inst_a.owns_chrome)
        check("Session B owns Chrome", inst_b.owns_chrome)
        check("Session C owns Chrome", inst_c.owns_chrome)

        targets = {inst.current_target_id for inst in instances}
        check("All sessions have different tab IDs", len(targets) == 3)

        # --- Step 3: Navigate each to different URLs in parallel ---
        print("\n=== Step 3: Navigate all 3 in parallel ===")
        urls = [
            ("A", inst_a, "https://example.com"),
            ("B", inst_b, "https://httpbin.org/html"),
            ("C", inst_c, "https://www.google.com"),
        ]

        async def navigate(label: str, inst, url: str) -> bool:
            try:
                if inst.cdp and inst.cdp.is_connected:
                    await inst.cdp.send("Page.navigate", {"url": url})
                elif inst.proxy:
                    targets = await inst.proxy.list_targets()
                    target = next(
                        (t for t in targets if t.get("id") == inst.current_target_id),
                        None,
                    )
                    if target:
                        ws_url = target.get("webSocketDebuggerUrl", "")
                        if ws_url:
                            await inst.proxy.send_cdp_command(ws_url, "Page.navigate", {"url": url})
                print(f"  Session {label} -> {url}")
                return True
            except Exception as e:
                print(f"  Session {label} FAILED to navigate: {e}")
                return False

        nav_results = await asyncio.gather(
            navigate(*urls[0]),
            navigate(*urls[1]),
            navigate(*urls[2]),
        )
        await asyncio.sleep(3)  # Let pages load

        check("All 3 navigations succeeded", all(nav_results))

        # --- Step 4: Verify each session sees its own page ---
        print("\n=== Step 4: Verify page content isolation ===")

        async def get_url(label: str, inst) -> str:
            try:
                if inst.cdp and inst.cdp.is_connected:
                    result = await inst.cdp.send(
                        "Runtime.evaluate",
                        {"expression": "window.location.href", "returnByValue": True},
                    )
                elif inst.proxy:
                    targets = await inst.proxy.list_targets()
                    target = next(
                        (t for t in targets if t.get("id") == inst.current_target_id),
                        None,
                    )
                    if target:
                        return target.get("url", "unknown")
                    return "target_not_found"
                else:
                    return "no_connection"
                return result.get("result", {}).get("value", "unknown")
            except Exception as e:
                return f"error: {e}"

        url_a, url_b, url_c = await asyncio.gather(
            get_url("A", inst_a),
            get_url("B", inst_b),
            get_url("C", inst_c),
        )

        print(f"  Session A URL: {url_a}")
        print(f"  Session B URL: {url_b}")
        print(f"  Session C URL: {url_c}")

        check("Session A on example.com", "example.com" in url_a, url_a)
        check("Session B on httpbin.org", "httpbin.org" in url_b, url_b)
        check("Session C on google.com", "google" in url_c, url_c)

        # --- Step 5: Create tabs in parallel ---
        print("\n=== Step 5: Create additional tabs in parallel ===")
        tab_results = await asyncio.gather(
            pool.create_tab(sessions[0], "https://github.com"),
            pool.create_tab(sessions[1], "https://news.ycombinator.com"),
            pool.create_tab(sessions[2], "https://en.wikipedia.org"),
            return_exceptions=True,
        )

        for label, result in zip(["A", "B", "C"], tab_results):
            if isinstance(result, Exception):
                print(f"  Session {label} tab creation FAILED: {result}")
            else:
                print(f"  Session {label} new tab: {result[:12]}")

        tab_successes = sum(1 for r in tab_results if not isinstance(r, Exception))
        check("All 3 tab creations succeeded", tab_successes == 3)

        # Verify each session now has 2 targets
        for label, sid in zip(["A", "B", "C"], sessions):
            inst = pool._instances.get(sid)
            if inst:
                check(
                    f"Session {label} has 2 targets",
                    len(inst.targets) == 2,
                    f"got {len(inst.targets)}: {[t[:12] for t in inst.targets]}",
                )

        # --- Step 6: List sessions ---
        print("\n=== Step 6: List sessions ===")
        session_info = pool.list_sessions()
        check("Pool reports 3 sessions", len(session_info) == 3, f"got {len(session_info)}")

        # --- Summary ---
        print(f"\n{'=' * 50}")
        print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")
        print(f"{'=' * 50}")

    finally:
        print("\n=== Cleanup ===")
        for sid in sessions:
            try:
                await pool.destroy(sid)
                print(f"  Destroyed {sid}")
            except Exception as e:
                print(f"  Failed to destroy {sid}: {e}")

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    asyncio.run(main())
